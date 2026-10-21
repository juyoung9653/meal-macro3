"""Named workspaces reconstructed from the immutable encrypted audit ledger."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

from src.domain.revisions import RevisionMetadata, RevisionRecord, RevisionSource
from src.services.revision_restore import RESTORE_TARGET_KEY, _branch_marker_changes
from src.storage.database_session import AutomaticBackupResult

BRANCH_EVENT_KEY = "workspace_branch"
MAIN_BRANCH_ID = UUID(int=0)


@dataclass(frozen=True)
class RevisionBranch:
    branch_id: UUID
    name: str
    tip_revision_id: UUID | None
    is_active: bool
    source_revision_id: UUID | None = None
    source_branch_id: UUID | None = None


@dataclass(frozen=True)
class BranchResult:
    revision_id: UUID | None
    branch: RevisionBranch
    backup: AutomaticBackupResult


class BranchRegistry:
    """Replay metadata; entity restoration cannot overwrite branch pointers."""
    def __init__(self):
        self.active = MAIN_BRANCH_ID
        self.branches = {MAIN_BRANCH_ID: RevisionBranch(MAIN_BRANCH_ID, "main", None, True)}
        self.parents = {}
        self.logical_tips = {}
        self.owners = {}
        self.head = None

    def apply(self, revision_id, decisions):
        event = decisions.get(BRANCH_EVENT_KEY)
        if event is None:
            tip = self.branches[self.active].tip_revision_id
            restore_target = decisions.get(RESTORE_TARGET_KEY)
            if restore_target is not None and len(self.branches) > 1 and "snapshot_branch" not in decisions:
                target = self.logical_tips.get(UUID(str(restore_target)))
                if target is None or target not in self.revision_ids(tip):
                    raise ValueError("다른 분기의 변경번호는 복구 대신 분기 전환 또는 새 분기 만들기를 사용해주세요.")
            self.owners[revision_id] = self.active
            self.parents[revision_id] = tip
            self.logical_tips[revision_id] = revision_id
            self.branches[self.active] = replace(self.branches[self.active], tip_revision_id=revision_id)
        else:
            if not isinstance(event, Mapping) or decisions.get(RESTORE_TARGET_KEY) is None:
                raise ValueError("분기 기록 형식이 올바르지 않습니다.")
            branch_id = UUID(str(event["branch_id"]))
            target = UUID(str(decisions[RESTORE_TARGET_KEY]))
            if target not in self.logical_tips:
                raise ValueError("분기 대상 변경번호가 없습니다.")
            if event.get("operation") == "create":
                name = event.get("name")
                if not isinstance(name, str) or not name.strip() or name != name.strip():
                    raise ValueError("분기 이름을 입력해주세요.")
                if branch_id in self.branches or any(b.name.casefold() == name.casefold() for b in self.branches.values()):
                    raise ValueError("이미 사용 중인 분기 이름입니다.")
                if target not in self.owners:
                    raise ValueError("분기 전환 기록 대신 실제 변경번호를 선택해주세요.")
                self.branches[branch_id] = RevisionBranch(branch_id, name, target, False, target, self.owners[target])
            elif event.get("operation") == "checkout":
                if branch_id not in self.branches or self.branches[branch_id].tip_revision_id != target:
                    raise ValueError("분기 끝 변경번호가 일치하지 않습니다.")
            else:
                raise ValueError("분기 작업이 올바르지 않습니다.")
            self.active = branch_id
            self.logical_tips[revision_id] = self.logical_tips[target]
        self.head = revision_id

    def revision_ids(self, revision_id):
        cursor = self.logical_tips[revision_id]
        result = []
        while cursor is not None:
            result.append(cursor)
            cursor = self.parents[cursor]
        return tuple(reversed(result))

    def listed(self):
        return tuple(replace(b, is_active=b.branch_id == self.active) for b in self.branches.values())


def registry_from_connection(connection):
    import json
    registry = BranchRegistry()
    for revision_id, decisions in connection.execute("SELECT revision_id, decisions_json FROM revision ORDER BY sequence"):
        registry.apply(UUID(str(revision_id)), json.loads(str(decisions)))
    return registry


def list_branches(database):
    return database.branches()


def create_branch(session, target_revision_id, expected_head, actor, *, name):
    lineage = session.database.branch_revision_ids(target_revision_id)
    if not lineage:
        raise ValueError("분기를 만들 변경번호가 없습니다.")
    target_revision_id = lineage[-1]
    return _switch(session, target_revision_id, expected_head, actor,
                   {"operation": "create", "branch_id": str(uuid4()), "name": name.strip()})


def checkout_branch(session, branch_id, expected_head, actor):
    branch = next((b for b in list_branches(session.database) if b.branch_id == branch_id), None)
    if branch is None:
        raise ValueError("분기를 찾을 수 없습니다.")
    return _switch(session, branch.tip_revision_id, expected_head, actor,
                   {"operation": "checkout", "branch_id": str(branch_id)})


def _switch(session, target_revision_id, expected_head, actor, event):
    from src.storage.encrypted_database import RevisionConflictError
    database = session.database
    latest = database.latest_revision()
    if latest is None or latest.metadata.revision_id != expected_head:
        raise RevisionConflictError("현재 변경번호가 달라졌습니다. 변경 이력을 다시 열어주세요.")
    active = next(branch for branch in list_branches(database) if branch.is_active)
    if event["operation"] == "checkout" and str(active.branch_id) == event["branch_id"]:
        return BranchResult(None, active, AutomaticBackupResult())
    current, target = database.snapshot(expected_head), database.snapshot(target_revision_id)
    changes = _branch_marker_changes(current, target, event)
    record = RevisionRecord(RevisionMetadata(
        uuid4(), expected_head, datetime.now(UTC), None, actor,
        "분기 생성" if event["operation"] == "create" else "분기 전환",
        RevisionSource.UI, decisions={RESTORE_TARGET_KEY: str(target_revision_id), BRANCH_EVENT_KEY: event},
    ), changes)
    backup = session.append_revision(record)
    branch = next(b for b in list_branches(database) if b.is_active)
    return BranchResult(record.metadata.revision_id, branch, backup)
