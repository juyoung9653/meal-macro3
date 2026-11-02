"""Status changes persist a complete route and its audit in one transaction."""

from dataclasses import replace
from datetime import UTC, date, datetime
from uuid import uuid4

import pytest

from src.domain.delivery_routing import (
    DRIVER_KIND, ROUTING_EVENT_KIND, Driver, RoutingEvent, _driver_state, _routing_event_state,
)
from src.domain.meal_status_changes import build_batch_meal_status_revision, build_meal_status_revision
from src.domain.projections import roster_audit_changes, status_event_audit_change
from src.domain.revisions import AuditChange, ChangeOperation, RevisionActor, RevisionMetadata, RevisionRecord, RevisionSource
from src.domain.roster import (
    DeliveryAssignment, Household, HouseholdMembership, InclusiveDateRange,
    Meal, MealEnrollment, Person, PersonProfile, RosterSnapshot,
)
from src.domain.status import MealStatus, StatusEvent
from src.storage.database_session import DatabaseSession


DAY = date(2026, 11, 2)
NOW = datetime(2026, 11, 2, tzinfo=UTC)
ACTOR = RevisionActor("합성 담당", "sequence-test")


def seed(session, *, family=False, common=False, routing=False):
    people = tuple(Person(uuid4(), PersonProfile(name)) for name in ("가", "나", "다", "중지자", "다른 기사"))
    households = tuple(Household(uuid4()) for _ in people)
    roster = RosterSnapshot(
        uuid4(), persons=people, households=households,
        memberships=tuple(HouseholdMembership(
            person.person_id, households[0 if family and index == 1 else index].household_id,
        ) for index, person in enumerate(people)),
        enrollments=tuple(MealEnrollment(person.person_id, meal) for person in people for meal in Meal),
        assignments=tuple(
            DeliveryAssignment(person.person_id, None if common and index == 1 else Meal.DINNER,
                               driver="다른 기사" if index == 4 else "기사", sequence=sequence)
            for index, (person, sequence) in enumerate(zip(people, ("001", "002", "010", "003", "088")))
        ) + tuple(DeliveryAssignment(person.person_id, Meal.LUNCH, driver="기사", sequence="077") for person in people),
    )
    stop = StatusEvent(uuid4(), roster.revision_id, people[3].person_id, Meal.DINNER,
                       MealStatus.STOPPED, InclusiveDateRange(date(2026, 10, 1)), NOW)
    changes = [*roster_audit_changes(RosterSnapshot(uuid4()), roster), status_event_audit_change(stop)]
    if routing:
        driver = Driver(uuid4(), "예약 기사")
        changes.append(AuditChange(DRIVER_KIND, driver.driver_id, ChangeOperation.INSERT,
                                   None, _driver_state(driver)))
        for index in (1, 4):
            event = RoutingEvent(uuid4(), people[index].person_id, date(2026, 11, 3),
                                 index + 1, {Meal.DINNER: driver.driver_id})
            changes.append(AuditChange(ROUTING_EVENT_KIND, event.event_id, ChangeOperation.INSERT,
                                       None, _routing_event_state(event)))
    session.append_revision(RevisionRecord(
        RevisionMetadata(roster.revision_id, None, NOW, None, ACTOR, "합성 명단", RevisionSource.IMPORT),
        tuple(changes),
    ))
    return session.database.current_roster(), tuple(person.person_id for person in people)


def change(session, targets, status, *, day=DAY, batch=False, periods=None):
    roster = session.database.current_roster()
    period = InclusiveDateRange(day, day if status is MealStatus.PAUSED else None)
    kwargs = dict(actor=ACTOR, recorded_at=NOW, revision_id=uuid4(), periods_by_meal=periods)
    if batch:
        prepared = build_batch_meal_status_revision(
            roster, targets, status, period, event_ids={target: uuid4() for target in targets}, **kwargs,
        )
    else:
        pid, meal = targets[0]
        prepared = build_meal_status_revision(
            roster, pid, (meal,), status, period, event_ids={meal: uuid4()}, **kwargs,
        )
    result = session.append_revision(prepared.record)
    assert result.error is None
    return session.database.current_roster()


def sequences(roster, ids, meal=Meal.DINNER):
    return tuple(roster.resolve_assignment(pid, meal).sequence for pid in ids)


@pytest.mark.parametrize("common", [False, True])
def test_stop_removes_sequence_compacts_route_and_survives_reopen(tmp_path, common):
    path = tmp_path / "stop.sqlite3"
    with DatabaseSession.create(path, "password", "recovery") as session:
        original, ids = seed(session, common=common)
        updated = change(session, ((ids[1], Meal.DINNER),), MealStatus.STOPPED)
        assert sequences(updated, ids) == ("1", None, "2", "003", "088")
        assert sequences(updated, ids, Meal.LUNCH) == ("077",) * 5
        assert updated.resolve_assignment(ids[1], Meal.DINNER).driver == "기사"
        if common:
            assert updated.resolve_assignment(ids[1], Meal.BREAKFAST).sequence == "002"
        assert session.database.latest_revision().metadata.parent_revision_id == original.revision_id
        assert session.database.status_history(ids[1], Meal.DINNER).status_on(DAY) is MealStatus.STOPPED
        # Historical snapshots and the initial imported strings are unchanged.
        from src.domain.projections import roster_from_historical_snapshot
        assert sequences(roster_from_historical_snapshot(session.database.snapshot(original.revision_id)), ids) == (
            "001", "002", "010", "003", "088",
        )
    with DatabaseSession.open(path, "password") as session:
        assert sequences(session.database.current_roster(), ids) == ("1", None, "2", "003", "088")


def test_delivery_appends_even_with_old_sequence_and_pause_preserves_order(tmp_path):
    with DatabaseSession.create(tmp_path / "resume.sqlite3", "password", "recovery") as session:
        original, ids = seed(session)
        paused = change(session, ((ids[1], Meal.DINNER),), MealStatus.PAUSED)
        assert paused.assignments == original.assignments
        delivered = change(session, ((ids[3], Meal.DINNER),), MealStatus.DELIVERY)
        assert sequences(delivered, ids) == ("1", "2", "3", "4", "088")
        resumed = change(session, ((ids[1], Meal.DINNER),), MealStatus.DELIVERY)
        assert sequences(resumed, ids) == ("1", "4", "2", "3", "088")


def test_household_keeps_one_number_and_batch_stop_compacts_once(tmp_path):
    with DatabaseSession.create(tmp_path / "family.sqlite3", "password", "recovery") as session:
        _, ids = seed(session, family=True)
        updated = change(session, ((ids[1], Meal.DINNER),), MealStatus.STOPPED)
        assert sequences(updated, ids) == ("1", None, "2", "003", "088")
        updated = change(session, ((ids[1], Meal.DINNER),), MealStatus.DELIVERY)
        assert sequences(updated, ids) == ("2", "2", "1", "003", "088")
        updated = change(session, tuple((pid, Meal.DINNER) for pid in ids[:2]), MealStatus.STOPPED, batch=True)
        assert sequences(updated, ids) == (None, None, "1", "003", "088")


def test_each_meal_uses_own_effective_date_and_dated_driver(tmp_path):
    with DatabaseSession.create(tmp_path / "dated.sqlite3", "password", "recovery") as session:
        _, ids = seed(session, routing=True)
        updated = change(session, ((ids[1], Meal.DINNER), (ids[2], Meal.BREAKFAST)),
                         MealStatus.STOPPED, batch=True, periods={
                             Meal.DINNER: InclusiveDateRange(date(2026, 11, 3)),
                             Meal.BREAKFAST: InclusiveDateRange(date(2026, 11, 6)),
                         })
        # Only the new driver's dinner route is compacted.
        assert sequences(updated, ids) == ("001", None, "010", "003", "1")
        assert updated.resolve_assignment(ids[2], Meal.BREAKFAST).sequence is None
        assert sequences(updated, ids, Meal.LUNCH) == ("077",) * 5


def test_sequence_failure_rolls_back_status_and_route(tmp_path, monkeypatch):
    import src.services.meal_status_sequence as module

    with DatabaseSession.create(tmp_path / "rollback.sqlite3", "password", "recovery") as session:
        original, ids = seed(session)
        def fail(*args, **kwargs):
            raise RuntimeError("합성 순서 저장 실패")
        monkeypatch.setattr(module, "build_scoped_sequence_revision", fail)
        with pytest.raises(RuntimeError, match="합성 순서 저장 실패"):
            change(session, ((ids[1], Meal.DINNER),), MealStatus.STOPPED)
        assert session.database.current_roster() == original
        assert not session.database.status_history(ids[1], Meal.DINNER).events


@pytest.mark.parametrize("share_sequence", [False, True])
def test_shared_status_targets_are_removed_together(tmp_path, share_sequence):
    from src.domain.household_shared import HOUSEHOLD_SHARED_ID, HOUSEHOLD_SHARED_KIND

    (tmp_path / "backups").mkdir()
    with DatabaseSession.create(tmp_path / "shared.sqlite3", "password", "recovery",
                                backup_directory=tmp_path / "backups") as session:
        roster, ids = seed(session, family=True)
        session.append_revision(RevisionRecord(
            RevisionMetadata(uuid4(), roster.revision_id, NOW, DAY, ACTOR, "가구 공용", RevisionSource.UI),
            (AuditChange(HOUSEHOLD_SHARED_KIND, HOUSEHOLD_SHARED_ID, ChangeOperation.INSERT,
                         None, {"columns": ["status:dinner", "sequence"] if share_sequence else ["status:dinner"]}),),
        ))
        updated = change(session, ((ids[1], Meal.DINNER),), MealStatus.STOPPED)
        assert sequences(updated, ids) == (None, None, "1", "003", "088")
        assert session.database.status_history(ids[0], Meal.DINNER).status_on(DAY) is MealStatus.STOPPED


def test_old_status_records_replay_without_applying_new_order_policy(tmp_path):
    with DatabaseSession.create(tmp_path / "old.sqlite3", "password", "recovery") as session:
        original, ids = seed(session)
        prepared = build_meal_status_revision(
            original, ids[1], (Meal.DINNER,), MealStatus.STOPPED, InclusiveDateRange(DAY),
            actor=ACTOR, recorded_at=NOW, revision_id=uuid4(), event_ids={Meal.DINNER: uuid4()},
        )
        legacy = replace(prepared.record, metadata=replace(
            prepared.record.metadata, rule_versions={"meal_status_change_policy_version": 2},
        ))
        session.append_revision(legacy)
        assert session.database.current_roster().assignments == original.assignments


def test_new_meal_without_driver_gets_first_sequence_and_replay_is_exact(tmp_path):
    person = Person(uuid4(), PersonProfile("신규 식종"))
    roster = RosterSnapshot(uuid4(), persons=(person,))
    initial = RevisionRecord(
        RevisionMetadata(roster.revision_id, None, NOW, None, ACTOR, "합성 명단", RevisionSource.IMPORT),
        roster_audit_changes(RosterSnapshot(uuid4()), roster),
    )
    with DatabaseSession.create(tmp_path / "new.sqlite3", "password", "recovery") as session:
        session.append_revision(initial)
        updated = change(session, ((person.person_id, Meal.DINNER),), MealStatus.DELIVERY)
        assert sequences(updated, (person.person_id,)) == ("1",)
        saved = session.database.latest_revision()
    with DatabaseSession.create(tmp_path / "replay.sqlite3", "password", "recovery") as session:
        session.append_revision(initial)
        session.append_revision(replace(saved, metadata=replace(saved.metadata, display_code=None)))
        assert session.database.current_roster() == updated
        assert session.database.latest_revision().changes == saved.changes


def test_open_route_refreshes_after_status_save_and_preserves_filters(tmp_path):
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication
    from src.desktop_v2.driver_households import DriverHouseholdsDialog
    from src.desktop_v2.roster_window import RosterWindow

    app = QApplication.instance() or QApplication([])
    settings = QSettings(str(tmp_path / "ui.ini"), QSettings.IniFormat)
    with DatabaseSession.create(tmp_path / "window.sqlite3", "password", "recovery") as session:
        roster, ids = seed(session)
        histories = lambda: {(pid, meal): session.database.status_history(pid, meal) for pid in ids for meal in Meal}
        window = RosterWindow(roster, histories(), column_settings=settings, inactive_settings=settings)
        provider = lambda hidden: window.driver_household_rows(include_hidden=hidden, service_day=DAY)
        dialog = DriverHouseholdsDialog(provider(False), service_day=DAY, drivers=(), rows_provider=provider)
        window._driver_household_dialogs.append(dialog)
        try:
            dialog.driver_combo.setCurrentIndex(next(
                index for index in range(dialog.driver_combo.count())
                if tuple(dialog.driver_combo.itemData(index)) == (None, "기사")
            ))
            dialog.meal_combo.setCurrentIndex(dialog.meal_combo.findData(Meal.DINNER))
            dialog._set_column_search("members", "나")
            assert dialog.table.rowCount() == 1
            updated = change(session, ((ids[1], Meal.DINNER),), MealStatus.STOPPED)
            window.replace_roster(updated, histories())
            window.refresh_driver_household_dialogs()
            app.processEvents()
            assert dialog.table.rowCount() == 0
            assert dialog._column_searches == {"members": "나"}
            assert dialog.meal_combo.currentData() == Meal.DINNER
            assert tuple(dialog.driver_combo.currentData()) == (None, "기사")
            assert dialog.source_revision_id == updated.revision_id
        finally:
            window._driver_household_dialogs.remove(dialog)
            dialog.close()
            window.close()
