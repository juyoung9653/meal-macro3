"""청구서 템플릿 렌더링 회귀 테스트."""

from __future__ import annotations

import os
from pathlib import Path
from typing import cast

import openpyxl
import pytest

from src.core.invoice_data import EnrichedPersonDict
from src.generators.invoice_generator import make_invoice
from src.templates.config import read_template_columns


@pytest.fixture(autouse=True)
def _clear_template_cache():
    read_template_columns.cache_clear()
    yield
    read_template_columns.cache_clear()


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
