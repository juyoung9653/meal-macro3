from dataclasses import replace
from datetime import date, timedelta

import pytest

from src.domain.document_column_sources import AUTO_HISTORY
from src.domain.projections import roster_entity_states
from src.domain.revisions import HistoricalSnapshot
from src.domain.roster import Meal
from src.domain.status import MealStatus
from src.generators.happy_delivery_report import build_happy_report_workbook
from src.services.happy_delivery_report import prepare_happy_report_data
from src.services.sqlite_delivery_report_service import prepare_delivery_report_data
from tests.test_daily_report_mapping import _mapped
from tests.test_delivery_report_remarks import source, MONDAY
from tests.test_happy_delivery_report import paths
from tests.test_sqlite_delivery_report_service import _snapshot


def notes(result, profile):
    return "\n".join((row.memo if profile == "급식" else row.dinner_note) or ""
                     for report in result.reports for row in report.rows)


@pytest.mark.parametrize("profile,prepare", [("급식", prepare_delivery_report_data), ("행복도시락", prepare_happy_report_data)])
def test_history_is_default_and_can_be_replaced_or_used_as_fallback(profile, prepare):
    snapshot, roster, _ = source(statuses=((1, Meal.DINNER, MealStatus.PAUSED, MONDAY, MONDAY),))
    roster = replace(roster, persons=tuple(replace(person, profile=replace(person.profile,
        note="개인 요청" if index == 0 else "")) for index, person in enumerate(roster.persons)))
    snapshot = HistoricalSnapshot(snapshot.revision_id, {**snapshot.entities, **roster_entity_states(roster)})
    result = prepare(snapshot, MONDAY, MONDAY, (Meal.DINNER,))
    assert "일시중지 8/17" in notes(result, profile)
    assert "아동1" in notes(result, profile)  # A paused person has no shipment.
    assert "개인 요청" not in notes(result, profile)
    mapping = _mapped(snapshot, profile, {"note": "비고"}, {"note": AUTO_HISTORY})
    result = prepare(snapshot, MONDAY, MONDAY, (Meal.DINNER,), mapping_snapshot=mapping)
    assert "개인 요청" in notes(result, profile)
    assert "일시중지 8/17" in notes(result, profile)
    mapping = _mapped(snapshot, profile, {"note": ""})
    result = prepare(snapshot, MONDAY, MONDAY, (Meal.DINNER,), mapping_snapshot=mapping)
    assert notes(result, profile) == ""
    mapping = _mapped(snapshot, profile, {"note": AUTO_HISTORY}, {"note": "비고"})
    result = prepare(snapshot, MONDAY, MONDAY, (Meal.DINNER,), mapping_snapshot=mapping)
    assert "개인 요청" in notes(result, profile)
    assert "일시중지 8/17" in notes(result, profile)


def test_friday_dinner_counts_selected_day_without_week_reference():
    snapshot, _ = _snapshot(people=2, meals=(Meal.DINNER,))
    friday = date(2026, 8, 21)
    result = prepare_happy_report_data(snapshot, friday, friday, (Meal.DINNER,))
    assert len(result.shipments) == 2
    assert {item.delivery_on for item in result.shipments} == {friday}
    assert result.reports[0].delivery_date == friday
    assert result.reports[0].dinner_on == friday
    assert result.reports[0].dinner_total == 2
    workbook = build_happy_report_workbook(result.reports, paths())
    try:
        assert workbook.sheetnames == ["26.08.21"]
        assert workbook.active["E4"].value == workbook.active["E5"].value == 1
    finally:
        workbook.close()


def test_thursday_breakfast_counts_selected_day_without_week_reference():
    snapshot, _ = _snapshot(meals=(Meal.BREAKFAST,))
    thursday = date(2026, 8, 20)
    result = prepare_happy_report_data(snapshot, thursday, thursday, (Meal.BREAKFAST,))
    assert len(result.shipments) == 1
    assert result.shipments[0].delivery_on == thursday
    assert result.reports[0].breakfast_on == thursday
    assert result.reports[0].breakfast_total == 1


def test_previous_pause_does_not_move_selected_day_backwards():
    thursday = date(2026, 8, 20)
    snapshot, _, _ = source(count=1, statuses=((0, Meal.DINNER, MealStatus.PAUSED, thursday, thursday),))
    friday = thursday + timedelta(days=1)
    result = prepare_happy_report_data(snapshot, friday, friday, (Meal.DINNER,))
    assert result.reports[0].dinner_on == friday
    assert result.reports[0].dinner_total == 1
    assert len(result.shipments) == 1
    assert result.shipments[0].delivery_on == friday
    workbook = build_happy_report_workbook(result.reports, paths())
    try:
        assert workbook.sheetnames == ["26.08.21"]
        assert workbook.active["E4"].value == 1
        assert all(cell.value != 0 for row in workbook.active for cell in row)
    finally:
        workbook.close()


def test_no_previous_history_leaves_counts_blank_instead_of_zero():
    snapshot, _, _ = source(count=1, starts={0: MONDAY + timedelta(days=7)})
    result = prepare_happy_report_data(snapshot, MONDAY, MONDAY, (Meal.DINNER,))
    assert result.reports[0].dinner_on is None
    workbook = build_happy_report_workbook(result.reports, paths())
    try:
        assert workbook.active["A4"].value == "합계"
        assert workbook.active["E4"].value is None
        assert all(cell.value != 0 for row in workbook.active for cell in row)
    finally:
        workbook.close()


def test_previous_week_history_is_not_used_for_an_empty_week():
    stopped_on = date(2026, 7, 1)
    snapshot, _, _ = source(count=1, statuses=((0, Meal.DINNER, MealStatus.STOPPED, stopped_on, None),))
    friday = date(2026, 8, 21)
    result = prepare_happy_report_data(snapshot, friday, friday, (Meal.DINNER,))
    assert result.reports[0].dinner_on is None
    assert result.reports[0].dinner_total == 0
    assert result.shipments == ()
