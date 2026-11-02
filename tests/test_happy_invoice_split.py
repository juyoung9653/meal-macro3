"""Happy Lunchbox split sheets must reconcile to the date-priced total."""
from datetime import date
from pathlib import Path

import openpyxl
import pytest

from src.core.delivery_matrix import RosterEntry
from src.core.invoice_data import MealPrices, MonthlyDelivery, MonthlyDeliveryBatch
from src.domain.roster import Meal
from src.generators.invoice_generator import make_invoice_publication
from src.services.document_column_mappings import invoice_default_mapping
from src.services.sqlite_document_service import prepare_monthly_document_data
from src.templates.config import read_template_column_indexes
from tests.test_sqlite_document_service import _source_snapshot


TEMPLATE = Path("assets/행복도시락/행복도시락_청구서템플릿.xlsx")


def _value(sheet, value):
    """Evaluate only the writer's sum/product formulas, without Excel or eval."""
    if not isinstance(value, str) or not value.startswith("="):
        return value or 0
    result = 0
    for term in value[1:].split("+"):
        factors = term.split("*")
        product = 1
        for factor in factors:
            product *= int(factor) if factor.isdigit() else (sheet[factor].value or 0)
        result += product
    return result


def _totals(path):
    book = openpyxl.load_workbook(path, data_only=False)
    try:
        result = {}
        for name in ("총괄", "학기중", "방중"):
            columns = read_template_column_indexes(str(TEMPLATE), sheet_name="방학_" + name)
            sheet = book[name]
            result[name] = tuple(_value(sheet, sheet.cell(4, columns[field]).value)
                                 for field in ("계", "청구금액"))
        return result
    finally:
        book.close()


@pytest.mark.parametrize("exact_amounts", [False, True])
@pytest.mark.parametrize("replace_count_column", [False, True])
def test_mixed_meal_split_uses_its_own_counts_and_prices(tmp_path, exact_amounts, replace_count_column):
    amounts = {Meal.BREAKFAST: 19_000, Meal.LUNCH: 33_000, Meal.DINNER: 45_000} if exact_amounts else None
    person = MonthlyDelivery(
        RosterEntry(2, {"성명": "합성 혼합 식종", "학교명": "합성 학교"}), "", "합성 기사", "1",
        2, 3, 4, (), billing_amount=97_000 if exact_amounts else None, billing_amounts=amounts,
    )
    batch = MonthlyDeliveryBatch(2026, 7, MealPrices(9000, 10000, 11000), True, (person,))
    mapping = invoice_default_mapping(TEMPLATE, "행복도시락", True)
    if replace_count_column:
        mapping["조식"] = "학교명"
    output = tmp_path / "split.xlsx"
    count, _ = make_invoice_publication(batch, mapping, output, str(TEMPLATE), meal_type="행복도시락")
    assert count == 1
    totals = _totals(output)
    assert totals == {
        "총괄": (9, 97_000 if exact_amounts else 92_000),
        "학기중": (6, 64_000 if exact_amounts else 62_000),
        "방중": (3, 33_000 if exact_amounts else 30_000),
    }
    assert totals["총괄"] == tuple(a + b for a, b in zip(totals["학기중"], totals["방중"]))
    if replace_count_column:
        book = openpyxl.load_workbook(output)
        try:
            for name in ("총괄", "학기중"):
                columns = read_template_column_indexes(str(TEMPLATE), sheet_name="방학_" + name)
                assert book[name].cell(4, columns["조식"]).value == "합성 학교"
        finally:
            book.close()


def test_sqlite_monthly_price_change_survives_split_publication(tmp_path):
    prepared = prepare_monthly_document_data(
        _source_snapshot(), date(2026, 8, 1), date(2026, 8, 31), tuple(Meal),
    )
    person = prepared.batch.deliveries[0]
    assert person.billing_amounts == {Meal.BREAKFAST: 117_000, Meal.LUNCH: 10_000, Meal.DINNER: 140_000}
    output = tmp_path / "date-priced-split.xlsx"
    make_invoice_publication(prepared.batch, invoice_default_mapping(TEMPLATE, "행복도시락", True),
                             output, str(TEMPLATE), meal_type="행복도시락")
    assert _totals(output) == {"총괄": (26, 267_000), "학기중": (25, 257_000), "방중": (1, 10_000)}


def test_split_never_guesses_mixed_price_amount_from_only_a_total(tmp_path):
    person = MonthlyDelivery(RosterEntry(2, {"성명": "합성 대상"}), "", "", "", 2, 3, 4, (), billing_amount=97_000)
    batch = MonthlyDeliveryBatch(2026, 7, MealPrices(9000, 10000, 11000), True, (person,))
    output = tmp_path / "split.xlsx"
    with pytest.raises(ValueError, match="식종별 청구금액"):
        make_invoice_publication(batch, {}, output, str(TEMPLATE), meal_type="행복도시락")
    assert not output.exists()
