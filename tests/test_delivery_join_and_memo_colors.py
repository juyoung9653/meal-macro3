"""Regression coverage for joined areas and generated memo heading colors."""
from copy import copy
from dataclasses import replace

import openpyxl
import pytest
from openpyxl.cell.rich_text import TextBlock
from openpyxl.styles import Font

from src.domain.daily_report_choices import ReportRemarkOptions
from src.domain.roster import Meal
from src.excel.mapped_delivery_template import inspect_delivery_form
from src.excel.report_memo import write_report_memo
from src.generators.substitute_generator import build_substitute_workbook
from src.services.sqlite_delivery_substitute_service import prepare_delivery_substitute_data
from tests.test_delivery_substitute_v2 import source, DAY, options, app


@pytest.mark.parametrize("fallback", [False, True])
def test_join_mapping_any_placeholder_in_header_and_body(tmp_path, fallback):
    snapshot, _ = source(sizes=(1, 1, 1, 1))
    data = prepare_delivery_substitute_data(snapshot, DAY, Meal.BREAKFAST)
    rows = tuple(replace(row, values={**row.values, "jurisdiction": area})
                 for row, area in zip(data.rows, (" 나동 ", "가동", "나동", " ")))
    foreign = replace(rows[0], driver_key="other", values={**rows[0].values, "jurisdiction": "제외동"})
    data = replace(data, rows=(*rows, foreign))
    template = tmp_path / "form.xlsx"
    book = openpyxl.Workbook()
    sheet = book.active
    sheet["A1"], sheet["B1"] = "{기사구역}", "{관할동}"
    sheet.append(["이름", "개인 동", "모든 동"])
    sheet.append(["{성명}", "{관할동}", "{기사구역}"])
    book.save(template)
    book.close()
    kwargs = ({"template_mapping": {"기사구역": ""}, "template_fallbacks": {"기사구역": "jurisdiction_join"}}
              if fallback else {"template_mapping": {"기사구역": "jurisdiction_join"}})
    result = build_substitute_workbook(data, options(data, **kwargs), template_path=template)
    output = tmp_path / "joined.xlsx"
    result.save(output)
    result.close()
    saved = openpyxl.load_workbook(output)
    try:
        assert saved.active["A1"].value == "나동, 가동"
        assert [saved.active.cell(row, 3).value for row in range(3, 7)] == ["나동, 가동"] * 4
        assert [saved.active.cell(row, 2).value for row in range(3, 7)] == [" 나동 ", "가동", "나동", None]
    finally:
        saved.close()


def test_join_option_is_selectable_and_keeps_saved_choices(app):
    from src.desktop_v2.delivery_placeholder_mapping import DeliveryPlaceholderMapping
    snapshot, _ = source()
    data = prepare_delivery_substitute_data(snapshot, DAY, Meal.BREAKFAST)
    form = inspect_delivery_form("assets/행복도시락/배송목록템플릿.xlsx")
    panel = DeliveryPlaceholderMapping(form, data.columns)
    try:
        combo = panel.combos["관할동 조인"]
        assert combo.currentText() == "관할동 합치기"
        assert combo.currentData() == "jurisdiction_join"
        assert panel.fallback_combos["관할동 조인"].findData("jurisdiction_join") >= 0
    finally:
        panel.close()
    panel = DeliveryPlaceholderMapping(form, data.columns, {"template_mapping": {"관할동 조인": ""}})
    try:
        assert panel.mapping()["관할동 조인"] == ""
    finally:
        panel.close()


def red_text(value):
    return "".join(block.text for block in value if isinstance(block, TextBlock)
                   and block.font.color.rgb == "FFFF0000")


@pytest.mark.parametrize("profile", ["school", "happy_dinner", "happy_lunch"])
@pytest.mark.parametrize("position", ["above", "below"])
def test_real_report_extra_headings_are_red_and_names_keep_template_color(tmp_path, profile, position):
    from tests.test_daily_report_extra_remarks import source_with_notes
    from tests.test_delivery_report_forms import DAY as report_day
    from src.services.sqlite_delivery_report_service import prepare_delivery_report_data
    from src.services.happy_delivery_report import prepare_happy_report_data
    from src.generators.report_generator import make_report_publication
    from src.generators.happy_delivery_report import build_happy_report_workbook
    from tests.test_happy_delivery_report import paths
    from src.domain.projections import roster_from_historical_snapshot, roster_entity_states
    from src.domain.revisions import HistoricalSnapshot
    snapshot, first = source_with_notes()
    roster = roster_from_historical_snapshot(snapshot)
    people = tuple(replace(person, profile=replace(person.profile, note="여러 줄\n추가 안내"))
                   for person in roster.persons)
    roster = replace(roster, persons=people)
    meal = Meal.LUNCH if profile == "happy_lunch" else Meal.DINNER
    if meal is Meal.LUNCH:
        roster = replace(roster,
            enrollments=tuple(replace(item, meal=meal) for item in roster.enrollments),
            households=tuple(replace(household, delivery_settings=tuple(replace(item, meal=meal)
                for item in household.delivery_settings)) for household in roster.households),
            persons=tuple(replace(person, delivery_settings=tuple(replace(item, meal=meal)
                for item in person.delivery_settings) if person.delivery_settings is not None else None)
                for person in roster.persons))
    snapshot = HistoricalSnapshot(snapshot.revision_id, roster_entity_states(roster))
    choices = ReportRemarkOptions((first, "note"), position, ("식재료 박스", "도시락"))
    output = tmp_path / "colors.xlsx"
    if profile == "school":
        prepared = prepare_delivery_report_data(snapshot, report_day, report_day, (meal,), remark_options=choices)
        make_report_publication(prepared.reports, output, "assets/급식/일일식수템플릿.xlsx")
        coordinate = "H4"
    else:
        prepared = prepare_happy_report_data(snapshot, report_day, report_day, (meal,), remark_options=choices)
        book = build_happy_report_workbook(prepared.reports, paths())
        book.save(output)
        book.close()
        coordinate = "I4"
    book = openpyxl.load_workbook(output, rich_text=True)
    try:
        value = book.active[coordinate].value
        colored = red_text(value)
        for heading in ("0", "여러 줄", "추가 안내", "식재료 박스 별도", "도시락 별도"):
            assert heading in colored
        assert "아동" in str(value) and "아동" not in colored
        assert "가구·" not in colored
        assert all(block.font.sz == book.active[coordinate].font.sz for block in value if isinstance(block, TextBlock))
    finally:
        book.close()


def test_all_status_headings_keep_red_and_plain_memo_does_not_change(tmp_path):
    book = openpyxl.Workbook()
    cell = book.active["A1"]
    cell.font = Font(name="Arial", size=14, italic=True, color="FF123456")
    original = copy(cell.font)
    headings = ("신규 1/1~", "배송 1/2~", "중지 1/3~", "일시중지 1/4~1/5", "복원")
    text = "일반 안내\n" + "\n".join(heading + "\n정자동(합성아동)" for heading in headings)
    write_report_memo(cell, text)
    assert copy(cell.font) == original
    assert str(cell.value) == text
    assert all(heading in red_text(cell.value) for heading in headings)
    assert "일반 안내" not in red_text(cell.value)
    assert "합성아동" not in red_text(cell.value)
    for block in cell.value:
        if isinstance(block, TextBlock):
            assert block.font.i and block.font.rFont == "Arial" and block.font.sz == 14
    book.close()
