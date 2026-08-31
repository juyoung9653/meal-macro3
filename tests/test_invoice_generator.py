"""청구서 템플릿 렌더링 회귀 테스트."""

from __future__ import annotations

import os
from pathlib import Path
from typing import cast

import openpyxl
import msoffcrypto
import pytest

from datetime import date

from src.core.delivery_matrix import RosterEntry
from src.core.invoice_data import (
    EnrichedPersonDict,
    MealPrices,
    MonthlyDelivery,
    MonthlyDeliveryBatch,
)
from src.generators.invoice_generator import make_invoice, make_invoice_publication
from src.templates.config import read_template_columns


@pytest.fixture(autouse=True)
def _clear_template_cache():
    read_template_columns.cache_clear()
    yield
    read_template_columns.cache_clear()


def test_typed_mixed_price_amount_is_written_as_exact_number(tmp_path: Path):
    template = "assets/급식_청구서템플릿.xlsx"
    if not os.path.exists(template):
        pytest.skip("급식 청구서 템플릿 없음")
    output = tmp_path / "mixed-price.xlsx"
    columns = read_template_columns(template, sheet_name="청구서")
    price_name = next(name for name in columns if "청구금액" in name)
    batch = MonthlyDeliveryBatch(
        2026,
        6,
        MealPrices(10_000, 10_000, 10_000),
        False,
        (
            MonthlyDelivery(
                RosterEntry(2, {"성명": "홍길동", "비고": ""}),
                "",
                "기사A",
                "001",
                0,
                12,
                0,
                (),
                billing_amount=123_456,
                household_key="household-1",
            ),
        ),
    )

    count, rendered, publication = make_invoice_publication(
        batch,
        {"성명": "성명", "__calc_price": price_name},
        str(output),
        template,
        2026,
        6,
        {"조식": 10_000, "중식": 10_000, "석식": 10_000},
        meal_type="급식",
    )

    assert count == 1 and rendered is batch
    assert publication.output_path == output
    workbook = openpyxl.load_workbook(output, data_only=False)
    try:
        price_column = next(
            index
            for index, name in enumerate(columns, start=1)
            if "청구금액" in name
        )
        assert workbook["청구서"].cell(4, price_column).value == 123_456
    finally:
        workbook.close()
def test_preserves_roster_note_and_ignores_legacy_reception_memo(tmp_path: Path):
    template = "assets/급식_청구서템플릿.xlsx"
    if not os.path.exists(template):
        pytest.skip("급식 청구서 템플릿 없음")

    output = tmp_path / "invoice.xlsx"
    person = cast(
        EnrichedPersonDict,
        {
            "ID": "P001",
            "성명": "홍길동",
            "비고": "명단 원본 비고",
            "_reception_memo": "출력되면 안 되는 접수장 메모",
            "_note": "명단 원본 비고",
            "_bf_cnt": 1,
            "_lh_cnt": 0,
            "_dn_cnt": 0,
            "_total": 1,
            "_amount": "9,500원",
            "weekly_meal_counts": [],
            "배송담당": "기사A",
            "배송순서": "003",
            "연번": "003",
        },
    )

    make_invoice(
        roster_data=[person],
        col_mapping={"성명": "성명", "비고": "비고"},
        output_path=str(output),
        template_path=template,
        year=2026,
        month=6,
        unit_prices={"조식": 9500, "중식": 9500, "석식": 9500},
        meal_type="급식",
    )

    wb = openpyxl.load_workbook(output, data_only=False)
    try:
        ws = wb["청구서"]
        columns = read_template_columns(template, sheet_name="청구서")
        note_column = columns.index("비고") + 1
        sequence_column = columns.index("연번") + 1
        assert ws.cell(row=4, column=note_column).value == "명단 원본 비고"
        assert ws.cell(row=4, column=sequence_column).value == "003"
        assert ws.cell(row=4, column=sequence_column).number_format == "@"
        assert all(
            cell.value != "출력되면 안 되는 접수장 메모"
            for row in ws.iter_rows()
            for cell in row
        )
    finally:
        wb.close()


@pytest.mark.parametrize(
    ("template", "meal_type", "expected_sheets"),
    [
        ("assets/급식_청구서템플릿.xlsx", "급식", ("청구서",)),
        (
            "assets/행복도시락_청구서템플릿.xlsx",
            "행복도시락",
            ("총괄", "학기중"),
        ),
    ],
)
def test_writes_global_note_in_each_subtotal_row_note_cell(
    tmp_path: Path,
    template: str,
    meal_type: str,
    expected_sheets: tuple[str, ...],
):
    if not os.path.exists(template):
        pytest.skip("청구서 템플릿 없음")

    output = tmp_path / f"{meal_type}_global_note.xlsx"
    note = "중식 단가: 1~14일 10,000원 / 15~30일 11,000원"
    person = cast(
        EnrichedPersonDict,
        {
            "성명": "홍길동",
            "비고": "명단 원본 비고",
            "_note": "명단 원본 비고",
            "_bf_cnt": 0,
            "_lh_cnt": 1,
            "_dn_cnt": 0,
            "_total": 1,
            "_amount": "10,000원",
            "weekly_meal_counts": [],
            "배송담당": "기사A",
            "배송순서": "001",
            "연번": "001",
        },
    )

    make_invoice(
        roster_data=[person],
        col_mapping={"성명": "성명", "비고": "비고"},
        output_path=str(output),
        template_path=template,
        year=2026,
        month=9,
        unit_prices={"조식": 10_000, "중식": 10_000, "석식": 10_000},
        meal_type=meal_type,
        global_note=note,
    )

    wb = openpyxl.load_workbook(output, data_only=False)
    try:
        assert tuple(wb.sheetnames) == expected_sheets
        for sheet_name in expected_sheets:
            ws = wb[sheet_name]
            note_column = next(
                cell.column
                for row in ws.iter_rows(min_row=1, max_row=2)
                for cell in row
                if cell.value == "비고"
            )
            assert ws.cell(row=3, column=note_column).value == note
            assert ws.cell(row=3, column=note_column).alignment.wrap_text
            assert ws.cell(row=4, column=note_column).value == "명단 원본 비고"
    finally:
        wb.close()


def test_invoice_writer_applies_output_password(tmp_path: Path):
    template = "assets/급식_청구서템플릿.xlsx"
    if not os.path.exists(template):
        pytest.skip("급식 청구서 템플릿 없음")
    output = tmp_path / "protected_invoice.xlsx"
    person = cast(
        EnrichedPersonDict,
        {
            "성명": "홍길동",
            "_bf_cnt": 1,
            "_lh_cnt": 0,
            "_dn_cnt": 0,
            "_total": 1,
            "_amount": "10,000원",
            "weekly_meal_counts": [],
            "배송담당": "기사A",
            "배송순서": "001",
            "연번": "001",
        },
    )

    make_invoice(
        roster_data=[person],
        col_mapping={"성명": "성명"},
        output_path=str(output),
        template_path=template,
        year=2026,
        month=9,
        unit_prices={"조식": 10_000, "중식": 10_000, "석식": 10_000},
        output_password="shared-1234",
    )

    with output.open("rb") as source:
        office_file = msoffcrypto.OfficeFile(source)
        assert office_file.is_encrypted()
        office_file.load_key(password="shared-1234")
