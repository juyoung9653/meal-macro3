"""Exact historical restoration and independent encrypted database branches."""
from __future__ import annotations

import secrets
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID, uuid4

from src.domain.revisions import (
    AuditChange, ChangeOperation, HistoricalSnapshot, RevisionActor,
    RevisionMetadata, RevisionRecord, RevisionSource,
)
from src.storage.database_credentials import DatabaseCredentials, credential_path_for
from src.storage.database_session import AutomaticBackupResult, DatabaseSession
from src.storage.encrypted_database import EncryptedDatabase, RevisionConflictError
from src.storage.file_lock import database_lock

RESTORE_TARGET_KEY = "snapshot_restore_target"
BRANCH_KEY = "snapshot_branch"


@dataclass(frozen=True)
class RevisionRestoreResult:
    revision_id: UUID | None
    backup: AutomaticBackupResult


@dataclass(frozen=True)
class RevisionBranchResult:
    path: Path
    database_id: UUID
    revision_id: UUID
    backup: AutomaticBackupResult


def _snapshot_changes(current: HistoricalSnapshot, target: HistoricalSnapshot):
    changes = []
    for ref in sorted(set(current.entities) | set(target.entities)):
        before, after = current.entities.get(ref), target.entities.get(ref)
        if before == after:
            continue
        operation = (ChangeOperation.INSERT if before is None else
                     ChangeOperation.DELETE if after is None else ChangeOperation.UPDATE)
        changes.append(AuditChange(ref.entity_kind, ref.entity_id, operation, before, after))
    return tuple(changes)


def _branch_marker_changes(current, target, branch):
    changes = _snapshot_changes(current, target)
    if not changes and branch is not None:
        # The ledger requires a change. A same-value update records branch lineage
        # without adding an entity that would spoil exact historical equivalence.
        if not target.entities:
            raise ValueError("내용이 없는 변경번호에서는 분기를 만들 수 없습니다.")
        ref = min(target.entities)
        state = target.entities[ref]
        changes = (AuditChange(ref.entity_kind, ref.entity_id, ChangeOperation.UPDATE, state, state),)
    return changes


def validate_snapshot_restore(current, target, record) -> None:
    """Called inside the storage transaction before bypassing edit propagation."""
    if record.metadata.parent_revision_id != current.revision_id:
        raise RevisionConflictError("현재 변경번호가 달라졌습니다. 변경 이력을 다시 열어주세요.")
    if record.metadata.source is not RevisionSource.UI or record.import_batch is not None:
        raise ValueError("복구 기록 형식이 올바르지 않습니다.")
    validate_snapshot_restore_changes(current, target, record.changes, record.metadata.decisions)


def validate_snapshot_restore_changes(current, target, changes, decisions) -> None:
    """Verify the entire restore diff both when saving and when replaying a ledger."""
    if decisions.get(RESTORE_TARGET_KEY) != str(target.revision_id):
        raise ValueError("복구 대상 변경번호가 일치하지 않습니다.")
    branch = decisions.get(BRANCH_KEY)
    if branch is not None:
        if not isinstance(branch, Mapping) or not isinstance(branch.get("name"), str) or not branch["name"].strip():
            raise ValueError("분기 정보가 올바르지 않습니다.")
        UUID(branch["source_database_id"])
        if branch.get("source_revision_id") != str(target.revision_id) or branch.get("source_head_revision_id") != str(current.revision_id):
            raise ValueError("분기 원본 변경번호가 일치하지 않습니다.")
    expected = _branch_marker_changes(current, target, branch or decisions.get("workspace_branch"))
    if tuple(changes) != expected:
        raise ValueError("복구 변경 내용이 선택한 변경번호의 전체 상태와 일치하지 않습니다.")


def prepare_snapshot_restore(database, target_revision_id, expected_head, actor, *, branch=None):
    latest = database.latest_revision()
    if latest is None or latest.metadata.revision_id != expected_head:
        raise RevisionConflictError("현재 변경번호가 달라졌습니다. 변경 이력을 다시 열어주세요.")
    current = database.snapshot(expected_head)
    target = database.snapshot(target_revision_id)
    changes = _branch_marker_changes(current, target, branch)
    if not changes:
        return None
    code = database.revision_display_code(target_revision_id)
    decisions = {RESTORE_TARGET_KEY: str(target_revision_id)}
    if branch is not None:
        decisions[BRANCH_KEY] = branch
    record = RevisionRecord(RevisionMetadata(
        revision_id=uuid4(), parent_revision_id=expected_head,
        recorded_at=datetime.now(UTC), effective_on=None, actor=actor,
        reason=(f"분기 생성 · {branch['name']} · {code}" if branch is not None else f"이전 변경번호 복구 · {code}"),
        source=RevisionSource.UI, decisions=decisions,
    ), changes)
    validate_snapshot_restore(current, target, record)
    return record


def restore_revision(session, target_revision_id: UUID, expected_head: UUID, actor: RevisionActor):
    """Append the complete historical entity diff; retain every existing revision."""
    record = prepare_snapshot_restore(session.database, target_revision_id, expected_head, actor)
    if record is None:
        return RevisionRestoreResult(None, AutomaticBackupResult())
    backup = session.append_revision(record)
    return RevisionRestoreResult(record.metadata.revision_id, backup)


def create_revision_branch(
    session, target_revision_id: UUID, expected_head: UUID, actor: RevisionActor,
    destination: str | Path, password: str, *, name: str,
):
    """Copy all audit/document history, restore in the copy, and publish new keys."""
    target = Path(destination).resolve()
    if target == session.path.resolve():
        raise ValueError("현재 운영 파일과 다른 위치를 선택해주세요.")
    if target.suffix.casefold() != ".sqlite3" or not target.parent.is_dir():
        raise ValueError("존재하는 폴더에서 .sqlite3 파일 위치를 선택해주세요.")
    name = name.strip()
    if not name:
        raise ValueError("분기 이름을 입력해주세요.")
    latest = session.database.latest_revision()
    if latest is None or latest.metadata.revision_id != expected_head:
        raise RevisionConflictError("현재 변경번호가 달라졌습니다. 변경 이력을 다시 열어주세요.")
    expected_entities = session.database.snapshot(target_revision_id).entities
    lineage = {
        "name": name, "source_database_id": str(session.credentials.database_id),
        "source_revision_id": str(target_revision_id),
        "source_head_revision_id": str(expected_head),
    }
    destination_credentials = credential_path_for(target)
    with database_lock(target):
        if target.exists() or destination_credentials.exists():
            raise FileExistsError("같은 이름의 운영 파일 또는 비밀번호 정보가 있습니다. 새 이름을 선택해주세요.")
        # Everything is encrypted even while staged, on the destination filesystem.
        with TemporaryDirectory(prefix=".revision-branch-", dir=target.parent) as temporary:
            staged = Path(temporary) / target.name
            key = secrets.token_bytes(32)
            session.database.backup_to(staged, key)
            credentials, _ = DatabaseCredentials.create(staged, password, database_key=key)
            with DatabaseSession.open(staged, password) as branch_session:
                record = prepare_snapshot_restore(
                    branch_session.database, target_revision_id, expected_head, actor, branch=lineage,
                )
                branch_session.append_revision(record)
                branch_session.database.check_integrity()
                if branch_session.database.snapshot(record.metadata.revision_id).entities != expected_entities:
                    raise ValueError("분기 검증 결과가 선택한 변경번호와 다릅니다.")
            installed = []
            try:
                for source, destination_path in (
                    (staged, target), (credential_path_for(staged), destination_credentials),
                ):
                    # Exclusive create never replaces another file, including a race.
                    with destination_path.open("xb") as output:
                        installed.append(destination_path)
                        with source.open("rb") as input_file:
                            shutil.copyfileobj(input_file, output)
                with EncryptedDatabase.open(target, key) as installed_database:
                    installed_database.check_integrity()
            except BaseException:
                for installed_path in reversed(installed):
                    installed_path.unlink(missing_ok=True)
                raise
        # Publication succeeded. A backup failure must not report creation failure.
        try:
            with DatabaseSession(
                EncryptedDatabase.open(target, key), DatabaseCredentials.load(target), key,
                backup_directory=session.backup_directory,
            ) as branch_session:
                backup = branch_session.try_backup_now()
        except Exception as error:
            backup = AutomaticBackupResult(error=error)
        return RevisionBranchResult(target, credentials.database_id, record.metadata.revision_id, backup)
