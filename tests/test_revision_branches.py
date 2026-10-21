"""Same-file branch state, ancestry, transaction, and recovery contracts."""
from dataclasses import replace
from uuid import uuid4

import pytest

from tests.test_revision_restore import ACTOR, STAMP, history, metadata
from src.domain.meal_history_cancellation import MealHistoryTarget, build_meal_history_cancellation
from src.domain.projections import STATUS_EVENT_KIND
from src.domain.revisions import AuditChange, ChangeOperation, RevisionRecord
from src.services.revision_branches import MAIN_BRANCH_ID, create_branch, checkout_branch, list_branches
from src.services.revision_restore import prepare_snapshot_restore, restore_revision
from src.services.person_history import build_history_tables
from src.storage.encrypted_database import EncryptedDatabase, RevisionConflictError


def append_setting(session, value):
    head = session.database.latest_revision().metadata.revision_id
    snapshot = session.database.snapshot(head)
    ref = next(ref for ref in snapshot.entities if ref.entity_kind == "future.settings")
    revision_id = uuid4()
    session.append_revision(RevisionRecord(metadata(revision_id, head), (
        AuditChange(ref.entity_kind, ref.entity_id, ChangeOperation.UPDATE, snapshot.entities[ref], {"value": value}),
    )))
    return revision_id


def test_divergent_branches_restore_exact_state_and_history(history):
    session, first, second, run = history
    database = session.database
    original = database.snapshot(second).entities
    branch = create_branch(session, first, second, ACTOR, name="실험")
    assert database.snapshot(branch.revision_id).entities == database.snapshot(first).entities
    assert database.branch_revision_ids() == (first,)
    branch_edit = append_setting(session, "branch")
    branch_state = database.snapshot(branch_edit).entities
    main = checkout_branch(session, MAIN_BRANCH_ID, branch_edit, ACTOR)
    assert database.snapshot(main.revision_id).entities == original
    assert database.branch_revision_ids() == (first, second)
    main_edit = append_setting(session, "main")
    main_state = database.snapshot(main_edit).entities
    back = checkout_branch(session, branch.branch.branch_id, main_edit, ACTOR)
    assert database.snapshot(back.revision_id).entities == branch_state
    assert database.branch_revision_ids() == (first, branch_edit)
    again = checkout_branch(session, MAIN_BRANCH_ID, back.revision_id, ACTOR)
    assert database.snapshot(again.revision_id).entities == main_state
    assert database.document_run(run.run_id) == run
    assert len(database.revision_summaries()) == 8
    database.check_integrity()


def test_equal_state_fork_stale_duplicate_and_transaction_failure(history, monkeypatch):
    session, first, second, _ = history
    database = session.database
    result = create_branch(session, second, second, ACTOR, name="같은 상태")
    assert database.snapshot(result.revision_id).entities == database.snapshot(second).entities
    assert database.branch_revision_ids() == (first, second)
    with pytest.raises(RevisionConflictError):
        checkout_branch(session, MAIN_BRANCH_ID, second, ACTOR)
    with pytest.raises(ValueError, match="이미 사용"):
        create_branch(session, first, result.revision_id, ACTOR, name="같은 상태")
    saved = list_branches(database)
    def fail(*args, **kwargs):
        raise RuntimeError("injected projection failure")
    monkeypatch.setattr("src.storage.encrypted_database.apply_typed_projection_changes", fail)
    with pytest.raises(RuntimeError, match="injected"):
        checkout_branch(session, MAIN_BRANCH_ID, result.revision_id, ACTOR)
    assert database.latest_revision().metadata.revision_id == result.revision_id
    assert list_branches(database) == saved


def test_branch_registry_survives_encrypted_backup_and_restart(history, tmp_path):
    session, first, second, _ = history
    result = create_branch(session, first, second, ACTOR, name="보존")
    edited = append_setting(session, "saved")
    saved = list_branches(session.database)
    key = b"b" * 32
    path = session.database.backup_to(tmp_path / "backup.sqlite3", key)
    with EncryptedDatabase.open(path, key) as reopened:
        assert reopened.branches() == saved
        assert reopened.branch_revision_ids() == (first, edited)
        assert reopened.snapshot(edited).entities == session.database.snapshot(edited).entities
        reopened.check_integrity()


def test_foreign_branch_fork_and_checkout_alias(history):
    session, first, second, _ = history
    branch = create_branch(session, first, second, ACTOR, name="실험")
    edited = append_setting(session, "branch")
    main = checkout_branch(session, MAIN_BRANCH_ID, edited, ACTOR)
    fork = create_branch(session, edited, main.revision_id, ACTOR, name="다른 분기에서")
    assert fork.branch.source_branch_id == branch.branch.branch_id
    assert session.database.branch_revision_ids() == (first, edited)
    alias = create_branch(session, main.revision_id, fork.revision_id, ACTOR, name="전환에서")
    assert alias.branch.source_revision_id == second


def test_cancellation_is_branch_local_and_checkout_has_no_user_history(history):
    session, first, second, _ = history
    database = session.database
    event = next(c for c in database.revision(second).changes if c.entity_kind == STATUS_EVENT_KIND)
    target = MealHistoryTarget(second, STATUS_EVENT_KIND, event.entity_id)
    branch = create_branch(session, first, second, ACTOR, name="상태 이전")
    with pytest.raises(ValueError, match="찾을 수"):
        build_meal_history_cancellation(database.snapshot(branch.revision_id),
            database.branch_revision_chain(branch.revision_id), target,
            actor=ACTOR, recorded_at=STAMP, revision_id=uuid4())
    main = checkout_branch(session, MAIN_BRANCH_ID, branch.revision_id, ACTOR)
    original_tables = build_history_tables(database.roster_snapshot(second), database.snapshot(second),
                                           database.branch_revision_chain(second))
    current_tables = build_history_tables(database.current_roster(), database.snapshot(main.revision_id),
                                          database.branch_revision_chain(main.revision_id))
    assert current_tables == original_tables
    _, record = build_meal_history_cancellation(database.snapshot(main.revision_id),
        database.branch_revision_chain(main.revision_id), target,
        actor=ACTOR, recorded_at=STAMP, revision_id=uuid4())
    session.append_revision(record)
    assert not database.status_events()
    database.check_integrity()


def test_snapshot_restore_keeps_registry_and_other_branch_tip(history):
    session, first, second, _ = history
    database = session.database
    branch = create_branch(session, second, second, ACTOR, name="되돌리기")
    edited = append_setting(session, "temporary")
    restored = restore_revision(session, first, edited, ACTOR)
    branches = list_branches(database)
    assert next(b for b in branches if b.is_active).tip_revision_id == restored.revision_id
    assert next(b for b in branches if b.branch_id == MAIN_BRANCH_ID).tip_revision_id == second
    main = checkout_branch(session, MAIN_BRANCH_ID, restored.revision_id, ACTOR)
    assert database.snapshot(main.revision_id).entities == database.snapshot(second).entities
    back = checkout_branch(session, branch.branch.branch_id, main.revision_id, ACTOR)
    assert database.snapshot(back.revision_id).entities == database.snapshot(first).entities
    assert database.branch_revision_ids() == (first, second, edited, restored.revision_id)
    database.check_integrity()


def test_cancelling_common_source_in_one_branch_does_not_cancel_other(history):
    session, first, second, _ = history
    database = session.database
    event = next(c for c in database.revision(second).changes if c.entity_kind == STATUS_EVENT_KIND)
    target = MealHistoryTarget(second, STATUS_EVENT_KIND, event.entity_id)
    branch = create_branch(session, second, second, ACTOR, name="개별 취소")
    _, cancellation = build_meal_history_cancellation(database.snapshot(branch.revision_id),
        database.branch_revision_chain(branch.revision_id), target,
        actor=ACTOR, recorded_at=STAMP, revision_id=uuid4())
    session.append_revision(cancellation)
    main = checkout_branch(session, MAIN_BRANCH_ID, cancellation.metadata.revision_id, ACTOR)
    assert len(database.status_events()) == 1
    _, main_cancellation = build_meal_history_cancellation(database.snapshot(main.revision_id),
        database.branch_revision_chain(main.revision_id), target,
        actor=ACTOR, recorded_at=STAMP, revision_id=uuid4())
    session.append_revision(main_cancellation)
    assert not database.status_events()
    database.check_integrity()


def test_foreign_restore_is_rejected_without_changing_state_or_registry(history):
    session, first, second, _ = history
    branch = create_branch(session, first, second, ACTOR, name="별도")
    edited = append_setting(session, "foreign")
    main = checkout_branch(session, MAIN_BRANCH_ID, edited, ACTOR)
    saved = list_branches(session.database)
    with pytest.raises(ValueError, match="다른 분기"):
        restore_revision(session, edited, main.revision_id, ACTOR)
    assert session.database.latest_revision().metadata.revision_id == main.revision_id
    assert session.database.snapshot(main.revision_id).entities == session.database.snapshot(second).entities
    assert list_branches(session.database) == saved


def test_legacy_bootstrap_and_direct_active_checkout(history):
    session, first, second, _ = history
    branches = list_branches(session.database)
    assert len(branches) == 1
    assert branches[0].branch_id == MAIN_BRANCH_ID
    assert branches[0].tip_revision_id == second
    result = checkout_branch(session, MAIN_BRANCH_ID, second, ACTOR)
    assert result.revision_id is None
    assert not result.backup.attempted
    assert session.database.latest_revision().metadata.revision_id == second
    assert session.database.branch_revision_ids() == (first, second)
    with pytest.raises(RevisionConflictError):
        checkout_branch(session, MAIN_BRANCH_ID, first, ACTOR)
    session.database.check_integrity()


def test_malformed_branch_metadata_fails_before_commit(history):
    session, first, second, _ = history
    record = prepare_snapshot_restore(session.database, first, second, ACTOR)
    record = replace(record, metadata=replace(record.metadata, decisions={
        **record.metadata.decisions,
        "workspace_branch": {"operation": "create", "branch_id": 123, "name": "잘못된 기록"},
    }))
    with pytest.raises(ValueError):
        session.append_revision(record)
    assert session.database.latest_revision().metadata.revision_id == second
    assert len(list_branches(session.database)) == 1
