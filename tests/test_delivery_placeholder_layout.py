"""Delivery list padding and person-level serial numbers."""
from dataclasses import replace
from pathlib import Path

import openpyxl
import pytest
from openpyxl.styles import Border, Side

from src.domain.delivery_substitute import SubstituteOptions
from src.generators.substitute_generator import build_substitute_workbook, _build_sheet
from src.services.sqlite_delivery_substitute_service import prepare_delivery_substitute_data
from src.domain.roster import Meal
from tests.test_delivery_substitute_v2 import source, DAY, options

TEMPLATE = "assets/행복도시락/배송목록템플릿.xlsx"


@pytest.mark.parametrize("sizes", [(1,), (2, 1), (1,) * 20])
def test_default_form_ends_on_last_person_and_numbers_each_person(tmp_path, sizes):
    snapshot, _ = source(sizes=sizes)
    data = prepare_delivery_substitute_data(snapshot, DAY, Meal.BREAKFAST)
    before = Path(TEMPLATE).read_bytes()
    template = openpyxl.load_workbook(TEMPLATE)
    expected_height = template.active.row_dimensions[3].height
    template.close()
    result = build_substitute_workbook(data, options(data), template_path=TEMPLATE)
    output = tmp_path / "delivery.xlsx"
    result.save(output)
    result.close()
    reopened = openpyxl.load_workbook(output)
    ws = reopened.active
    last = sum(sizes) + 2
    assert ws.max_row == last
    assert ws.print_area == f"'기사명단'!$A$1:$H${last}"
    assert [ws.cell(row, 1).value for row in range(3, last + 1)] == list(range(1, sum(sizes) + 1))
    assert all(ws.row_dimensions[row].height == expected_height for row in range(3, last + 1))
    assert not any(row > last for row in ws.row_dimensions)
    assert all(merged.max_row <= last for merged in ws.merged_cells.ranges)
    reopened.close()
    assert Path(TEMPLATE).read_bytes() == before


def test_footer_formula_merges_and_blank_member_fields_are_preserved(tmp_path):
    path = tmp_path / "template.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["연번", "명단"])
    ws.append(["{연번}", "{성명}"])
    ws["A3"] = "{연번}"
    ws["B3"] = "{비고}"
    ws["A5"] = "하단 안내"
    ws.merge_cells("A5:B6")
    ws["C5"] = "=COUNTA(B2:B3)"
    ws["A20"].border = Border(bottom=Side(style="thin"))
    ws.row_dimensions[20].height = 40
    ws.print_area = "A1:C20"
    wb.save(path)
    wb.close()
    snapshot, _ = source(sizes=(2, 1))
    data = prepare_delivery_substitute_data(snapshot, DAY, Meal.BREAKFAST)
    chosen = options(data, template_rows=(2, 3), template_mapping={"성명": "", "비고": ""})
    result = build_substitute_workbook(data, chosen, template_path=path)
    ws = result.active
    assert [ws.cell(row, 1).value for row in range(2, 8)] == [1, 1, 2, 2, 3, 3]
    assert ws["A9"].value == "하단 안내"
    assert "A9:B10" in {str(merged) for merged in ws.merged_cells.ranges}
    assert ws["C9"].value == "=COUNTA(B2:B7)"
    assert ws.max_row == 10
    assert ws.print_area == "'Sheet'!$A$1:$C$10"
    result.close()


def test_serial_mapping_restarts_for_each_driver_and_keeps_delivery_sequence():
    snapshot, _ = source(sizes=(2, 1))
    data = prepare_delivery_substitute_data(snapshot, DAY, Meal.BREAKFAST)
    rows = (data.rows[0], replace(data.rows[1], driver_key="other"), data.rows[2])
    data = replace(data, rows=rows)
    for driver, expected in ((rows[0].driver_key, [1, 2]), ("other", [1])):
        chosen = SubstituteOptions(driver, ("name",), template_mapping={"연번": "row_number"})
        result = build_substitute_workbook(data, chosen, template_path=TEMPLATE)
        assert [result.active.cell(row, 1).value for row in range(3, 3 + len(expected))] == expected
        result.close()
    assert data.rows[0].values["sequence"] == data.rows[1].values["sequence"] == "001"


def test_legacy_serial_column_is_not_merged_for_a_household():
    snapshot, _ = source(sizes=(2, 1))
    data = prepare_delivery_substitute_data(snapshot, DAY, Meal.BREAKFAST)
    chosen = replace(options(data), columns=("row_number", "name"))
    wb, _, _ = _build_sheet(data, chosen)
    assert [wb.active.cell(row, 1).value for row in (3, 4, 5)] == [1, 2, 3]
    assert "A3:A4" not in {str(merged) for merged in wb.active.merged_cells.ranges}
    wb.close()
