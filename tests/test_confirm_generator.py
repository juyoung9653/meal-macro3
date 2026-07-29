"""준비된 주차 데이터를 사용하는 확인서 생성기 테스트."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import openpyxl
import pytest

from src.generators.confirm_generator import make_confirm
from src.templates.template_scanner import scan_placeholders


def _weekly_counts(
    *, breakfast: int = 0, lunch: int = 0, dinner: int = 0
) -> list[dict]:
    ranges = [
        ("20260601", "20260606"),
        ("20260607", "20260613"),
        ("20260614", "20260620"),
        ("20260621", "20260627"),
        ("20260628", "20260630"),
    ]
    return [
        {
            "week": week,
            "start_date": start,
            "end_date": end,
            "조식": breakfast if week == 1 else 0,
            "중식": lunch if week == 1 else 0,
            "석식": dinner if week == 1 else 0,
        }
        for week, (start, end) in enumerate(ranges, start=1)
    ]


@pytest.fixture
def confirm_template() -> str:
    path = "assets/급식_확인서템플릿.xlsx"
    if not os.path.exists(path):
        pytest.skip("급식 확인서 템플릿 없음")
    return path


def test_renders_existing_template_from_prepared_weekly_data_without_reader_calls(
    tmp_path: Path, confirm_template: str
):
    people = [
        {
            "ID": "P001",
            "성명": "홍길동",
            "세대주\n성명": "홍보호",
            "핸드폰": "010-1111-1111",
            "관할동": "정자동",
            "도로명주소": "성남시 분당구 같은집",
            "_note": "견과류 알레르기",
            "배송담당": "기사A",
            "weekly_meal_counts": _weekly_counts(breakfast=1, lunch=2),
        },
        {
            "ID": "P002",
            "성명": "김영희",
            "세대주\n성명": "김보호",
            "핸드폰": "010-2222-2222",
            "관할동": "정자동",
            "도로명주소": "성남시 분당구 같은집",
            "_note": "",
            "배송담당": "기사B",
            "weekly_meal_counts": _weekly_counts(dinner=3),
        },
    ]

    with patch(
        "src.excel.db_reader.get_db_reader",
        side_effect=AssertionError("확인서 생성기는 reader를 호출하면 안 됩니다."),
    ):
        created = make_confirm(
            roster_data=people,
            col_mapping={},
            output_dir=str(tmp_path),
            template_path=confirm_template,
            year=2026,
            month=6,
            meal_type="급식",
        )

    assert created == [
        {
            "sheet_name": "정자동",
            "total_hh": 1,
            "count_1": 0,
            "count_2": 1,
            "count_3": 0,
            "count_4": 0,
            "count_5": 0,
            "file": "확인서_202606.xlsx",
        }
    ]

    output = tmp_path / "확인서_202606.xlsx"
    wb = openpyxl.load_workbook(output, data_only=False)
    try:
        assert wb.sheetnames == ["정자동"]
        ws = wb["정자동"]
        assert ws["K3"].value == "기사A / 기사B"
        assert ws["G6"].value == 1
        assert ws["H6"].value is None
        assert ws["I6"].value == 2
        assert ws["G11"].value is None
        assert ws["H11"].value == 3
        assert ws["I11"].value is None
    finally:
        wb.close()


def test_none_roster_values_render_as_blanks_with_safe_sheet_fallback(
    tmp_path: Path, confirm_template: str
):
    person = {
        "ID": "P001",
        "성명": None,
        "세대주\n성명": None,
        "핸드폰": None,
        "관할동": None,
        "도로명주소": None,
        "_note": None,
        "배송담당": None,
        "weekly_meal_counts": _weekly_counts(lunch=1),
    }

    created = make_confirm(
        roster_data=[person],
        col_mapping={},
        output_dir=str(tmp_path),
        template_path=confirm_template,
        year=2026,
        month=6,
        meal_type="급식",
    )

    assert created[0]["sheet_name"] == "기타"
    wb = openpyxl.load_workbook(tmp_path / created[0]["file"], data_only=False)
    try:
        ws = wb["기타"]
        for coordinate in ("C3", "F3", "K3", "B6", "C6", "D6"):
            assert ws[coordinate].value is None
        assert all(
            "None" not in cell.value
            for row in ws.iter_rows()
            for cell in row
            if isinstance(cell.value, str)
        )
    finally:
        wb.close()


def test_happy_lunch_delivery_phone_placeholder_is_explicitly_blank(tmp_path: Path):
    template = "assets/행복도시락_확인서템플릿.xlsx"
    if not os.path.exists(template):
        pytest.skip("행복도시락 확인서 템플릿 없음")

    person = {
        "ID": "P001",
        "성명": "홍길동",
        "세대주\n성명": "홍보호",
        "핸드폰": "010-1111-1111",
        "관할동": "정자동",
        "도로명주소": "성남시 분당구 1",
        "_note": "",
        "배송담당": "기사A",
        "weekly_meal_counts": _weekly_counts(lunch=1),
    }

    with patch(
        "src.excel.db_reader.get_db_reader",
        side_effect=AssertionError("reader 호출 금지"),
    ):
        created = make_confirm(
            roster_data=[person],
            col_mapping={},
            output_dir=str(tmp_path),
            template_path=template,
            year=2026,
            month=6,
            meal_type="행복도시락",
        )

    placeholder = scan_placeholders(template, ["1명"])["1명"]["d_phone"][0]
    wb = openpyxl.load_workbook(tmp_path / created[0]["file"], data_only=False)
    try:
        ws = wb[created[0]["sheet_name"]]
        assert ws.cell(*placeholder).value is None
        assert ws["K3"].value == "기사A"
    finally:
        wb.close()
