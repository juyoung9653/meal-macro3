"""Lunch ranges follow actual eligible deliveries, not all school calendars."""

from dataclasses import replace
from datetime import UTC, date, datetime
from uuid import uuid4

import pytest

from src.domain.delivery_routing import DRIVER_KIND
from src.domain.entitlements import LunchDayRule, SchoolLunchMark
from src.domain.projections import roster_entity_states, roster_from_historical_snapshot, status_event_state
from src.domain.revisions import EntityRef, HistoricalSnapshot
from src.domain.roster import InclusiveDateRange, Meal
from src.domain.school_calendar import SCHOOL_LUNCH_SCHEDULE_KIND, SchoolLunchDay, SchoolLunchSchoolSchedule, school_lunch_calendar_from_snapshot, school_lunch_school_schedule_state
from src.domain.status import MealStatus, StatusEvent
from src.services.driver_lunch_periods import build_driver_lunch_periods
from tests.test_sqlite_delivery_report_service import _snapshot, _weekdays, _with_routing


def _calendar(states, name, start, end, *, grade_exception=False):
    schedule = SchoolLunchSchoolSchedule(name, (InclusiveDateRange(date(2026, 8, 1), date(2026, 8, 31)),), tuple(
        SchoolLunchDay(day, LunchDayRule(
            SchoolLunchMark.SCHOOL_PROVIDES if grade_exception or not start <= day <= end else SchoolLunchMark.VENDOR_REQUIRED,
            frozenset({'6'}) if grade_exception and start <= day <= end else frozenset(),
        )) for day in _weekdays(date(2026, 8, 1), date(2026, 8, 31))))
    states[EntityRef(SCHOOL_LUNCH_SCHEDULE_KIND, schedule.entity_id)] = school_lunch_school_schedule_state(schedule)


def test_unrelated_schools_do_not_extend_driver_dates_and_grade_exceptions_apply():
    snapshot, ids = _snapshot(people=2, meals=(Meal.LUNCH,))
    roster = roster_from_historical_snapshot(snapshot)
    roster = replace(roster, persons=(roster.persons[0], replace(roster.persons[1], profile=replace(roster.persons[1].profile, school_name='다른초'))))
    states = dict(snapshot.entities)
    states.update(roster_entity_states(roster))
    _calendar(states, '푸른초', date(2026, 8, 17), date(2026, 8, 20))
    _calendar(states, '다른초', date(2026, 8, 19), date(2026, 8, 25), grade_exception=True)
    _calendar(states, '담당하지 않는 학교', date(2026, 8, 3), date(2026, 8, 31))
    snapshot = HistoricalSnapshot(snapshot.revision_id, states)
    rows = build_driver_lunch_periods(snapshot, date(2026, 8, 1), date(2026, 8, 31))
    assert [(r.driver_name, r.starts_on, r.ends_on) for r in rows] == [('김기사', date(2026, 8, 17), date(2026, 8, 25))]


def test_dated_driver_change_and_paused_dates_and_requested_range():
    snapshot, ids = _snapshot(meals=(Meal.LUNCH,))
    snapshot = _with_routing(snapshot, (ids[0], Meal.LUNCH, date(2026, 8, 19), '새 기사'))
    event = StatusEvent(uuid4(), snapshot.revision_id, ids[0], Meal.LUNCH, MealStatus.PAUSED,
                        InclusiveDateRange(date(2026, 8, 21), date(2026, 8, 21)), datetime(2026, 8, 1, tzinfo=UTC))
    states = dict(snapshot.entities)
    states[EntityRef('status.event', event.event_id)] = status_event_state(event)
    snapshot = HistoricalSnapshot(snapshot.revision_id, states)
    rows = build_driver_lunch_periods(snapshot, date(2026, 8, 17), date(2026, 8, 23))
    assert {r.driver_name: (r.starts_on, r.ends_on) for r in rows} == {
        '김기사': (date(2026, 8, 17), date(2026, 8, 18)),
        '새 기사': (date(2026, 8, 19), date(2026, 8, 20)),
    }


def test_enrollment_limits_and_unassigned_driver_are_preserved():
    snapshot, _ = _snapshot(meals=(Meal.LUNCH,), include_driver=False)
    roster = roster_from_historical_snapshot(snapshot)
    roster = replace(roster, enrollments=tuple(replace(e, period=InclusiveDateRange(date(2026, 8, 18), date(2026, 8, 19))) for e in roster.enrollments))
    states = dict(snapshot.entities)
    states.update(roster_entity_states(roster))
    rows = build_driver_lunch_periods(HistoricalSnapshot(snapshot.revision_id, states), date(2026, 8, 1), date(2026, 8, 31))
    assert [(r.driver_name, r.starts_on, r.ends_on) for r in rows] == [('미지정', date(2026, 8, 18), date(2026, 8, 19))]


def test_missing_calendar_does_not_invent_dates_for_registered_drivers():
    snapshot, _ = _snapshot(meals=(Meal.LUNCH,))
    states = {ref: state for ref, state in snapshot.entities.items() if ref.entity_kind != SCHOOL_LUNCH_SCHEDULE_KIND}
    driver_id = uuid4()
    states[EntityRef(DRIVER_KIND, driver_id)] = {'name': '김기사', 'telephone': ''}
    rows = build_driver_lunch_periods(HistoricalSnapshot(snapshot.revision_id, states), date(2026, 8, 1), date(2026, 8, 31))
    assert len(rows) == 1 and rows[0].driver_id == driver_id
    assert rows[0].starts_on is None and rows[0].ends_on is None


def test_same_named_registered_drivers_remain_distinct():
    snapshot, ids = _snapshot(people=2, meals=(Meal.LUNCH,))
    snapshot = _with_routing(snapshot, (ids[0], Meal.LUNCH, date(2026, 8, 1), '동명 기사'),
                            (ids[1], Meal.LUNCH, date(2026, 8, 1), '동명 기사'))
    rows = build_driver_lunch_periods(snapshot, date(2026, 8, 17), date(2026, 8, 21))
    assert len(rows) == 2 and rows[0].driver_id != rows[1].driver_id
    assert all(r.starts_on == date(2026, 8, 17) and r.ends_on == date(2026, 8, 21) for r in rows)


def test_plain_workbook_contains_only_driver_and_dates(tmp_path):
    from openpyxl import load_workbook
    from src.desktop_v2.current_sheet_export import build_current_sheet_workbook
    from src.desktop_v2.driver_lunch_export import lunch_period_snapshot
    snapshot, _ = _snapshot(meals=(Meal.LUNCH,))
    rows = build_driver_lunch_periods(snapshot, date(2026, 8, 17), date(2026, 8, 21))
    exported = lunch_period_snapshot(rows, date(2026, 8, 17), date(2026, 8, 21))
    workbook = build_current_sheet_workbook(exported)
    path = tmp_path / 'period.xlsx'
    workbook.save(path)
    workbook.close()
    book = load_workbook(path)
    assert list(book.active.values) == [('배송기사', '중식 시작일', '중식 종료일'), ('김기사', '2026-08-17', '2026-08-21')]
    assert book.active.page_setup.fitToWidth is None
    assert not book.active.print_area
    book.close()


@pytest.fixture
def app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_export_button_keeps_registry_open_and_unsaved_input(app):
    from src.desktop_v2.delivery_routing import DriverRegistryDialog
    from src.domain.delivery_routing import RoutingState
    dialog = DriverRegistryDialog(RoutingState())
    dialog.name.setText('입력 중 기사')
    requested = []
    dialog.lunch_period_export_requested.connect(lambda: requested.append(True))
    dialog.show()
    dialog.lunch_export_button.click()
    assert requested == [True] and dialog.isVisible()
    assert dialog.name.text() == '입력 중 기사'
    dialog.close()


def test_range_validation_and_cancel(app):
    from PySide6.QtWidgets import QDialog
    from src.desktop_v2.driver_lunch_export import DriverLunchPeriodDialog
    dialog = DriverLunchPeriodDialog(date(2026, 8, 21), date(2026, 8, 17))
    dialog.export_button.click()
    assert dialog.result() != QDialog.DialogCode.Accepted
    assert dialog.error.text()
    dialog.reject()
    assert dialog.result() == QDialog.DialogCode.Rejected


def test_suggested_range_uses_nearest_reviewed_season():
    from src.desktop_v2.driver_lunch_export import suggested_lunch_period
    snapshot, _ = _snapshot(meals=(Meal.LUNCH,))
    calendar = school_lunch_calendar_from_snapshot(snapshot)
    assert suggested_lunch_period(calendar, date(2026, 8, 15)) == (date(2026, 8, 1), date(2026, 9, 30))


def test_suggested_range_separates_summer_and_winter_across_years():
    from types import SimpleNamespace
    from src.desktop_v2.driver_lunch_export import suggested_lunch_period
    calendar = SimpleNamespace(schedules=(SimpleNamespace(covered_periods=(
        InclusiveDateRange(date(2026, 7, 20), date(2026, 8, 20)),
        InclusiveDateRange(date(2026, 12, 24), date(2027, 2, 28)),
    )), SimpleNamespace(covered_periods=(
        InclusiveDateRange(date(2026, 12, 28), date(2027, 3, 2)),
    ))))
    assert suggested_lunch_period(calendar, date(2027, 1, 1)) == (date(2026, 12, 24), date(2027, 3, 2))


@pytest.mark.parametrize('accepted', [False, True])
def test_application_export_workflow_cancel_or_freeze_latest_data(app, monkeypatch, accepted):
    from types import SimpleNamespace
    from PySide6.QtWidgets import QDialog
    import src.desktop_v2.application as application
    snapshot, _ = _snapshot(meals=(Meal.LUNCH,))
    roster = roster_from_historical_snapshot(snapshot)
    reads, exported = [], []
    database = SimpleNamespace(current_roster=lambda: reads.append(True) or roster,
                               snapshot=lambda revision: snapshot)
    controller = SimpleNamespace(
        _message_box=SimpleNamespace(critical=lambda *a: pytest.fail(str(a))),
        _run_current_sheet_export=lambda *a, **k: exported.append(a[2]),
    )
    window = SimpleNamespace(table_model=SimpleNamespace(service_day=date(2026, 8, 17)))
    monkeypatch.setattr(application, 'run_progress_task', lambda task, **kwargs: task())
    steps = application.DesktopStartupController._run_driver_lunch_export.steps(
        controller, SimpleNamespace(database=database), window)
    # No QWidget parent is required for this controller test.
    from src.desktop_v2 import driver_lunch_export
    original_dialog = driver_lunch_export.DriverLunchPeriodDialog
    monkeypatch.setattr(driver_lunch_export, 'DriverLunchPeriodDialog', lambda start, end, parent: original_dialog(start, end))
    request = next(steps)
    assert request.dialog.windowTitle() == '중식 기간 Excel 내보내기'
    with pytest.raises(StopIteration):
        steps.send(QDialog.DialogCode.Accepted if accepted else QDialog.DialogCode.Rejected)
    assert len(exported) == int(accepted)
    assert len(reads) == 1 + int(accepted)
    if accepted:
        assert exported[0].sheets[0].headers == ('배송기사', '중식 시작일', '중식 종료일')
    request.dialog.close()
