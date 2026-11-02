"""Same-week references must not change actual deliveries or cross week boundaries."""
from dataclasses import replace
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from src.domain.projections import roster_entity_states, roster_from_historical_snapshot
from src.domain.revisions import HistoricalSnapshot
from src.domain.roster import InclusiveDateRange, Meal
from src.domain.status import MealStatus
from src.generators.happy_delivery_report import build_happy_report_workbook
from src.services.happy_delivery_report import prepare_happy_report_data
from src.services.happy_report_dates import reference_delivery_days
from tests.test_delivery_report_remarks import source, MONDAY
from tests.test_happy_delivery_report import paths
from tests.test_sqlite_delivery_report_service import _snapshot


@pytest.mark.parametrize("meal", tuple(Meal))
def test_reference_priority_and_week_boundaries_for_every_meal(meal):
    # This week crosses a year boundary: Monday 12/28 through Sunday 1/3.
    monday = date(2026, 12, 28)
    deliveries = [
        SimpleNamespace(meal=meal, delivery_on=monday + timedelta(days=offset),
                        entitlement_count=count)
        for offset, count in ((-3, 9), (1, 1), (2, 0), (3, 2), (7, 9))
    ]
    days = reference_delivery_days(deliveries, monday, monday + timedelta(days=6), (meal,))
    assert [days[monday + timedelta(days=i), meal] for i in range(7)] == [
        monday + timedelta(days=i) for i in (1, 1, 1, 3, 3, 3, 3)
    ]
    outside = [item for item in deliveries if item.entitlement_count == 9]
    assert all(day is None for day in
               reference_delivery_days(outside, monday, monday + timedelta(days=6), (meal,)).values())


def test_november_third_breakfast_uses_november_sixth_with_current_week_quantity():
    original, _ = _snapshot(people=2, meals=(Meal.BREAKFAST,))
    roster = roster_from_historical_snapshot(original)
    roster = replace(roster, enrollments=(
        roster.enrollments[0],
        replace(roster.enrollments[1], period=InclusiveDateRange(date(2026, 11, 2), None)),
    ))
    historical = HistoricalSnapshot(original.revision_id, {
        **original.entities, **roster_entity_states(roster),
    })
    result = prepare_happy_report_data(historical, date(2026, 11, 3), date(2026, 11, 3),
                                      (Meal.BREAKFAST,))
    report = result.reports[0]
    assert report.breakfast_on == date(2026, 11, 6)
    assert report.breakfast_total == 6  # Previous week had only one person / three meals.
    assert result.shipments == ()
    assert result.calculation_manifest["display_reference_days"][0]["breakfast"] == "2026-11-06"


@pytest.mark.parametrize("meal", (Meal.DINNER, Meal.LUNCH))
def test_first_day_without_meals_uses_next_delivery_in_same_week(meal):
    original, _ = _snapshot(meals=(meal,))
    roster = roster_from_historical_snapshot(original)
    roster = replace(roster, enrollments=tuple(
        replace(item, period=InclusiveDateRange(MONDAY + timedelta(days=1), None))
        for item in roster.enrollments
    ))
    historical = HistoricalSnapshot(original.revision_id, {
        **original.entities, **roster_entity_states(roster),
    })
    result = prepare_happy_report_data(historical, MONDAY, MONDAY, (meal,))
    report = result.reports[0]
    field = "dinner" if meal is Meal.DINNER else "lunch"
    assert getattr(report, field + "_on") == MONDAY + timedelta(days=1)
    assert getattr(report, field + "_total") == 1
    assert result.shipments == ()
    assert result.calculation_manifest["display_reference_days"][0][field] == "2026-08-18"


def test_weekend_lunch_reuses_friday_and_keeps_lunch_workbook():
    original, _ = _snapshot(meals=(Meal.LUNCH,))
    sunday = MONDAY + timedelta(days=6)
    result = prepare_happy_report_data(original, sunday, sunday, (Meal.LUNCH,))
    assert result.reports[0].lunch_on == MONDAY + timedelta(days=4)
    assert result.reports[0].lunch_total == 1
    assert result.shipments == ()
    workbook = build_happy_report_workbook(result.reports, paths())
    try:
        assert workbook.sheetnames == ["중식_26.08.23"]
        assert workbook.active["D4"].value == 1
    finally:
        workbook.close()


@pytest.mark.parametrize("meal", tuple(Meal))
def test_whole_week_without_meals_leaves_blank_sheet_despite_previous_week(meal):
    original, _ = _snapshot(meals=(meal,))
    roster = roster_from_historical_snapshot(original)
    roster = replace(roster, enrollments=tuple(
        replace(item, period=InclusiveDateRange(date(2026, 1, 1), MONDAY - timedelta(days=1)))
        for item in roster.enrollments
    ))
    historical = HistoricalSnapshot(original.revision_id, {
        **original.entities, **roster_entity_states(roster),
    })
    result = prepare_happy_report_data(historical, MONDAY, MONDAY, (meal,))
    report = result.reports[0]
    assert (report.breakfast_on, report.dinner_on, report.lunch_on) == (None, None, None)
    assert (report.breakfast_total, report.dinner_total, report.lunch_total) == (0, 0, 0)
    assert result.shipments == ()
    workbook = build_happy_report_workbook(result.reports, paths())
    try:
        assert len(workbook.worksheets) == 1
        assert workbook.active["A4"].value == "합계"
        assert all(cell.value != 0 for row in workbook.active for cell in row)
    finally:
        workbook.close()


def test_multiple_weeks_use_their_own_friday_quantities_and_only_actual_shipments():
    original, _ = _snapshot(people=2, meals=(Meal.BREAKFAST,))
    roster = roster_from_historical_snapshot(original)
    roster = replace(roster, enrollments=(
        roster.enrollments[0],
        replace(roster.enrollments[1], period=InclusiveDateRange(date(2026, 11, 9), None)),
    ))
    historical = HistoricalSnapshot(original.revision_id, {
        **original.entities, **roster_entity_states(roster),
    })
    result = prepare_happy_report_data(historical, date(2026, 11, 3), date(2026, 11, 10),
                                      (Meal.BREAKFAST,))
    assert [(report.breakfast_on, report.breakfast_total) for report in result.reports] == (
        [(date(2026, 11, 6), 3)] * 6 + [(date(2026, 11, 13), 6)] * 2
    )
    assert len(result.shipments) == 1
    assert result.shipments[0].delivery_on == date(2026, 11, 6)


def test_delivery_form_filter_applies_before_choosing_reference():
    original, _, _ = source(forms={0: "BOX", 1: "일반"},
        statuses=((0, Meal.DINNER, MealStatus.PAUSED, MONDAY, MONDAY + timedelta(days=1)),))
    result = prepare_happy_report_data(original, MONDAY, MONDAY, (Meal.DINNER,),
                                      delivery_forms=("BOX",))
    assert result.reports[0].dinner_on == MONDAY + timedelta(days=2)
    assert result.reports[0].dinner_total == 1
    assert result.shipments == ()
