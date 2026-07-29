"""AnnualDeliveryBook → 청구서 Excel 통합 테스트."""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import openpyxl
import pytest

from src.core.delivery_matrix import (
    AnnualDeliveryBook,
    DeliveryCellValue,
    DeliveryRow,
    RosterEntry,
)
from src.excel.annual_workbook import AnnualWorkbook
from src.generators.confirm_generator import make_confirm
from src.services.invoice_controller import InvoiceGenerationConfig, InvoiceService
from src.templates.config import read_template_columns


@pytest.fixture(autouse=True)
def _clear_template_cache():
    read_template_columns.cache_clear()
    yield
    read_template_columns.cache_clear()


@pytest.fixture
def template_path() -> str:
    path = "assets/급식_청구서템플릿.xlsx"
    if not os.path.exists(path):
        pytest.skip("급식 청구서 템플릿 없음")
    return path


@pytest.fixture
def confirm_template_path() -> str:
    path = "assets/급식_확인서템플릿.xlsx"
    if not os.path.exists(path):
        pytest.skip("급식 확인서 템플릿 없음")
    return path



def _book() -> AnnualDeliveryBook:
    # 실제 물리 열처럼 역순이며 다른 월도 포함한다.
    dates = (
        date(2026, 7, 1),
        date(2026, 6, 4),
        date(2026, 6, 3),
        date(2026, 6, 2),
        date(2026, 6, 1),
    )
    blank = DeliveryCellValue.BLANK
    roster = (
        RosterEntry(
            "P001",
            {
                "성명": "홍길동",
                "학교명": "분당초",
                "비고": "원본 비고",
                "조식 여부": 0,
            },
        ),
        RosterEntry(
            "P002",
            {"성명": "김철수", "학교명": "분당초", "비고": ""},
        ),
    )
    rows = (
        DeliveryRow(
            "P001",
            "조식",
            "김배송",
            "003",
            {
                date(2026, 6, 1): DeliveryCellValue.DELIVERED,
                date(2026, 6, 2): blank,
                date(2026, 6, 3): DeliveryCellValue.PAUSED,
                date(2026, 6, 4): DeliveryCellValue.DELIVERED,
                date(2026, 7, 1): DeliveryCellValue.DELIVERED,
            },
        ),
        DeliveryRow(
            "P002",
            "석식",
            "이배송",
            2,
            {
                date(2026, 6, 1): DeliveryCellValue.DELIVERED,
                date(2026, 6, 2): DeliveryCellValue.DELIVERED,
                date(2026, 6, 3): DeliveryCellValue.DELIVERED,
                date(2026, 6, 4): blank,
                date(2026, 7, 1): blank,
            },
        ),
    )
    return AnnualDeliveryBook(2026, roster, rows, dates)


def _write_annual_workbook(path: Path) -> Path:
    wb = openpyxl.Workbook()
    roster = wb.worksheets[0]
    roster.title = "명단"
    roster.append(
        [
            "ID",
            "성명",
            "학교명",
            "관할동",
            "도로명주소",
            "세대주\n성명",
            "핸드폰",
            "비고",
        ]
    )
    # ID 외 blank는 AnnualWorkbook이 None으로 보존해야 한다.
    roster.append(["P001", None, "분당초", None, None, None, None, None])

    delivery = wb.create_sheet("배송현황")
    # 실제 물리 열은 7월→6월→5월 역순이다.
    delivery.append(
        [
            "ID",
            "식종",
            "배송담당",
            "연번",
            "20260701",
            "20260608",
            "20260607",
            "20260602",
            "20260601",
            "20260531",
        ]
    )
    delivery.append(["P001", "조식", "기사A", "003", 1, 0, 1, None, 1, 1])
    wb.save(path)
    wb.close()
    return path


def test_generate_invoice_creates_valid_workbook_from_annual_book(
    tmp_path: Path, template_path: str
):
    output = tmp_path / "청구서_202606.xlsx"
    config = InvoiceGenerationConfig(
        template_path=template_path,
        output_path=str(output),
        year=2026,
        month=6,
        annual_book=_book(),
        col_mapping={
            "성명": "성명",
            "학교명": "학교명",
            "비고": "비고",
            "__calc_breakfast": "조식",
            "__calc_dinner": "석식",
            "__calc_lunch": "중식",
            "__calc_price": "청구금액(원)",
        },
    )

    result = InvoiceService().generate(config)

    assert result.success, result.error
    assert result.count == 2
    assert output.is_file()
    assert result.enriched[0]["_bf_cnt"] == 2
    assert result.enriched[0]["_dn_cnt"] == 0
    assert result.enriched[0]["배송담당"] == "김배송"
    assert result.enriched[1]["_dn_cnt"] == 3
    assert result.enriched[1]["배송담당"] == "이배송"

    wb = openpyxl.load_workbook(output, data_only=False)
    try:
        assert wb.sheetnames == ["청구서"]
        ws = wb["청구서"]
        assert ws.max_row >= 5
        # 실제 템플릿 M/N/O = 중식/석식/조식
        assert ws["A4"].value == "003"
        assert ws["O4"].value == 2
        assert ws["N5"].value == 3
        assert "9500" in str(ws["Q4"].value)
    finally:
        wb.close()


def test_real_annual_xlsx_to_invoice_and_confirm_end_to_end(
    tmp_path: Path,
    template_path: str,
    confirm_template_path: str,
):
    annual_path = _write_annual_workbook(tmp_path / "annual.xlsx")
    invoice_path = tmp_path / "invoice.xlsx"
    confirm_dir = tmp_path / "confirm"
    confirm_dir.mkdir()

    book = AnnualWorkbook(annual_path).load()
    assert book.roster_by_id["P001"].extra["성명"] is None
    assert book.dates == (
        date(2026, 5, 31),
        date(2026, 6, 1),
        date(2026, 6, 2),
        date(2026, 6, 7),
        date(2026, 6, 8),
        date(2026, 7, 1),
    )

    result = InvoiceService().generate(
        InvoiceGenerationConfig(
            template_path=template_path,
            output_path=str(invoice_path),
            year=2026,
            month=6,
            annual_book=book,
            col_mapping={
                "성명": "성명",
                "학교명": "학교명",
                "비고": "비고",
                "__calc_breakfast": "조식",
                "__calc_dinner": "석식",
                "__calc_lunch": "중식",
                "__calc_price": "청구금액(원)",
            },
        )
    )

    assert result.success, result.error
    assert invoice_path.is_file()
    assert result.enriched[0]["_bf_cnt"] == 2
    assert result.enriched[0]["weekly_meal_counts"][0]["조식"] == 1
    assert result.enriched[0]["weekly_meal_counts"][1]["조식"] == 1
    assert {
        "_breakfast",
        "_dinner",
        "_lunch",
        "_delivery_start",
        "_delivery_end",
        "_daily",
        "_delivery_rows_raw",
        "_reception_memo",
        "excluded_dates",
    }.isdisjoint(result.enriched[0])

    invoice_wb = openpyxl.load_workbook(invoice_path, data_only=False)
    try:
        invoice_ws = invoice_wb["청구서"]
        assert invoice_ws["A4"].value == "003"
        assert invoice_ws["O4"].value == 2
    finally:
        invoice_wb.close()

    created = make_confirm(
        roster_data=result.enriched,
        col_mapping={},
        output_dir=str(confirm_dir),
        template_path=confirm_template_path,
        year=2026,
        month=6,
        meal_type="급식",
    )
    confirm_path = confirm_dir / created[0]["file"]
    assert confirm_path.is_file()
    assert created[0]["sheet_name"] == "기타"

    confirm_wb = openpyxl.load_workbook(confirm_path, data_only=False)
    try:
        confirm_ws = confirm_wb["기타"]
        assert confirm_ws["K3"].value == "기사A"
        assert confirm_ws["G6"].value == 1
        assert confirm_ws["G7"].value == 1
        assert all(
            "None" not in cell.value
            for row in confirm_ws.iter_rows()
            for cell in row
            if isinstance(cell.value, str)
        )
    finally:
        confirm_wb.close()
