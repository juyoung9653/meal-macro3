"""Regression coverage for status-event order restored from the ledger."""

from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

apsw = pytest.importorskip(
    "apsw",
    reason="install requirements-sqlite-v2.txt for encrypted DB integration tests",
)

from src.desktop_v2.application import DesktopStartupController
from src.domain.projections import (
    STATUS_EVENT_KIND,
    roster_audit_changes,
    status_event_audit_change,
    status_events_from_historical_snapshot,
)
from src.domain.revisions import (
    RevisionActor,
    RevisionMetadata,
    RevisionRecord,
    RevisionSource,
)
from src.domain.roster import (
    InclusiveDateRange,
    Meal,
    MealEnrollment,
    Person,
    PersonProfile,
    RosterSnapshot,
)
from src.domain.status import MealStatus, StatusEvent, resolve_status_history
from src.storage.encrypted_database import EncryptedDatabase


KEY = bytes.fromhex("41" * 32)
PERSON_ID = UUID("81000000-0000-0000-0000-000000000001")
ROSTER_REVISION_ID = UUID("81000000-0000-0000-0000-000000000010")
STOP_REVISION_ID = UUID("81000000-0000-0000-0000-000000000020")
RESUME_REVISION_ID = UUID("81000000-0000-0000-0000-000000000030")
STOP_EVENT_ID = UUID("81000000-0000-0000-0000-000000000900")
RESUME_EVENT_ID = UUID("81000000-0000-0000-0000-000000000100")
RECORDED_AT = datetime(2026, 10, 14, 9, 30, tzinfo=UTC)
SAME_INSTANT_KST = datetime(
    2026,
    10,
    14,
    18,
    30,
    tzinfo=timezone(timedelta(hours=9)),
)
SERVICE_DAY = date(2026, 10, 15)


def _roster() -> RosterSnapshot:
    return RosterSnapshot(
        ROSTER_REVISION_ID,
        persons=(Person(PERSON_ID, PersonProfile("원장순서")),),
        enrollments=(MealEnrollment(PERSON_ID, Meal.DINNER),),
    )


def _event(
    event_id: UUID,
    revision_id: UUID,
    status: MealStatus,
    *,
    recorded_at: datetime = RECORDED_AT,
) -> StatusEvent:
    return StatusEvent(
        event_id=event_id,
        revision_id=revision_id,
        person_id=PERSON_ID,
        meal=Meal.DINNER,
        status=status,
        period=InclusiveDateRange(SERVICE_DAY, None),
        recorded_at=recorded_at,
        source_text=status.value,
    )


def _record(
    revision_id: UUID,
    parent_revision_id: UUID | None,
    changes,
) -> RevisionRecord:
    return RevisionRecord(
        RevisionMetadata(
            revision_id=revision_id,
            parent_revision_id=parent_revision_id,
            recorded_at=RECORDED_AT,
            effective_on=SERVICE_DAY,
            actor=RevisionActor("tester", "S-1-5-21-status-order"),
            reason="동일 시각 상태 순서 검증",
            source=RevisionSource.UI,
        ),
        tuple(changes),
    )


def _create_database(path: Path, *, same_revision: bool) -> UUID:
    roster = _roster()
    with EncryptedDatabase.create(path, KEY) as database:
        if same_revision:
            stop = _event(STOP_EVENT_ID, ROSTER_REVISION_ID, MealStatus.STOPPED)
            resume = _event(RESUME_EVENT_ID, ROSTER_REVISION_ID, MealStatus.DELIVERY)
            database.append_revision(
                _record(
                    ROSTER_REVISION_ID,
                    None,
                    (
                        *roster_audit_changes(None, roster),
                        status_event_audit_change(stop),
                        status_event_audit_change(resume),
                    ),
                )
            )
            return ROSTER_REVISION_ID

        database.append_revision(
            _record(
                ROSTER_REVISION_ID,
                None,
                roster_audit_changes(None, roster),
            )
        )
        database.append_revision(
            _record(
                STOP_REVISION_ID,
                ROSTER_REVISION_ID,
                (status_event_audit_change(
                    _event(
                        STOP_EVENT_ID,
                        STOP_REVISION_ID,
                        MealStatus.STOPPED,
                        recorded_at=SAME_INSTANT_KST,
                    )
                ),),
            )
        )
        database.append_revision(
            _record(
                RESUME_REVISION_ID,
                STOP_REVISION_ID,
                (status_event_audit_change(
                    _event(RESUME_EVENT_ID, RESUME_REVISION_ID, MealStatus.DELIVERY)
                ),),
            )
        )
        return RESUME_REVISION_ID


def _stored_structure(database: EncryptedDatabase) -> tuple[tuple, tuple]:
    connection = database._require_connection()
    schema = tuple(connection.execute(
        """
        SELECT type, name, tbl_name, sql
        FROM sqlite_schema
        WHERE name NOT LIKE 'sqlite_%'
        ORDER BY type, name
        """
    ))
    ledger = tuple(connection.execute(
        """
        SELECT revision_id, ordinal, entity_kind, entity_id, operation,
               before_json, after_json
        FROM audit_change
        ORDER BY rowid
        """
    ))
    return schema, ledger


@pytest.mark.parametrize("same_revision", [True, False], ids=["one-revision", "two-revisions"])
def test_equal_time_status_events_follow_ledger_order_after_reopen(
    tmp_path: Path,
    same_revision: bool,
):
    path = tmp_path / "status-order.sqlite3"
    latest_revision_id = _create_database(path, same_revision=same_revision)
    expected_event_ids = (STOP_EVENT_ID, RESUME_EVENT_ID)

    with EncryptedDatabase.open(path, KEY) as database:
        structure_before = _stored_structure(database)
        snapshot = database.snapshot(latest_revision_id)
        snapshot_event_ids = tuple(
            reference.entity_id
            for reference in snapshot.entities
            if reference.entity_kind == STATUS_EVENT_KIND
        )
        assert snapshot_event_ids == expected_event_ids

        historical = resolve_status_history(
            PERSON_ID,
            Meal.DINNER,
            status_events_from_historical_snapshot(
                snapshot, person_id=PERSON_ID, meal=Meal.DINNER,
            ),
        )
        current = database.status_history(PERSON_ID, Meal.DINNER)
        assert current == historical
        assert tuple(event.event_id for event in current.events) == expected_event_ids
        assert current.status_on(SERVICE_DAY) is MealStatus.DELIVERY

        statements: list[str] = []

        def trace(_cursor, statement, _bindings):
            statements.append(statement)
            return True

        connection = database._require_connection()
        connection.set_exec_trace(trace)
        try:
            roster = database.current_roster()
            assert roster is not None
            histories = DesktopStartupController._histories(
                SimpleNamespace(database=database),
                roster,
            )
        finally:
            connection.set_exec_trace(None)

        batch = histories[(PERSON_ID, Meal.DINNER)]
        assert batch == historical
        assert tuple(event.event_id for event in batch.events) == expected_event_ids
        assert batch.status_on(SERVICE_DAY) is MealStatus.DELIVERY
        assert sum(
            "FROM status_event_projection" in statement
            for statement in statements
        ) == 1
        assert _stored_structure(database) == structure_before
