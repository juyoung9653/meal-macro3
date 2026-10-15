"""Coverage for the all-meal roster summary in the date sidebar."""

import os
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from uuid import UUID

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from src.desktop_v2.roster_window import (
    RosterTableModel,
    RosterWindow,
)
from src.domain.roster import (
    DeliveryAssignment,
    InclusiveDateRange,
    Meal,
    MealEnrollment,
    Person,
    PersonProfile,
    RosterSnapshot,
)
from src.domain.status import (
    MealStatus,
    StatusEvent,
    resolve_status_history,
)
from src.domain.delivery_routing import Driver, RoutingEvent, RoutingState
from src.domain.entitlements import LunchDayRule, SchoolLunchMark
from src.domain.school_calendar import (
    SchoolLunchCalendar,
    SchoolLunchDay,
    SchoolLunchSchoolSchedule,
)


DAY = date(2026, 8, 21)
REVISION_ID = UUID("90000000-0000-0000-0000-000000000001")
FIRST_ID = UUID("90000000-0000-0000-0000-000000000002")
SECOND_ID = UUID("90000000-0000-0000-0000-000000000003")
DRIVER_ID = UUID("90000000-0000-0000-0000-000000000004")


@pytest.fixture(scope="module")
def app():
    application = QApplication.instance() or QApplication([])
    yield application


def _roster() -> RosterSnapshot:
    return RosterSnapshot(
        REVISION_ID,
        persons=(
            Person(FIRST_ID, PersonProfile("첫째", school_name="샘플초")),
            Person(SECOND_ID, PersonProfile("둘째")),
        ),
        enrollments=(
            *(MealEnrollment(FIRST_ID, meal) for meal in Meal),
            MealEnrollment(SECOND_ID, Meal.BREAKFAST),
        ),
        assignments=(
            DeliveryAssignment(FIRST_ID, Meal.DINNER, "저녁", "1"),
            DeliveryAssignment(FIRST_ID, Meal.BREAKFAST, "아침", "2"),
            DeliveryAssignment(FIRST_ID, Meal.LUNCH, "점심", "3"),
        ),
    )


def _histories():
    events = (
        StatusEvent(
            UUID("90000000-0000-0000-0000-000000000011"), REVISION_ID, FIRST_ID,
            Meal.DINNER, MealStatus.STOPPED, InclusiveDateRange(DAY, DAY),
            datetime(2026, 8, 1, tzinfo=UTC), "저녁 미배송",
        ),
        StatusEvent(
            UUID("90000000-0000-0000-0000-000000000012"), REVISION_ID, FIRST_ID,
            Meal.BREAKFAST, MealStatus.PAUSED, InclusiveDateRange(DAY, DAY),
            datetime(2026, 8, 1, tzinfo=UTC), "아침 중지",
        ),
    )
    return {
        (event.person_id, event.meal): resolve_status_history(
            event.person_id, event.meal, (event,)
        )
        for event in events
    }


def _calendar() -> SchoolLunchCalendar:
    covered = InclusiveDateRange(DAY, DAY)
    return SchoolLunchCalendar((
        SchoolLunchSchoolSchedule(
            "샘플초", (covered,), (
                SchoolLunchDay(DAY, LunchDayRule(SchoolLunchMark.VENDOR_REQUIRED)),
            ),
        ),
    ))


def test_meal_summary_uses_school_calendar_only_for_lunch_status(app):
    model = RosterTableModel(_roster(), _histories())
    model.set_context(DAY, Meal.LUNCH)

    counts = model.meal_status_counts()
    assert counts[Meal.DINNER] == {
        MealStatus.DELIVERY.value: 0,
        MealStatus.PAUSED.value: 0,
        MealStatus.STOPPED.value: 1,
    }
    assert counts[Meal.BREAKFAST] == {
        MealStatus.DELIVERY.value: 1,
        MealStatus.PAUSED.value: 1,
        MealStatus.STOPPED.value: 0,
    }
    assert counts[Meal.LUNCH] == {
        MealStatus.DELIVERY.value: 0,
        MealStatus.PAUSED.value: 0,
        MealStatus.STOPPED.value: 0,
    }

    model.set_school_calendar(_calendar())
    assert model.meal_status_counts()[Meal.LUNCH]["배송"] == 1
    sequence_column = next(
        index for index, column in enumerate(model.columns) if column.key == "sequence"
    )
    assert model.data(model.index(0, sequence_column)) == "석식: 1\n조식: 2\n중식: 3"
    model.set_search("첫째")
    assert model.meal_status_counts()[Meal.BREAKFAST]["배송"] == 0
    model.set_context(DAY + timedelta(days=1), Meal.LUNCH)
    assert model.meal_status_counts()[Meal.DINNER]["배송"] == 1
    assert model.meal_status_counts()[Meal.BREAKFAST]["배송"] == 1


def test_meal_summary_sidebar_shows_all_meals_and_driver_filter_matches_any_meal(app):
    routing = RoutingState(
        drivers=(Driver(DRIVER_ID, "조식 기사"),),
        events=(RoutingEvent(
            UUID("90000000-0000-0000-0000-000000000020"), FIRST_ID, DAY, 1,
            {Meal.BREAKFAST: DRIVER_ID}, None, None,
        ),),
    )
    model = RosterTableModel(_roster(), _histories(), routing_state=routing)
    model.set_context(DAY, Meal.LUNCH)
    model.set_driver_filter(DRIVER_ID)
    assert model.rowCount() == 1
    assert model.person_at(0).person_id == FIRST_ID

    window = RosterWindow(_roster(), _histories(), school_calendar=_calendar())
    window._set_day(DAY)
    window.show()
    app.processEvents()
    assert not hasattr(window, "_meal_buttons")
    assert tuple(window._meal_summary_widgets) == (Meal.DINNER, Meal.BREAKFAST, Meal.LUNCH)
    assert "석식  배송 0 · 일시중지 0" == window._meal_summary_widgets[Meal.DINNER].text()
    assert "조식  배송 1 · 일시중지 1" == window._meal_summary_widgets[Meal.BREAKFAST].text()
    assert "중식  배송 1 · 일시중지 0" == window._meal_summary_widgets[Meal.LUNCH].text()
    assert window._meal_summary_widgets[Meal.DINNER].isHidden()
    assert window._meal_summary_widgets[Meal.BREAKFAST].isVisible()
    assert window._meal_summary_widgets[Meal.LUNCH].isVisible()
    assert "비대상은 집계에서 제외" in window._meal_summary_widgets[Meal.LUNCH].toolTip()
    assert window._birth_month_filter.geometry().top() > max(
        summary.geometry().bottom()
        for summary in window._meal_summary_widgets.values()
        if summary.isVisible()
    )
    window.replace_roster(_roster(), {})
    assert window._meal_summary_widgets[Meal.DINNER].isHidden()
    friday_dinner = replace(_roster(), delivery_defaults=tuple(
        replace(setting, weekdays=(0, 1, 2, 3, 4))
        if setting.meal is Meal.DINNER else setting
        for setting in _roster().delivery_defaults
    ))
    window.replace_roster(friday_dinner, {})
    assert window._meal_summary_widgets[Meal.DINNER].isVisible()
    window.replace_roster(_roster(), _histories())
    assert window._meal_summary_widgets[Meal.DINNER].isHidden()
    assert window._count_metric.geometry().bottom() < window.height()
    window.close()


def test_sidebar_only_shows_default_delivery_days_without_changing_columns(app):
    window = RosterWindow(_roster(), {}, school_calendar=_calendar())
    window.show()
    try:
        for day, visible in (
            (DAY - timedelta(days=1), {Meal.DINNER}),
            (DAY, {Meal.BREAKFAST, Meal.LUNCH}),
            (DAY + timedelta(days=1), set()),
            (DAY + timedelta(days=2), set()),
            (DAY + timedelta(days=3), {Meal.DINNER}),
        ):
            window._set_day(day)
            app.processEvents()
            assert {
                meal for meal, widget in window._meal_summary_widgets.items()
                if widget.isVisible()
            } == visible
            for meal in (Meal.DINNER, Meal.BREAKFAST):
                column = next(
                    index for index, item in enumerate(window.table_model.columns)
                    if item.meal is meal
                )
                assert window.table_model.index(0, column).data().endswith("\n배송")
    finally:
        window.close()


def test_sidebar_refreshes_default_delivery_mapping_and_filtered_counts(app):
    roster = _roster()
    window = RosterWindow(roster, {}, school_calendar=_calendar())
    window._set_day(DAY)
    window.show()
    try:
        assert window._meal_summary_widgets[Meal.BREAKFAST].isVisible()
        changed = replace(roster, delivery_defaults=tuple(
            replace(setting, delivery_weekdays=(0, 1, 2, 3, 3, 3, 3))
            if setting.meal is Meal.BREAKFAST else setting
            for setting in roster.delivery_defaults
        ))
        window.replace_roster(changed, {})
        assert window._meal_summary_widgets[Meal.BREAKFAST].isHidden()
        window._set_day(DAY - timedelta(days=1))
        assert window._meal_summary_widgets[Meal.BREAKFAST].isVisible()
        window._search_input.setText("일치하지않는성명")
        assert window._meal_summary_widgets[Meal.BREAKFAST].isHidden()
        window._search_input.clear()
        assert window._meal_summary_widgets[Meal.BREAKFAST].isVisible()
        window.replace_roster(roster, {})
        assert window._meal_summary_widgets[Meal.BREAKFAST].isHidden()
    finally:
        window.close()
