"""Encrypted SQLite and immutable revision-ledger integration tests."""

from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import UUID

import pytest

apsw = pytest.importorskip(
    "apsw",
    reason="install requirements-sqlite-v2.txt for encrypted DB integration tests",
)

from src.domain.revisions import (
    AuditChange,
    ChangeOperation,
    ImportBatch,
    RevisionActor,
    RevisionMetadata,
    RevisionRecord,
    RevisionSource,
)
from src.domain import (
    PERSON_KIND,
    CustomFieldDefinition,
    CustomFieldType,
    DeliveryAssignment,
    Household,
    HouseholdMembership,
    InclusiveDateRange,
    Meal,
    MealEnrollment,
    MealStatus,
    Person,
    PersonProfile,
    RosterSnapshot,
    StatusEvent,
    roster_audit_changes,
    status_event_audit_change,
)
from src.storage import (
    DatabaseAlreadyExistsError,
    DatabaseEncryptionUnavailableError,
    DatabaseFormatError,
    DatabaseUnlockError,
    EncryptedDatabase,
    RevisionConflictError,
)


KEY = bytes.fromhex("11" * 32)
WRONG_KEY = bytes.fromhex("22" * 32)
REVISION_ID = UUID("30000000-0000-0000-0000-000000000001")
SECOND_REVISION_ID = UUID("30000000-0000-0000-0000-000000000002")
ENTITY_ID = UUID("30000000-0000-0000-0000-000000000003")
BATCH_ID = UUID("30000000-0000-0000-0000-000000000004")
HOUSEHOLD_ID = UUID("30000000-0000-0000-0000-000000000005")
STATUS_EVENT_ID = UUID("30000000-0000-0000-0000-000000000006")
CUSTOM_FIELD_ID = UUID("30000000-0000-0000-0000-000000000007")


def _record(
    revision_id: UUID = REVISION_ID,
    parent_revision_id: UUID | None = None,
) -> RevisionRecord:
    metadata = RevisionMetadata(
        revision_id=revision_id,
        parent_revision_id=parent_revision_id,
        recorded_at=datetime(2026, 8, 24, 15, 0, tzinfo=UTC),
        effective_on=date(2026, 8, 17),
        actor=RevisionActor("tester", "S-1-5-21-test"),
        reason="조사표 확정",
        source=RevisionSource.IMPORT,
        decisions={"identity": "confirmed"},
        rule_versions={"roster": 1},
    )
    change = AuditChange(
        entity_kind="person",
        entity_id=ENTITY_ID,
        operation=ChangeOperation.INSERT,
        before=None,
        after={"name": "홍길동", "meals": ["조식", "석식"]},
    )
    batch = ImportBatch(
        batch_id=BATCH_ID,
        revision_id=revision_id,
        source_filename="survey.xlsx",
        sha256="a" * 64,
        byte_size=1234,
        sheet_name="조사표",
        header_fingerprint="header-v1",
        mapping_manifest={"S": "breakfast", "T": "dinner", "U": None},
        effective_on=date(2026, 8, 17),
        decisions={"ambiguous_flags": []},
    )
    return RevisionRecord(metadata, (change,), batch)


def _roster_snapshot(revision_id: UUID, *, name: str = "홍길동") -> RosterSnapshot:
    return RosterSnapshot(
        revision_id,
        persons=(
            Person(
                ENTITY_ID,
                PersonProfile(
                    name,
                    mobile_phone="010-1234-5678",
                    custom_values={CUSTOM_FIELD_ID: "A구역"},
                ),
            ),
        ),
        households=(Household(HOUSEHOLD_ID),),
        memberships=(HouseholdMembership(ENTITY_ID, HOUSEHOLD_ID),),
        enrollments=(MealEnrollment(ENTITY_ID, Meal.DINNER),),
        assignments=(DeliveryAssignment(ENTITY_ID, Meal.DINNER, "기사A", "001"),),
        custom_fields=(
            CustomFieldDefinition(
                CUSTOM_FIELD_ID,
                "배송 구역",
                CustomFieldType.TEXT,
                0,
            ),
        ),
    )


def _roster_record(
    snapshot: RosterSnapshot,
    *,
    before: RosterSnapshot | None = None,
    status_event: StatusEvent | None = None,
) -> RevisionRecord:
    changes = roster_audit_changes(before, snapshot)
    if status_event is not None:
        changes = (*changes, status_event_audit_change(status_event))
    return RevisionRecord(
        RevisionMetadata(
            revision_id=snapshot.revision_id,
            parent_revision_id=None if before is None else before.revision_id,
            recorded_at=datetime(2026, 8, 24, 16, 0, tzinfo=UTC),
            effective_on=date(2026, 8, 17),
            actor=RevisionActor("tester", "S-1-5-21-test"),
            reason="명단 projection 확정",
            source=RevisionSource.UI,
        ),
        changes,
    )


def _downgrade_to_schema_two_for_migration_test(path: Path) -> None:
    connection = apsw.Connection(str(path))
    try:
        connection.pragma("cipher", "chacha20")
        connection.pragma("hexkey", KEY.hex())
        connection.execute("BEGIN IMMEDIATE")
        for trigger in (
            "document_run_validate_insert",
            "document_run_no_update",
            "document_run_no_delete",
        ):
            connection.execute(f"DROP TRIGGER {trigger}")
        connection.execute("DROP TABLE document_run")
        for trigger in (
            "revision_display_code_no_insert_after_seal",
            "revision_display_code_no_update",
            "revision_display_code_no_delete",
            "revision_seal_requires_display_code",
        ):
            connection.execute(f"DROP TRIGGER {trigger}")
        connection.execute("DROP TABLE revision_display_code")
        connection.execute("DROP TRIGGER schema_migration_no_delete")
        connection.execute("DELETE FROM schema_migration WHERE version >= 3")
        connection.execute(
            """
            CREATE TRIGGER schema_migration_no_delete
            BEFORE DELETE ON schema_migration
            BEGIN
                SELECT RAISE(ABORT, 'schema migration rows are immutable');
            END
            """
        )
        connection.pragma("user_version", 2)
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def test_create_append_reopen_and_read_revision(tmp_path: Path):
    path = tmp_path / "operations.sqlite3"
    with EncryptedDatabase.create(path, KEY) as database:
        assert database.schema_version == 5
        assert database.latest_revision() is None
        database.append_revision(_record())
        database.check_integrity()

    with EncryptedDatabase.open(path, KEY) as reopened:
        stored = reopened.latest_revision()
        assert stored is not None
        assert stored.metadata.revision_id == REVISION_ID
        assert stored.metadata.display_code == "20260825-001"
        assert reopened.revision_display_code(REVISION_ID) == "20260825-001"
        assert stored.metadata.actor.sid == "S-1-5-21-test"
        assert stored.changes[0].before is None
        assert stored.changes[0].after == {
            "meals": ("조식", "석식"),
            "name": "홍길동",
        }
        assert stored.import_batch is not None
        assert stored.import_batch.source_filename == "survey.xlsx"
        assert stored.import_batch.mapping_manifest["U"] is None


def test_plain_sqlite_and_wrong_key_cannot_read_database(tmp_path: Path):
    path = tmp_path / "operations.sqlite3"
    EncryptedDatabase.create(path, KEY).close()

    with pytest.raises(DatabaseUnlockError):
        EncryptedDatabase.open(path, WRONG_KEY)

    import sqlite3

    plain = sqlite3.connect(path)
    try:
        with pytest.raises(sqlite3.DatabaseError):
            plain.execute("SELECT name FROM sqlite_schema").fetchall()
    finally:
        plain.close()


def test_revision_parent_must_match_latest_and_failure_is_atomic(tmp_path: Path):
    path = tmp_path / "operations.sqlite3"
    with EncryptedDatabase.create(path, KEY) as database:
        database.append_revision(_record())
        stale = _record(SECOND_REVISION_ID, parent_revision_id=None)

        with pytest.raises(RevisionConflictError, match="parent revision"):
            database.append_revision(stale)

        assert database.latest_revision().metadata.revision_id == REVISION_ID


def test_revision_display_code_counts_by_korean_recorded_date_not_effective_date(
    tmp_path: Path,
):
    path = tmp_path / "operations.sqlite3"
    first = _roster_snapshot(REVISION_ID)
    second = _roster_snapshot(SECOND_REVISION_ID, name="홍 길동")

    with EncryptedDatabase.create(path, KEY) as database:
        database.append_revision(_roster_record(first))
        database.append_revision(_roster_record(second, before=first))

        assert database.revision_display_code(REVISION_ID) == "20260825-001"
        assert database.revision_display_code(SECOND_REVISION_ID) == "20260825-002"
        summaries = database.revision_summaries()
        assert tuple(item.revision_id for item in summaries) == (
            SECOND_REVISION_ID,
            REVISION_ID,
        )
        assert tuple(item.display_code for item in summaries) == (
            "20260825-002",
            "20260825-001",
        )
        assert database.revision(SECOND_REVISION_ID).metadata.effective_on == date(
            2026, 8, 17
        )


def test_database_rejects_caller_assigned_revision_display_code(tmp_path: Path):
    path = tmp_path / "operations.sqlite3"
    record = _record()
    assigned = RevisionRecord(
        replace(record.metadata, display_code="20260825-999"),
        record.changes,
        record.import_batch,
    )

    with EncryptedDatabase.create(path, KEY) as database:
        with pytest.raises(ValueError, match="DB가 저장 시 자동"):
            database.append_revision(assigned)

        assert database.latest_revision() is None


def test_open_migrates_schema_two_and_backfills_stable_display_codes(tmp_path: Path):
    path = tmp_path / "operations.sqlite3"
    first = _roster_snapshot(REVISION_ID)
    second = _roster_snapshot(SECOND_REVISION_ID, name="홍 길동")
    with EncryptedDatabase.create(path, KEY) as database:
        database.append_revision(_roster_record(first))
        database.append_revision(_roster_record(second, before=first))

    _downgrade_to_schema_two_for_migration_test(path)

    with EncryptedDatabase.open(path, KEY) as migrated:
        assert migrated.schema_version == 5
        assert migrated.revision_display_code(REVISION_ID) == "20260825-001"
        assert migrated.revision_display_code(SECOND_REVISION_ID) == "20260825-002"
        migrated.check_integrity()


def test_historical_snapshot_replays_full_before_and_after_states(tmp_path: Path):
    path = tmp_path / "operations.sqlite3"
    with EncryptedDatabase.create(path, KEY) as database:
        first = _record()
        database.append_revision(first)
        original = first.changes[0].after
        second = RevisionRecord(
            RevisionMetadata(
                revision_id=SECOND_REVISION_ID,
                parent_revision_id=REVISION_ID,
                recorded_at=datetime(2026, 8, 24, 16, 0, tzinfo=UTC),
                effective_on=date(2026, 8, 19),
                actor=RevisionActor("tester", "S-1-5-21-test"),
                reason="학교명 수정",
                source=RevisionSource.UI,
            ),
            (
                AuditChange(
                    "person",
                    ENTITY_ID,
                    ChangeOperation.UPDATE,
                    before=original,
                    after={
                        "name": "홍길동",
                        "school": "샘플초",
                        "meals": ["조식", "석식"],
                    },
                ),
            ),
        )
        database.append_revision(second)

        at_first = database.snapshot(REVISION_ID)
        at_second = database.snapshot(SECOND_REVISION_ID)

        assert at_first.entity("person", ENTITY_ID) == original
        assert at_second.entity("person", ENTITY_ID)["school"] == "샘플초"


def test_mismatched_audit_before_rejects_whole_revision(tmp_path: Path):
    path = tmp_path / "operations.sqlite3"
    with EncryptedDatabase.create(path, KEY) as database:
        database.append_revision(_record())
        invalid = RevisionRecord(
            RevisionMetadata(
                revision_id=SECOND_REVISION_ID,
                parent_revision_id=REVISION_ID,
                recorded_at=datetime(2026, 8, 24, 16, 0, tzinfo=UTC),
                effective_on=date(2026, 8, 19),
                actor=RevisionActor("tester", "S-1-5-21-test"),
                reason="잘못된 이전 값",
                source=RevisionSource.UI,
            ),
            (
                AuditChange(
                    "person",
                    ENTITY_ID,
                    ChangeOperation.UPDATE,
                    before={"name": "다른 사람"},
                    after={"name": "홍길동"},
                ),
            ),
        )

        with pytest.raises(RevisionConflictError, match="audit before"):
            database.append_revision(invalid)

        assert database.latest_revision().metadata.revision_id == REVISION_ID


def test_revision_rows_are_immutable_at_database_level(tmp_path: Path):
    path = tmp_path / "operations.sqlite3"
    with EncryptedDatabase.create(path, KEY) as database:
        database.append_revision(_record())

    connection = apsw.Connection(str(path))
    try:
        connection.pragma("cipher", "chacha20")
        connection.pragma("hexkey", KEY.hex())
        with pytest.raises(apsw.ConstraintError, match="immutable"):
            connection.execute(
                "UPDATE revision SET reason = 'tampered' WHERE revision_id = ?",
                (str(REVISION_ID),),
            )
        with pytest.raises(apsw.ConstraintError, match="sealed revision"):
            connection.execute(
                """
                INSERT INTO audit_change(
                    revision_id, ordinal, entity_kind, entity_id, operation,
                    before_json, after_json
                ) VALUES (?, 2, 'person', ?, 'update', '{}', '{}')
                """,
                (str(REVISION_ID), str(ENTITY_ID)),
            )
    finally:
        connection.close()


def test_create_refuses_to_overwrite_existing_file(tmp_path: Path):
    path = tmp_path / "operations.sqlite3"
    path.write_bytes(b"existing")

    with pytest.raises(DatabaseAlreadyExistsError):
        EncryptedDatabase.create(path, KEY)

    assert path.read_bytes() == b"existing"


def test_plain_database_is_rejected_before_unlock(tmp_path: Path):
    import sqlite3

    path = tmp_path / "plain.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE plain_data(value TEXT)")
    connection.close()

    with pytest.raises(DatabaseFormatError, match="평문"):
        EncryptedDatabase.open(path, KEY)


def test_missing_encryption_runtime_fails_closed_and_leaves_no_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    path = tmp_path / "operations.sqlite3"
    monkeypatch.delattr(apsw, "mc_version")

    with pytest.raises(DatabaseEncryptionUnavailableError, match="암호화 runtime"):
        EncryptedDatabase.create(path, KEY)

    assert not path.exists()


def test_typed_roster_projection_and_historical_snapshot_stay_in_sync(tmp_path: Path):
    path = tmp_path / "operations.sqlite3"
    first = _roster_snapshot(REVISION_ID)
    second = _roster_snapshot(SECOND_REVISION_ID, name="홍 길동")
    event = StatusEvent(
        event_id=STATUS_EVENT_ID,
        revision_id=SECOND_REVISION_ID,
        person_id=ENTITY_ID,
        meal=Meal.DINNER,
        status=MealStatus.STOPPED,
        period=InclusiveDateRange(date(2026, 8, 17), None),
        recorded_at=datetime(2026, 8, 24, 16, 0, tzinfo=UTC),
        source_text="17일 중지",
    )

    with EncryptedDatabase.create(path, KEY) as database:
        database.append_revision(_roster_record(first))
        database.append_revision(
            _roster_record(second, before=first, status_event=event)
        )

        current = database.current_roster()
        historical = database.roster_snapshot(REVISION_ID)

        assert current is not None
        assert current.revision_id == SECOND_REVISION_ID
        assert current.person(ENTITY_ID).profile.name == "홍 길동"
        assert current.enrollments == second.enrollments
        assert current.assignments == second.assignments
        assert current.custom_fields == second.custom_fields
        assert historical.person(ENTITY_ID).profile.name == "홍길동"
        assert database.status_events(
            person_id=ENTITY_ID,
            meal=Meal.DINNER,
        ) == (event,)
        database.check_integrity()

    with EncryptedDatabase.open(path, KEY) as reopened:
        assert reopened.current_roster().person(ENTITY_ID).profile.name == "홍 길동"
        assert reopened.status_events() == (event,)


def test_invalid_typed_state_rolls_back_revision_and_projection(tmp_path: Path):
    path = tmp_path / "operations.sqlite3"
    invalid = RevisionRecord(
        RevisionMetadata(
            revision_id=REVISION_ID,
            parent_revision_id=None,
            recorded_at=datetime(2026, 8, 24, 16, 0, tzinfo=UTC),
            effective_on=date(2026, 8, 17),
            actor=RevisionActor("tester", "S-1-5-21-test"),
            reason="잘못된 typed state",
            source=RevisionSource.UI,
        ),
        (
            AuditChange(
                PERSON_KIND,
                ENTITY_ID,
                ChangeOperation.INSERT,
                before=None,
                after={"name": "필드 부족"},
            ),
        ),
    )

    with EncryptedDatabase.create(path, KEY) as database:
        with pytest.raises(ValueError, match="상태 필드"):
            database.append_revision(invalid)

        assert database.latest_revision() is None
        assert database.current_roster() is None


def test_projection_rows_cannot_change_outside_open_revision(tmp_path: Path):
    path = tmp_path / "operations.sqlite3"
    with EncryptedDatabase.create(path, KEY) as database:
        database.append_revision(_roster_record(_roster_snapshot(REVISION_ID)))

    connection = apsw.Connection(str(path))
    try:
        connection.pragma("cipher", "chacha20")
        connection.pragma("hexkey", KEY.hex())
        with pytest.raises(apsw.ConstraintError, match="open revision"):
            connection.execute(
                "UPDATE person_projection SET name = '변조' WHERE person_id = ?",
                (str(ENTITY_ID),),
            )
    finally:
        connection.close()
