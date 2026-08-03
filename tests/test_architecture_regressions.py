"""Architecture-review regression tests for Excel and template boundaries."""

from __future__ import annotations

from pathlib import Path

import openpyxl
import pytest

from src.excel.excel_utils import adjust_formula
from src.templates import template_scanner
from src.templates.template_scanner import scan_placeholders


@pytest.fixture(autouse=True)
def _clear_placeholder_cache():
    template_scanner._mem_cache.clear()
    yield
    template_scanner._mem_cache.clear()


def test_adjust_formula_shifts_references():
    assert adjust_formula("=A1+B12", 1) == "=A2+B13"
    assert adjust_formula("plain text", 1) == "plain text"


def test_placeholder_cache_is_scoped_to_selected_sheets(tmp_path: Path):
    template = tmp_path / "template.xlsx"
    workbook = openpyxl.Workbook()
    workbook.active.title = "A"
    workbook["A"]["A1"] = "{first}"
    workbook.create_sheet("B")["A1"] = "{second}"
    workbook.save(template)
    workbook.close()

    assert scan_placeholders(str(template), ["A"]) == {"A": {"first": [(1, 1)]}}
    assert scan_placeholders(str(template), ["B"]) == {"B": {"second": [(1, 1)]}}


def test_placeholder_scan_rejects_missing_requested_sheet(tmp_path: Path):
    template = tmp_path / "template.xlsx"
    workbook = openpyxl.Workbook()
    workbook.active.title = "existing"
    workbook.save(template)
    workbook.close()

    with pytest.raises(ValueError, match="Template sheet not found: missing"):
        scan_placeholders(str(template), ["missing"])
