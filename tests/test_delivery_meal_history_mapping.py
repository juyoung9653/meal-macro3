"""Meal-change notes in delivery placeholders, using synthetic snapshots only."""
from dataclasses import replace
from datetime import timedelta

import openpyxl
import pytest

from src.desktop_v2.delivery_placeholder_mapping import DeliveryPlaceholderMapping
from src.domain.document_templates import (
    DOCUMENT_TEMPLATE_SETTING_KIND, DOCUMENT_TEMPLATE_SETTING_ID,
    DocumentTemplateSetting, DocumentTemplateProfile, document_template_setting_state,
)
from src.domain.projections import roster_entity_states
from src.domain.revisions import EntityRef, HistoricalSnapshot
from src.domain.roster import Meal, MealEnrollment, DeliveryAssignment
from src.domain.status import MealStatus
from src.excel.mapped_delivery_template import inspect_delivery_form
from src.generators.substitute_generator import build_substitute_workbook
from src.services.sqlite_delivery_substitute_service import prepare_delivery_substitute_data
from tests.test_delivery_report_remarks import source, snapshot, prepare, memo, MONDAY
from tests.test_delivery_substitute_v2 import app, options


def happy(original):
    entities = dict(original.entities)
    entities[EntityRef(DOCUMENT_TEMPLATE_SETTING_KIND, DOCUMENT_TEMPLATE_SETTING_ID)] = (
        document_template_setting_state(DocumentTemplateSetting(
            1, DocumentTemplateProfile.HAPPY_LUNCHBOX, original.revision_id)))
    return HistoricalSnapshot(original.revision_id, entities)


@pytest.mark.parametrize("starts,statuses,offset,expected", [
    ({1: MONDAY}, (), 0, "신규 8/17~"),
    ({1: MONDAY}, (), 3, "신규 8/17~"),
    ({1: MONDAY}, (), 7, ""),
    ({}, ((1, Meal.DINNER, MealStatus.PAUSED, MONDAY, MONDAY + timedelta(days=2)),),
     0, "일시중지 8/17~8/19"),
    ({}, ((1, Meal.DINNER, MealStatus.PAUSED, MONDAY, MONDAY),), 1, "복원"),
    ({}, ((1, Meal.DINNER, MealStatus.STOPPED, MONDAY, None),
          (1, Meal.DINNER, MealStatus.DELIVERY, MONDAY + timedelta(days=1), None)),
     1, "배송 8/18~"),
    ({}, ((1, Meal.DINNER, MealStatus.PAUSED, MONDAY, MONDAY + timedelta(days=3)),
          (1, Meal.DINNER, MealStatus.DELIVERY, MONDAY, None)),
     0, "배송 8/17~"),
])
def test_history_matches_school_daily_report(starts, statuses, offset, expected):
    original, roster, _ = source(starts=starts, statuses=statuses)
    day = MONDAY + timedelta(days=offset)
    data = prepare_delivery_substitute_data(happy(original), day, Meal.DINNER)
    values = {row.person_id: row.values for row in data.rows}
    assert values[roster.persons[1].person_id]["meal_history"] == (
        "석식: " + expected if expected else "")
    daily_memo = memo(prepare(original, day))
    if expected:
        assert expected + "\n정자동(아동1)" in daily_memo
    else:
        assert daily_memo == ""
    assert len(data.rows) == 2
    assert all(row.values["meal_receives"] == "1" for row in data.rows)


def test_stopped_selected_meal_history_on_other_meal_row_and_source_isolation():
    original, roster, events = source(statuses=(
        (1, Meal.DINNER, MealStatus.STOPPED, MONDAY, None),
    ))
    roster = replace(roster,
        enrollments=roster.enrollments + tuple(
            MealEnrollment(p.person_id, Meal.BREAKFAST, roster.enrollments[0].period)
            for p in roster.persons),
        assignments=roster.assignments + tuple(
            DeliveryAssignment(p.person_id, Meal.BREAKFAST, "김기사", str(i))
            for i, p in enumerate(roster.persons)))
    original = snapshot(roster, events)
    current = happy(original)
    combined = prepare_delivery_substitute_data(current, MONDAY, Meal.DINNER, include_breakfast=True)
    row = next(row for row in combined.rows if row.person_id == roster.persons[1].person_id)
    assert row.values["meal_history"] == "석식: 중지 8/17~"
    assert row.values["meal_receives"] == ""
    assert row.values["receives:breakfast"] == "1"
    single = prepare_delivery_substitute_data(current, MONDAY, Meal.DINNER)
    assert len(single.rows) == 1
    previous = HistoricalSnapshot(original.revision_id, roster_entity_states(roster))
    restored = prepare_delivery_substitute_data(previous, MONDAY, Meal.DINNER, mapping_snapshot=current)
    assert all(row.values["meal_history"] == "" for row in restored.rows)


@pytest.mark.parametrize("secondary", [False, True])
def test_mapping_choices_saved_priorities_and_excel_output(app, tmp_path, secondary):
    original, roster, _ = source(starts={1: MONDAY})
    persons = tuple(replace(p, profile=replace(p.profile, note="명단 비고" if i == 0 else ""))
                    for i, p in enumerate(roster.persons))
    roster = replace(roster, persons=persons)
    original = HistoricalSnapshot(original.revision_id, roster_entity_states(roster))
    data = prepare_delivery_substitute_data(happy(original), MONDAY, Meal.DINNER)
    path = tmp_path / "template.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["{성명}", "{비고}", "{식종 변경 이력}"])
    ws["B1"].alignment = openpyxl.styles.Alignment(wrap_text=True)
    ws.row_dimensions[1].height = 45
    wb.save(path)
    wb.close()
    form = inspect_delivery_form(path)
    mapping = {"비고": "note" if secondary else "meal_history"}
    fallbacks = {"비고": "meal_history" if secondary else "note"}
    widget = DeliveryPlaceholderMapping(form, data.columns, {
        "template_mapping": mapping, "template_fallbacks": fallbacks})
    for combo in (widget.combos["비고"], widget.fallback_combos["비고"]):
        assert combo.itemText(combo.findData("meal_history")) == "식종 변경 이력"
    chosen = options(data, template_mapping=widget.mapping(),
                     template_fallbacks=widget.fallbacks(), template_rows=widget.rows())
    result = build_substitute_workbook(data, chosen, template_path=path)
    output = tmp_path / "result.xlsx"
    result.save(output)
    result.close()
    with_output = openpyxl.load_workbook(output)
    assert list(with_output.active.values) == [
        ("아동0", "명단 비고", None),
        ("아동1", "석식: 신규 8/17~", "석식: 신규 8/17~"),
    ]
    assert with_output.active["B2"].alignment.wrap_text
    assert with_output.active.row_dimensions[2].height == 45
    with_output.close()
    widget.close()


def test_combined_meals_keep_separate_history_lines():
    friday = MONDAY + timedelta(days=4)
    original, roster, events = source(statuses=(
        (1, Meal.DINNER, MealStatus.PAUSED, MONDAY, friday),
        (1, Meal.BREAKFAST, MealStatus.PAUSED, MONDAY, friday),
    ))
    roster = replace(roster,
        enrollments=roster.enrollments + tuple(
            MealEnrollment(p.person_id, Meal.BREAKFAST, roster.enrollments[0].period)
            for p in roster.persons),
        assignments=roster.assignments + tuple(
            DeliveryAssignment(p.person_id, Meal.BREAKFAST, "김기사", str(i))
            for i, p in enumerate(roster.persons)))
    data = prepare_delivery_substitute_data(
        happy(snapshot(roster, events)), friday, Meal.DINNER, include_breakfast=True)
    row = next(row for row in data.rows if row.person_id == roster.persons[1].person_id)
    assert row.values["meal_history"] == "석식: 일시중지 8/17~8/21\n조식: 일시중지 8/17~8/21"
    assert row.values["meal_receives"] == row.values["receives:breakfast"] == "1"
