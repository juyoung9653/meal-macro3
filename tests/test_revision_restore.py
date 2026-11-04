"""Historical restore and branch isolation with synthetic encrypted databases."""
from dataclasses import replace
from datetime import UTC, date, datetime
from uuid import uuid4

import pytest

from src.domain.documents import DocumentKind, DocumentRun
from src.domain.household_shared import HOUSEHOLD_SHARED_KIND, HOUSEHOLD_SHARED_ID
from src.domain.projections import roster_audit_changes, status_event_audit_change
from src.domain.revisions import AuditChange, ChangeOperation, ImportBatch, RevisionActor, RevisionMetadata, RevisionRecord, RevisionSource
from src.domain.roster import Household, HouseholdMembership, InclusiveDateRange, Meal, MealEnrollment, Person, PersonProfile, RosterSnapshot
from src.domain.status import MealStatus, StatusEvent
from src.services.revision_restore import create_revision_branch, restore_revision, prepare_snapshot_restore
from src.storage.database_credentials import credential_path_for
from src.storage.database_session import DatabaseSession
from src.storage.encrypted_database import RevisionConflictError

ACTOR = RevisionActor("synthetic", "test-sid")
DAY = date(2026, 10, 1)
STAMP = datetime(2026, 10, 1, tzinfo=UTC)


def metadata(revision_id, parent, *, source=RevisionSource.UI):
    return RevisionMetadata(revision_id, parent, STAMP, DAY, ACTOR, "합성 검증", source)


@pytest.fixture
def history(tmp_path):
    backups = tmp_path / "backups"
    backups.mkdir()
    with DatabaseSession.create(tmp_path / "source.sqlite3", "source-password", backup_directory=backups) as session:
        if session.database.schema_version < 11:
            # Temporary support for seeding legacy-only states with the current writer.
            session.database._require_connection().execute("ALTER TABLE person_projection ADD COLUMN lunch_period_json TEXT")
        first, second = uuid4(), uuid4()
        person, household, setting = uuid4(), uuid4(), uuid4()
        before = RosterSnapshot(first,
            persons=(Person(person, PersonProfile("합성 대상", note="옛 메모", mobile_phone="010-0000-0000")),),
            households=(Household(household),), memberships=(HouseholdMembership(person, household),),
            enrollments=(MealEnrollment(person, Meal.DINNER, InclusiveDateRange(DAY)),))
        batch = ImportBatch(uuid4(), first, "synthetic.xlsx", "a" * 64, 10, "명단", "header", {}, DAY)
        session.append_revision(RevisionRecord(metadata(first, None, source=RevisionSource.IMPORT), (
            *roster_audit_changes(None, before),
            AuditChange("future.settings", setting, ChangeOperation.INSERT, None, {"password": "old-secret", "nested": {"value": 1}}),
        ), batch))
        p2 = uuid4()
        after = replace(before, revision_id=second,
            persons=(replace(before.persons[0], profile=replace(before.persons[0].profile, note="새 메모")), Person(p2, PersonProfile("추가 대상"))),
            memberships=(*before.memberships, HouseholdMembership(p2, household)),
            enrollments=(*before.enrollments, MealEnrollment(p2, Meal.DINNER, InclusiveDateRange(DAY))))
        status = StatusEvent(uuid4(), second, person, Meal.DINNER, MealStatus.PAUSED, InclusiveDateRange(DAY, DAY), STAMP, "검증")
        session.append_revision(RevisionRecord(metadata(second, first), (
            *roster_audit_changes(before, after), status_event_audit_change(status),
            AuditChange("future.settings", setting, ChangeOperation.UPDATE, {"password": "old-secret", "nested": {"value": 1}}, {"password": "new-secret", "nested": {"value": 2}}),
            AuditChange("future.added", uuid4(), ChangeOperation.INSERT, None, {"value": "remove-on-restore"}),
        )))
        run = DocumentRun(uuid4(), DocumentKind.CONFIRMATION, second, session.database.revision_display_code(second), DAY, DAY, STAMP, ACTOR, {"version": 1}, {"version": 1}, "synthetic.xlsx", "c" * 64, 10)
        session.append_document_run(run)
        yield session, first, second, run


def test_restore_exact_all_entities_status_settings_and_immutable_history(history):
    session, first, second, run = history
    original_record = session.database.revision(first)
    target = session.database.snapshot(first).entities
    result = restore_revision(session, first, second, ACTOR)
    assert result.revision_id is not None
    assert result.backup.succeeded
    assert session.database.snapshot(result.revision_id).entities == target
    assert len(session.database.current_roster().persons) == 1
    assert session.database.status_events() == ()
    assert session.database.revision(first) == original_record
    assert session.database.revision(second).metadata.revision_id == second
    assert session.database.document_run(run.run_id) == run
    assert len(session.database.revision_summaries()) == 3
    session.database.check_integrity()
    again = restore_revision(session, first, result.revision_id, ACTOR)
    assert again.revision_id is None
    assert not again.backup.attempted
    assert len(session.database.revision_summaries()) == 3


def test_restore_rejects_stale_and_forged_partial_diff(history):
    session, first, second, _ = history
    with pytest.raises(RevisionConflictError):
        restore_revision(session, first, first, ACTOR)
    record = prepare_snapshot_restore(session.database, first, second, ACTOR)
    with pytest.raises(ValueError, match="전체 상태"):
        session.append_revision(replace(record, changes=record.changes[:1]))
    assert session.database.latest_revision().metadata.revision_id == second


def test_restore_bypasses_household_edit_propagation(history, monkeypatch):
    session, first, second, _ = history
    person = session.database.current_roster().persons[0].person_id
    third = uuid4()
    session.append_revision(RevisionRecord(metadata(third, second), (
        AuditChange(HOUSEHOLD_SHARED_KIND, HOUSEHOLD_SHARED_ID, ChangeOperation.INSERT, None, {"columns": ["note"]}),
    )))
    def forbidden(*args, **kwargs):
        raise AssertionError("snapshot restoration must not propagate edits")
    monkeypatch.setattr("src.domain.household_shared.expand_household_shared_revision", forbidden)
    result = restore_revision(session, first, third, ACTOR)
    assert session.database.snapshot(result.revision_id).entities == session.database.snapshot(first).entities


def test_branch_independent_identity_keys_history_backup_and_future_writes(history, tmp_path):
    session, first, second, run = history
    source_hash = session.path.read_bytes()
    source_credentials = credential_path_for(session.path).read_bytes()
    destination = tmp_path / "branch.sqlite3"
    result = create_revision_branch(session, first, second, ACTOR, destination, "branch-password", name="검토 분기")
    assert result.backup.succeeded
    assert result.database_id != session.credentials.database_id
    with DatabaseSession.open(destination, "branch-password") as branch:
        assert branch.credentials.unlock_user("branch-password") != session.credentials.unlock_user("source-password")
        assert branch.database.snapshot(result.revision_id).entities == session.database.snapshot(first).entities
        assert branch.database.revision(first).import_batch == session.database.revision(first).import_batch
        assert branch.database.document_run(run.run_id) == run
        lineage = branch.database.latest_revision().metadata.decisions["snapshot_branch"]
        assert lineage["source_database_id"] == str(session.credentials.database_id)
        assert lineage["source_revision_id"] == str(first)
        branch.append_revision(RevisionRecord(metadata(uuid4(), result.revision_id), (
            AuditChange("future.branch-only", uuid4(), ChangeOperation.INSERT, None, {"value": 1}),
        )))
    assert session.database.latest_revision().metadata.revision_id == second
    assert session.path.read_bytes() == source_hash
    assert credential_path_for(session.path).read_bytes() == source_credentials
    with DatabaseSession.restore_and_open_with_user_password(result.backup.record.path, tmp_path / "recovered.sqlite3", "branch-password") as recovered:
        assert recovered.credentials.database_id == result.database_id
        assert recovered.database.snapshot(result.revision_id).entities == session.database.snapshot(first).entities


def test_branch_current_revision_keeps_exact_snapshot_and_records_lineage(history, tmp_path):
    session, _, head, _ = history
    result = create_revision_branch(session, head, head, ACTOR, tmp_path / "current-branch.sqlite3", "branch-password", name="현재 분기")
    with DatabaseSession.open(result.path, "branch-password") as branch:
        assert branch.database.snapshot(result.revision_id).entities == session.database.snapshot(head).entities
        assert branch.database.latest_revision().metadata.decisions["snapshot_branch"]["name"] == "현재 분기"


def test_branch_existing_destination_and_invalid_password_leave_source_safe(history, tmp_path):
    session, first, head, _ = history
    destination = tmp_path / "keep.sqlite3"
    destination.write_bytes(b"existing-file")
    with pytest.raises(FileExistsError):
        create_revision_branch(session, first, head, ACTOR, destination, "password", name="분기")
    assert destination.read_bytes() == b"existing-file"
    failed = tmp_path / "failed.sqlite3"
    with pytest.raises(ValueError):
        create_revision_branch(session, first, head, ACTOR, failed, "", name="분기")
    assert not failed.exists()
    assert not credential_path_for(failed).exists()
    assert not list(tmp_path.glob(".revision-branch-*"))
    assert session.database.latest_revision().metadata.revision_id == head


def test_branch_publish_failure_removes_only_new_files(history, tmp_path, monkeypatch):
    session, first, head, _ = history
    original = __import__("shutil").copyfileobj
    calls = []
    def copy(input_file, output):
        calls.append(output.name)
        if len(calls) == 2:
            raise OSError("injected credential write failure")
        return original(input_file, output)
    monkeypatch.setattr("src.services.revision_restore.shutil.copyfileobj", copy)
    destination = tmp_path / "failed.sqlite3"
    with pytest.raises(OSError):
        create_revision_branch(session, first, head, ACTOR, destination, "password", name="분기")
    assert not destination.exists()
    assert not credential_path_for(destination).exists()
    assert session.database.latest_revision().metadata.revision_id == head


def test_repeated_restore_resurrects_status_and_deleted_ids_with_original_priority(history):
    session, first, second, _ = history
    expected = session.database.snapshot(second).entities
    events = session.database.status_events()
    restored_a = restore_revision(session, first, second, ACTOR)
    restored_b = restore_revision(session, second, restored_a.revision_id, ACTOR)
    assert restored_b.backup.succeeded
    assert session.database.snapshot(restored_b.revision_id).entities == expected
    assert session.database.status_events() == events
    assert all(event.revision_id == second for event in session.database.status_events())
    restored_again = restore_revision(session, first, restored_b.revision_id, ACTOR)
    assert restored_again.backup.succeeded
    assert session.database.snapshot(restored_again.revision_id).entities == session.database.snapshot(first).entities
    session.database.check_integrity()


def test_real_schema_seven_upgrades_then_restores_status_provenance(tmp_path, monkeypatch):
    import src.storage.encrypted_database as storage
    from src.storage.migrations import MIGRATIONS
    with monkeypatch.context() as older:
        older.setattr(storage, "MIGRATIONS", tuple(m for m in MIGRATIONS if m.version <= 7))
        older.setattr(storage, "CURRENT_SCHEMA_VERSION", 7)
        generator = history.__wrapped__(tmp_path)
        session, first, second, _ = next(generator)
        assert session.database.schema_version == 7
        original_events = session.database.status_events()
        source = session.path
        session.database._require_connection().execute("ALTER TABLE person_projection DROP COLUMN lunch_period_json")
        generator.close()
    with DatabaseSession.open(source, "source-password") as session:
        assert session.database.schema_version == MIGRATIONS[-1].version
        a = restore_revision(session, first, second, ACTOR)
        b = restore_revision(session, second, a.revision_id, ACTOR)
        assert session.database.status_events() == original_events
        assert session.database.snapshot(b.revision_id).entities == session.database.snapshot(second).entities
        session.database.check_integrity()


def test_tampered_restore_marker_is_rejected_during_ledger_integrity(history):
    import json
    from src.storage.encrypted_database import DatabaseIntegrityError
    session, first, second, _ = history
    connection = session.database._require_connection()
    # Corrupt this synthetic fixture to prove that merely adding a marker does
    # not authorize ordinary INSERTs to bypass historical equivalence on reopen.
    connection.execute("DROP TRIGGER revision_no_update")
    connection.execute("UPDATE revision SET decisions_json = ? WHERE revision_id = ?", (json.dumps({"snapshot_restore_target": str(first)}), str(second)))
    with pytest.raises(DatabaseIntegrityError, match="복구 기록"):
        session.database.check_integrity()


def test_restored_tied_status_order_matches_snapshot_projection_and_document_source(history):
    from uuid import UUID
    from src.domain.projections import status_events_from_historical_snapshot
    from src.domain.status import resolve_status_history
    session, first, second, _ = history
    person = session.database.current_roster().persons[0].person_id
    third = uuid4()
    paused = StatusEvent(UUID(int=2), third, person, Meal.DINNER, MealStatus.PAUSED, InclusiveDateRange(DAY, DAY), STAMP)
    delivery = StatusEvent(UUID(int=1), third, person, Meal.DINNER, MealStatus.DELIVERY, InclusiveDateRange(DAY), STAMP)
    session.append_revision(RevisionRecord(metadata(third, second), (status_event_audit_change(paused), status_event_audit_change(delivery))))
    expected = session.database.status_events(person_id=person, meal=Meal.DINNER)
    assert resolve_status_history(person, Meal.DINNER, expected).status_on(DAY) is MealStatus.DELIVERY
    for _ in range(2):
        head = session.database.latest_revision().metadata.revision_id
        a = restore_revision(session, first, head, ACTOR)
        b = restore_revision(session, third, a.revision_id, ACTOR)
        for snapshot in (session.database.snapshot(b.revision_id), session.database._current_entity_snapshot(b.revision_id)):
            actual = status_events_from_historical_snapshot(snapshot, person_id=person, meal=Meal.DINNER)
            assert actual == expected
            assert resolve_status_history(person, Meal.DINNER, actual).status_on(DAY) is MealStatus.DELIVERY
        assert session.database.status_events(person_id=person, meal=Meal.DINNER) == expected
        run = DocumentRun(uuid4(), DocumentKind.CONFIRMATION, b.revision_id, session.database.revision_display_code(b.revision_id), DAY, DAY, STAMP, ACTOR, {"version": 1}, {"version": 1}, "synthetic.xlsx", "c" * 64, 10)
        session.append_document_run(run)
        source_events = status_events_from_historical_snapshot(session.database.document_source_snapshot(run.run_id), person_id=person, meal=Meal.DINNER)
        assert source_events == expected


def test_restore_removes_meal_rows_and_restoring_forward_recovers_them(history):
    from src.desktop_v2.ledger_reader import load_revision_chain
    from src.desktop_v2.meal_history import build_meal_history_entries_by_person
    from src.services.monthly_meal_history import build_monthly_meal_history
    from src.services.person_history import build_history_tables
    session, first, second, _ = history
    def visible():
        head = session.database.latest_revision_id()
        chain = load_revision_chain(session.database, head)
        snapshot = session.database.snapshot(head)
        personal = build_meal_history_entries_by_person(chain)
        monthly = build_monthly_meal_history(snapshot, chain, 2026, 10)
        tables = build_history_tables(session.database.current_roster(), snapshot, chain)
        return personal, monthly.entries, tables['식종 변경'][1]
    assert all(visible())
    restored = restore_revision(session, first, second, ACTOR)
    personal, monthly, tables = visible()
    assert not personal and not monthly
    baseline = build_history_tables(session.database.roster_snapshot(first), session.database.snapshot(first),
                                    load_revision_chain(session.database, first))['식종 변경'][1]
    assert tables == baseline
    restore_revision(session, second, restored.revision_id, ACTOR)
    assert all(visible())
    assert session.database.revision(second).metadata.revision_id == second


def test_restored_meal_visibility_survives_branch_checkout(history):
    from src.desktop_v2.meal_history import build_meal_history_entries_by_person
    from src.services.revision_branches import create_branch, checkout_branch, MAIN_BRANCH_ID
    session, first, second, _ = history
    database = session.database
    branch = create_branch(session, second, second, ACTOR, name='식종 이력 복구 검증')
    restored = restore_revision(session, first, branch.revision_id, ACTOR)
    assert not build_meal_history_entries_by_person(database.branch_revision_chain(restored.revision_id))
    main = checkout_branch(session, MAIN_BRANCH_ID, restored.revision_id, ACTOR)
    assert build_meal_history_entries_by_person(database.branch_revision_chain(main.revision_id))
    back = checkout_branch(session, branch.branch.branch_id, main.revision_id, ACTOR)
    assert not build_meal_history_entries_by_person(database.branch_revision_chain(back.revision_id))
