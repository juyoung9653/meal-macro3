"""Excel 시트 목록 조회와 선택 읽기 테스트."""

import pytest
from openpyxl import Workbook

from src.services.roster_loader import FileReader, RosterParser
from src.widgets.invoice_tab import _load_lunch_file


@pytest.fixture
def multi_sheet_file(tmp_path):
    path = tmp_path / "multi.xlsx"
    workbook = Workbook()
    first = workbook.active
    first.title = "첫 시트"
    first.append(["성명", "학교명"])
    first.append(["첫사람", "첫학교"])
    second = workbook.create_sheet("선택 시트")
    second.append(["성명", "학교명"])
    second.append(["선택사람", "선택학교"])
    workbook.save(path)
    return path


def test_lists_excel_sheets_in_workbook_order(multi_sheet_file):
    assert FileReader.list_excel_sheets(str(multi_sheet_file)) == [
        "첫 시트",
        "선택 시트",
    ]


def test_reads_selected_sheet(multi_sheet_file):
    rows = FileReader.read_excel(str(multi_sheet_file), sheet_name="선택 시트")
    headers, data = RosterParser().parse(rows or [])

    assert headers == ["성명", "학교명"]
    assert data == [{"성명": "선택사람", "학교명": "선택학교"}]


def test_uses_first_sheet_when_selection_is_omitted(multi_sheet_file):
    rows = FileReader.read_excel(str(multi_sheet_file))
    _headers, data = RosterParser().parse(rows or [])
    assert data[0]["성명"] == "첫사람"


def test_rejects_missing_sheet(multi_sheet_file):
    with pytest.raises(ValueError, match="찾을 수 없습니다"):
        FileReader.read_excel(str(multi_sheet_file), sheet_name="없는 시트")


def test_lunch_loader_reads_selected_sheet(multi_sheet_file):
    loaded = _load_lunch_file(str(multi_sheet_file), sheet_name="선택 시트")
    assert loaded is not None
    headers, data = loaded
    assert headers == ["성명", "학교명"]
    assert data == [{"성명": "선택사람", "학교명": "선택학교"}]
