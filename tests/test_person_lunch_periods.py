"""Editable lunch periods drive persistence, imports, actual service and routing."""

from dataclasses import replace
from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QApplication

from src.domain.entitlements import decide_entitlement
from src.domain.lunch_periods import build_lunch_period_revision, set_lunch_periods, suggested_person_lunch_period
from src.domain.meal_status_changes import build_meal_status_revision, build_batch_meal_status_revision
from src.domain.projections import roster_audit_changes, roster_entity_states, roster_from_historical_snapshot, status_event_state
from src.domain.revisions import EntityRef, HistoricalSnapshot, RevisionActor, RevisionMetadata, RevisionRecord, RevisionSource
from src.domain.roster import InclusiveDateRange, Meal
from src.domain.school_calendar import school_lunch_calendar_from_snapshot
from src.domain.status import MealStatus, StatusEvent, resolve_status_history
from src.domain.delivery_routing import routing_state_from_snapshot
from src.services.driver_households import build_driver_household_rows, lunch_period_texts
from src.services.driver_lunch_periods import build_driver_lunch_periods
from src.storage.encrypted_database import EncryptedDatabase
from tests.test_sqlite_delivery_report_service import _snapshot
from tests.test_driver_lunch_periods import _calendar
from tests.test_vacation_lunch_import import _roster, _preview, KEEP, OMIT, START


ACTOR = RevisionActor("tester", "lunch-period-test")
STAMP = datetime(2026, 8, 1, tzinfo=UTC)


def _edit(roster, pid, key, value):
    return build_lunch_period_revision(roster, pid, key, value, actor=ACTOR,
        recorded_at=STAMP, revision_id=uuid4(), effective_on=date(2026, 8, 1))


def _states_with(snapshot, roster):
    states = {ref: state for ref, state in snapshot.entities.items() if not ref.entity_kind.startswith("roster.")}
    states.update(roster_entity_states(roster))
    return HistoricalSnapshot(roster.revision_id, states)


def test_legacy_edit_restricts_actual_delivery_and_stop_still_wins():
    historical, (pid,) = _snapshot(meals=(Meal.LUNCH,))
    roster = roster_from_historical_snapshot(historical)
    start = _edit(roster, pid, "lunch_starts_on", date(2026, 8, 18))
    end = _edit(start.snapshot, pid, "lunch_ends_on", date(2026, 8, 21))
    snapshot = _states_with(historical, end.snapshot)
    rows = build_driver_lunch_periods(snapshot, date(2026, 8, 17), date(2026, 8, 24))
    assert [(r.starts_on, r.ends_on) for r in rows] == [(date(2026, 8, 18), date(2026, 8, 21))]
    for day, expected in ((17, False), (18, True), (21, True), (24, False)):
        assert decide_entitlement(end.snapshot, pid, Meal.LUNCH, date(2026, 8, day)).entitled is expected
    stopped = StatusEvent(uuid4(), end.snapshot.revision_id, pid, Meal.LUNCH,
        MealStatus.STOPPED, InclusiveDateRange(date(2026, 8, 19)), STAMP)
    states = dict(snapshot.entities)
    states[EntityRef("status.event", stopped.event_id)] = status_event_state(stopped)
    rows = build_driver_lunch_periods(HistoricalSnapshot(snapshot.revision_id, states), date(2026, 8, 17), date(2026, 8, 24))
    assert [(r.starts_on, r.ends_on) for r in rows] == [(date(2026, 8, 18), date(2026, 8, 18))]
    history = resolve_status_history(pid, Meal.LUNCH, (stopped,))
    assert not decide_entitlement(end.snapshot, pid, Meal.LUNCH, date(2026, 8, 20), status_history=history).entitled


def test_date_validation_clear_and_old_history_retained():
    historical, (pid,) = _snapshot(meals=(Meal.LUNCH,))
    roster = roster_from_historical_snapshot(historical)
    period = InclusiveDateRange(date(2026, 8, 17), date(2026, 8, 28))
    roster = set_lunch_periods(roster, {pid: period}, replace_from={pid: date(2026, 8, 1)})
    assert _edit(roster, pid, "lunch_starts_on", period.starts_on) is None
    with pytest.raises(ValueError, match="종료일"):
        _edit(roster, pid, "lunch_starts_on", date(2026, 8, 31))
    moved = _edit(roster, pid, "lunch_starts_on", date(2026, 8, 20)).snapshot
    assert Meal.LUNCH not in moved.meals_for(pid, on=date(2026, 8, 19))
    assert Meal.LUNCH in moved.meals_for(pid, on=date(2026, 7, 31))
    cleared = _edit(moved, pid, "lunch_starts_on", None).snapshot
    assert cleared.person(pid).lunch_period == InclusiveDateRange()
    assert Meal.LUNCH not in cleared.meals_for(pid, on=date(2026, 8, 21))
    assert Meal.LUNCH in cleared.meals_for(pid, on=date(2026, 7, 31))
    with pytest.raises(ValueError, match="시작일을 먼저"):
        _edit(cleared, pid, "lunch_ends_on", date(2026, 8, 31))


def test_school_grade_bounds_import_and_missing_person_preserve_past():
    from src.importing.reconciliation import stage_reconciliation
    from src.importing.revision import ImportPurpose, build_import_revision
    roster = _roster()
    preview = _preview("유지")
    states = dict(roster_entity_states(roster))
    _calendar(states, "기존 학교", date(2026, 8, 17), date(2026, 8, 26))
    _calendar(states, "관계없는 학교", date(2026, 8, 3), date(2026, 8, 31))
    historical = HistoricalSnapshot(roster.revision_id, states)
    prepared = build_import_revision(roster, preview,
        stage_reconciliation(roster, preview.observations, effective_on=START),
        decisions={}, actor=ACTOR, recorded_at=STAMP, revision_id=uuid4(), batch_id=uuid4(),
        purpose=ImportPurpose.VACATION_LUNCH, household_grouping_snapshot=historical)
    current = prepared.result.snapshot
    assert current.person(KEEP).lunch_period == InclusiveDateRange(date(2026, 8, 17), date(2026, 8, 26))
    assert current.person(OMIT).lunch_period == InclusiveDateRange()
    assert Meal.LUNCH in current.meals_for(OMIT, on=date(2026, 7, 31))
    assert Meal.LUNCH not in current.meals_for(OMIT, on=START)
    assert Meal.BREAKFAST in current.meals_for(OMIT, on=START)
    assert Meal.LUNCH not in current.meals_for(KEEP, on=date(2026, 8, 27))
    # A subsequent import uses its own effective date and retains prior service.
    later = _preview("유지", "제외")
    updated = build_import_revision(current, later,
        stage_reconciliation(current, later.observations, effective_on=date(2026, 8, 20)),
        decisions={}, actor=ACTOR, recorded_at=datetime(2026, 8, 20, tzinfo=UTC),
        revision_id=uuid4(), batch_id=uuid4(), purpose=ImportPurpose.VACATION_LUNCH,
        household_grouping_snapshot=_states_with(historical, current)).result.snapshot
    assert updated.person(OMIT).lunch_period.starts_on == date(2026, 8, 20)
    assert Meal.LUNCH not in updated.meals_for(OMIT, on=date(2026, 8, 19))
    assert Meal.LUNCH in updated.meals_for(KEEP, on=date(2026, 8, 18))


def test_school_grade_exception_and_missing_schedule():
    historical, (pid,) = _snapshot(meals=(Meal.LUNCH,))
    roster = roster_from_historical_snapshot(historical)
    profile = roster.person(pid).profile
    states = dict(historical.entities)
    _calendar(states, "푸른초", date(2026, 8, 19), date(2026, 8, 25), grade_exception=True)
    calendar = school_lunch_calendar_from_snapshot(HistoricalSnapshot(roster.revision_id, states))
    assert suggested_person_lunch_period(profile, calendar, date(2026, 8, 1)) == InclusiveDateRange(date(2026, 8, 19), date(2026, 8, 25))
    assert suggested_person_lunch_period(replace(profile, grade="5"), calendar, date(2026, 8, 1)) == InclusiveDateRange()
    assert suggested_person_lunch_period(profile, None, date(2026, 8, 20)) == InclusiveDateRange(date(2026, 8, 20))


def test_midseason_single_and_batch_add_use_actual_start_and_preserve_resume_bounds():
    historical, ids = _snapshot(people=2, meals=(Meal.BREAKFAST,))
    roster = roster_from_historical_snapshot(historical)
    states = dict(historical.entities)
    _calendar(states, "푸른초", date(2026, 8, 3), date(2026, 8, 28))
    calendar = school_lunch_calendar_from_snapshot(HistoricalSnapshot(roster.revision_id, states))
    args = dict(actor=ACTOR, recorded_at=STAMP, revision_id=uuid4(), school_calendar=calendar)
    prepared = build_meal_status_revision(roster, ids[0], (Meal.LUNCH,), MealStatus.DELIVERY,
        InclusiveDateRange(date(2026, 8, 19)), event_ids={Meal.LUNCH: uuid4()}, **args)
    assert prepared.snapshot.person(ids[0]).lunch_period == InclusiveDateRange(date(2026, 8, 19), date(2026, 8, 28))
    assert Meal.LUNCH not in prepared.snapshot.meals_for(ids[0], on=date(2026, 8, 18))
    assert Meal.BREAKFAST in prepared.snapshot.meals_for(ids[0], on=date(2026, 8, 18))
    # A delivery exception within an existing period must not shorten or extend it.
    resumed = build_meal_status_revision(prepared.snapshot, ids[0], (Meal.LUNCH,), MealStatus.DELIVERY,
        InclusiveDateRange(date(2026, 8, 24), date(2026, 8, 25)),
        event_ids={Meal.LUNCH: uuid4()}, **{**args, "revision_id": uuid4()})
    assert resumed.snapshot.person(ids[0]).lunch_period == prepared.snapshot.person(ids[0]).lunch_period
    assert resumed.snapshot.enrollments == prepared.snapshot.enrollments
    targets = tuple((pid, Meal.LUNCH) for pid in ids)
    batch = build_batch_meal_status_revision(roster, targets, MealStatus.DELIVERY,
        InclusiveDateRange(date(2026, 8, 19)), event_ids={target: uuid4() for target in targets}, **args)
    assert all(p.lunch_period == prepared.snapshot.person(ids[0]).lunch_period for p in batch.snapshot.persons)


def test_general_import_cannot_reopen_managed_lunch_outside_period():
    from src.importing.application import apply_reconciliation
    from src.importing.reconciliation import stage_reconciliation
    roster = set_lunch_periods(_roster(), {KEEP: InclusiveDateRange(date(2026, 8, 17), date(2026, 8, 26))},
        replace_from={KEEP: date(2026, 8, 1)})
    preview = _preview("유지")
    result = apply_reconciliation(roster,
        stage_reconciliation(roster, preview.observations, effective_on=date(2026, 9, 1)), new_revision_id=uuid4())
    assert result.snapshot.person(KEEP).lunch_period == roster.person(KEEP).lunch_period
    assert Meal.LUNCH not in result.snapshot.meals_for(KEEP, on=date(2026, 9, 1))
    observations = tuple(replace(item, meals=(Meal.BREAKFAST,)) for item in preview.observations)
    removed = apply_reconciliation(roster,
        stage_reconciliation(roster, observations, effective_on=date(2026, 8, 20)), new_revision_id=uuid4()).snapshot
    assert removed.person(KEEP).lunch_period == InclusiveDateRange()
    assert Meal.LUNCH in removed.meals_for(KEEP, on=date(2026, 8, 19))
    assert removed.meals_for(KEEP, on=date(2026, 8, 20)) == (Meal.BREAKFAST,)


def test_period_roundtrip_and_history_preserve_legacy_states(tmp_path):
    historical, (pid,) = _snapshot(meals=(Meal.LUNCH,))
    roster = roster_from_historical_snapshot(historical)
    initial = RevisionRecord(RevisionMetadata(roster.revision_id, None, STAMP, date(2026, 8, 1),
        ACTOR, "합성 명단", RevisionSource.UI), roster_audit_changes(None, roster))
    start = _edit(roster, pid, "lunch_starts_on", date(2026, 8, 18))
    end = _edit(start.snapshot, pid, "lunch_ends_on", date(2026, 8, 28))
    path = tmp_path / "lunch.db"
    with EncryptedDatabase.create(path, b"k" * 32) as database:
        for record in (initial, start.record, end.record):
            database.append_revision(record)
        assert database.current_roster().person(pid).lunch_period == end.snapshot.person(pid).lunch_period
        assert "lunch_period" not in database.snapshot(roster.revision_id).entity("roster.person", pid)
        assert roster_from_historical_snapshot(database.snapshot(start.snapshot.revision_id)).person(pid).lunch_period.ends_on is None
        database.check_integrity()
    with EncryptedDatabase.open(path, b"k" * 32) as database:
        assert database.current_roster().person(pid).lunch_period == end.snapshot.person(pid).lunch_period


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


def test_household_dates_and_one_toggle_control_both_columns_and_exports(app, tmp_path):
    from src.desktop_v2.driver_households import DriverHouseholdsDialog
    historical, ids = _snapshot(people=2, meals=(Meal.LUNCH,))
    roster = roster_from_historical_snapshot(historical)
    roster = set_lunch_periods(roster, {pid: InclusiveDateRange(date(2026, 8, 17 + i), date(2026, 8, 28)) for i, pid in enumerate(ids)},
        replace_from={pid: date(2026, 8, 1) for pid in ids})
    routing = routing_state_from_snapshot(historical)
    rows = build_driver_household_rows(roster, routing, date(2026, 8, 20))
    assert len(rows) == 1
    start_text, end_text = lunch_period_texts(rows[0])
    assert set(start_text.splitlines()) == {"아동1: 2026-08-17", "아동2: 2026-08-18"}
    assert set(end_text.splitlines()) == {"아동1: 2026-08-28", "아동2: 2026-08-28"}
    settings = QSettings(str(tmp_path / "view.ini"), QSettings.Format.IniFormat)
    def dialog():
        return DriverHouseholdsDialog(rows, service_day=date(2026, 8, 20), drivers=routing.drivers,
            settings=settings, settings_key="lunch-test")
    first = dialog()
    first.show()
    app.processEvents()
    assert first.current_view_snapshot().headers[-2:] == ("중식 시작일", "중식 종료일")
    first.lunch_period_checkbox.click()
    assert all(first.table.isColumnHidden(i) for i in (6, 7))
    first._set_column_search("members", "아동")
    first._set_column_search("members", "")
    first.close()
    restored = dialog()
    assert not restored.lunch_period_checkbox.isChecked()
    assert "중식 시작일" not in restored.current_view_snapshot().headers
    exports = []
    restored.export_requested.connect(exports.append)
    restored.template_export_button.click()
    assert "중식 종료일" not in exports[-1].sheets[0].headers
    restored.lunch_period_checkbox.click()
    assert all(not restored.table.isColumnHidden(i) for i in (6, 7))
    restored.template_export_button.click()
    assert exports[-1].sheets[0].rows[0][-2:] == lunch_period_texts(rows[0])
    restored.close()


def test_basic_columns_are_editable_even_without_custom_fields(app):
    from src.desktop_v2.roster_window import RosterTableModel
    from src.desktop_v2.roster_cell_edit import is_roster_cell_editable
    model = RosterTableModel(_roster())
    keys = [column.key for column in model.columns]
    index = keys.index("status:lunch")
    assert keys[index + 1:index + 3] == ["lunch_starts_on", "lunch_ends_on"]
    for key in keys[index + 1:index + 3]:
        assert is_roster_cell_editable(key)


def test_direct_date_edits_stay_personal_with_shared_lunch_status():
    from src.domain.household_shared import HOUSEHOLD_SHARED_KIND, HOUSEHOLD_SHARED_ID, expand_household_shared_revision
    historical, ids = _snapshot(people=2, meals=(Meal.LUNCH,))
    roster = roster_from_historical_snapshot(historical)
    states = dict(historical.entities)
    states[EntityRef(HOUSEHOLD_SHARED_KIND, HOUSEHOLD_SHARED_ID)] = {"columns": ["status:lunch"]}
    edited = _edit(roster, ids[0], "lunch_starts_on", date(2026, 8, 18))
    expanded = expand_household_shared_revision(HistoricalSnapshot(roster.revision_id, states), edited.record)
    assert expanded == edited.record
    assert edited.snapshot.person(ids[1]).lunch_period is None
    assert Meal.LUNCH in edited.snapshot.meals_for(ids[1], on=date(2026, 8, 17))


def test_shared_delivery_resume_cannot_extend_either_persons_lunch_period():
    from src.domain.household_shared import HOUSEHOLD_SHARED_KIND, HOUSEHOLD_SHARED_ID, expand_household_shared_revision
    historical, ids = _snapshot(people=2, meals=(Meal.LUNCH,))
    roster = roster_from_historical_snapshot(historical)
    roster = set_lunch_periods(roster,
        {pid: InclusiveDateRange(date(2026, 8, 17+i), date(2026, 8, 25+i)) for i, pid in enumerate(ids)},
        replace_from={pid: date(2026, 8, 1) for pid in ids})
    states = dict(_states_with(historical, roster).entities)
    states[EntityRef(HOUSEHOLD_SHARED_KIND, HOUSEHOLD_SHARED_ID)] = {"columns": ["status:lunch"]}
    prepared = build_meal_status_revision(roster, ids[0], (Meal.LUNCH,), MealStatus.DELIVERY,
        InclusiveDateRange(date(2026, 8, 20)), actor=ACTOR, recorded_at=STAMP,
        revision_id=uuid4(), event_ids={Meal.LUNCH: uuid4()})
    expanded = expand_household_shared_revision(HistoricalSnapshot(roster.revision_id, states), prepared.record)
    for change in expanded.changes:
        ref = EntityRef(change.entity_kind, change.entity_id)
        if change.after is None:
            states.pop(ref, None)
        else:
            states[ref] = change.after
    updated = roster_from_historical_snapshot(HistoricalSnapshot(expanded.metadata.revision_id, states))
    assert set(updated.enrollments) == set(roster.enrollments)
    assert all(updated.person(pid).lunch_period == roster.person(pid).lunch_period for pid in ids)


def test_cancelling_new_lunch_restores_dates_and_enrollment_together():
    from src.domain.meal_history_cancellation import MealHistoryTarget, build_meal_history_cancellation
    historical, (pid,) = _snapshot(meals=(Meal.BREAKFAST,))
    roster = roster_from_historical_snapshot(historical)
    initial = RevisionRecord(RevisionMetadata(roster.revision_id, None, STAMP, date(2026, 8, 1),
        ACTOR, "합성 명단", RevisionSource.UI), roster_audit_changes(None, roster))
    prepared = build_meal_status_revision(roster, pid, (Meal.LUNCH,), MealStatus.DELIVERY,
        InclusiveDateRange(date(2026, 8, 19)), actor=ACTOR, recorded_at=STAMP,
        revision_id=uuid4(), event_ids={Meal.LUNCH: uuid4()},
        school_calendar=school_lunch_calendar_from_snapshot(historical))
    states = dict(_states_with(historical, prepared.snapshot).entities)
    event = prepared.events[0]
    states[EntityRef("status.event", event.event_id)] = status_event_state(event)
    restored, _ = build_meal_history_cancellation(HistoricalSnapshot(prepared.snapshot.revision_id, states),
        (initial, prepared.record), MealHistoryTarget(prepared.snapshot.revision_id, "status.event", event.event_id),
        actor=ACTOR, recorded_at=STAMP, revision_id=uuid4())
    assert restored.person(pid).lunch_period is None
    assert restored.meals_for(pid, on=date(2026, 8, 19)) == (Meal.BREAKFAST,)


def test_cell_controller_saves_valid_dates_and_rejects_invalid_input(app, tmp_path):
    from src.desktop_v2.roster_cell_edit import RosterCellEditController, RosterCellEditStatus
    from src.storage.database_session import DatabaseSession
    roster = _roster()
    initial = RevisionRecord(RevisionMetadata(roster.revision_id, None, STAMP, START,
        ACTOR, "합성 명단", RevisionSource.UI), roster_audit_changes(None, roster))
    errors = []
    class Messages:
        @staticmethod
        def critical(*args):
            errors.append(args[-1])
    with DatabaseSession.create(tmp_path / "controller.db", "test-password", "test-recovery",
            backup_directory=tmp_path / "backup") as session:
        session.append_revision(initial)
        controller = RosterCellEditController(session, message_box=Messages,
            actor_provider=lambda: ACTOR, recorded_at_provider=lambda: STAMP, effective_date_provider=lambda: START)
        assert controller.save(KEEP, "lunch_starts_on", "2026-08-18").completed
        assert controller.save(KEEP, "lunch_ends_on", "2026-08-28").completed
        saved = session.database.current_roster()
        assert controller.save(KEEP, "lunch_ends_on", "2026-08-28").status is RosterCellEditStatus.CANCELLED
        assert controller.save(KEEP, "lunch_ends_on", "2026-08-17").status is RosterCellEditStatus.FAILED
        assert controller.save(KEEP, "lunch_ends_on", "2026-02-30").status is RosterCellEditStatus.FAILED
        assert session.database.current_roster().revision_id == saved.revision_id
        assert len(errors) == 2
        assert controller.save(KEEP, "lunch_starts_on", "").completed
        assert session.database.current_roster().person(KEEP).lunch_period == InclusiveDateRange()
