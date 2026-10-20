"""청구서 템플릿 렌더링 회귀 테스트."""

from __future__ import annotations

import os
from pathlib import Path

import msoffcrypto
import openpyxl
import pytest

from src.core.delivery_matrix import RosterEntry
from src.core.invoice_data import (
    MealPrices,
    MonthlyDelivery,
    MonthlyDeliveryBatch,
)
from src.generators.invoice_generator import make_invoice_publication
from src.services.document_column_mappings import (
    effective_mapping,
    invoice_default_mapping,
)
from src.templates.config import read_template_columns


@pytest.fixture(autouse=True)
def _clear_template_cache():
    read_template_columns.cache_clear()
    yield
    read_template_columns.cache_clear()


def test_typed_mixed_price_amount_is_written_as_exact_number(tmp_path: Path):
    template = "assets/급식/급식_청구서템플릿.xlsx"
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

    count, publication = make_invoice_publication(
        batch,
        {"성명": "성명", "__calc_price": price_name},
        str(output),
        template,
        meal_type="급식",
    )

    assert count == 1
    assert publication.output_path == output
    workbook = openpyxl.load_workbook(output, data_only=False)
    try:
        price_column = next(
            index for index, name in enumerate(columns, start=1) if "청구금액" in name
        )
        assert workbook["청구서"].cell(4, price_column).value == 123_456
    finally:
        workbook.close()


def test_distinct_meal_prices_reach_saved_invoice_formula(tmp_path: Path):
    template = Path("assets/급식/급식_청구서템플릿.xlsx")
    output = tmp_path / "distinct-prices.xlsx"
    person = MonthlyDelivery(
        roster=RosterEntry(2, {"성명": "단가 검증"}),
        note="",
        delivery_staff="기사A",
        delivery_sequence="001",
        breakfast_count=3,
        lunch_count=2,
        dinner_count=4,
        weekly=(),
        billing_amount=None,
    )
    batch = MonthlyDeliveryBatch(
        2026, 6, MealPrices(7_100, 8_300, 9_700), False, (person,)
    )

    count, _publication = make_invoice_publication(
        batch,
        invoice_default_mapping(template, "급식", False),
        str(output),
        str(template),
        meal_type="급식",
    )

    assert count == 1
    workbook = openpyxl.load_workbook(output, data_only=False)
    try:
        sheet = workbook["청구서"]
        assert tuple(sheet[cell].value for cell in ("M2", "N2", "O2", "Q2")) == (
            "중식", "석식", "조식", "청구금액(원)"
        )
        assert tuple(sheet[cell].value for cell in ("M4", "N4", "O4")) == (2, 4, 3)
        formula = sheet["Q4"].value
        assert sheet["Q4"].data_type == "f"
        assert formula == "=O4*7100+N4*9700+M4*8300"
        # openpyxl does not calculate Excel formulas. Evaluate this checked,
        # multiplication-and-sum formula using the cells actually saved.
        amount = 0
        for term in formula.removeprefix("=").split("+"):
            reference, price = term.split("*")
            amount += sheet[reference].value * int(price)
        assert amount == 76_700
    finally:
        workbook.close()


def test_preserves_roster_note_and_numbers_invoice_rows(tmp_path: Path):
    template = "assets/급식/급식_청구서템플릿.xlsx"
    if not os.path.exists(template):
        pytest.skip("급식 청구서 템플릿 없음")

    output = tmp_path / "invoice.xlsx"
    person = MonthlyDelivery(
        roster=RosterEntry(
            2, {"ID": "P001", "성명": "홍길동", "비고": "명단 원본 비고"}
        ),
        note="명단 원본 비고",
        delivery_staff="기사A",
        delivery_sequence="003",
        breakfast_count=1,
        lunch_count=0,
        dinner_count=0,
        weekly=(),
    )

    make_invoice_publication(
        batch=MonthlyDeliveryBatch(
            2026, 6, MealPrices(9500, 9500, 9500), False, (person,)
        ),
        col_mapping={"성명": "성명", "비고": "비고"},
        output_path=str(output),
        template_path=template,
        meal_type="급식",
    )

    wb = openpyxl.load_workbook(output, data_only=False)
    try:
        ws = wb["청구서"]
        columns = read_template_columns(template, sheet_name="청구서")
        note_column = columns.index("비고") + 1
        sequence_column = columns.index("연번") + 1
        assert ws.cell(row=4, column=note_column).value == "명단 원본 비고"
        assert ws.cell(row=4, column=sequence_column).value == 1
        assert ws.cell(row=4, column=sequence_column).number_format == "0"
    finally:
        wb.close()


@pytest.mark.parametrize(
    ("template", "meal_type", "expected_sheets"),
    [
        ("assets/급식/급식_청구서템플릿.xlsx", "급식", ("청구서",)),
        (
            "assets/행복도시락/행복도시락_청구서템플릿.xlsx",
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
    person = MonthlyDelivery(
        roster=RosterEntry(2, {"성명": "홍길동", "비고": "명단 원본 비고"}),
        note="명단 원본 비고",
        delivery_staff="기사A",
        delivery_sequence="001",
        breakfast_count=0,
        lunch_count=1,
        dinner_count=0,
        weekly=(),
    )

    make_invoice_publication(
        batch=MonthlyDeliveryBatch(
            2026, 9, MealPrices(10000, 10000, 10000), False, (person,)
        ),
        col_mapping={"성명": "성명", "비고": "비고"},
        output_path=str(output),
        template_path=template,
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
    template = "assets/급식/급식_청구서템플릿.xlsx"
    if not os.path.exists(template):
        pytest.skip("급식 청구서 템플릿 없음")
    output = tmp_path / "protected_invoice.xlsx"
    person = MonthlyDelivery(
        roster=RosterEntry(2, {"성명": "홍길동"}),
        note="",
        delivery_staff="기사A",
        delivery_sequence="001",
        breakfast_count=1,
        lunch_count=0,
        dinner_count=0,
        weekly=(),
    )

    make_invoice_publication(
        batch=MonthlyDeliveryBatch(
            2026, 9, MealPrices(10000, 10000, 10000), False, (person,)
        ),
        col_mapping={"성명": "성명"},
        output_path=str(output),
        template_path=template,
        output_password="shared-1234",
    )

    with output.open("rb") as source:
        office_file = msoffcrypto.OfficeFile(source)
        assert office_file.is_encrypted()
        office_file.load_key(password="shared-1234")


@pytest.mark.parametrize(
    ("meal_type", "is_vacation_month", "template", "output_sheets"),
    [
        ("급식", False, "assets/급식/급식_청구서템플릿.xlsx", ("청구서",)),
        (
            "행복도시락",
            False,
            "assets/행복도시락/행복도시락_청구서템플릿.xlsx",
            ("총괄", "학기중"),
        ),
        (
            "행복도시락",
            True,
            "assets/행복도시락/행복도시락_청구서템플릿.xlsx",
            ("총괄", "학기중", "방중"),
        ),
    ],
)
def test_generated_invoice_keeps_relationship_editable_and_automatic_columns_protected(
    tmp_path: Path,
    meal_type: str,
    is_vacation_month: bool,
    template: str,
    output_sheets: tuple[str, ...],
):
    if not os.path.exists(template):
        pytest.skip("청구서 템플릿 없음")

    def normalized(value: str) -> str:
        return "".join(value.split()).replace("(급식수)", "")

    prefix = "" if meal_type == "급식" else ("방학_" if is_vacation_month else "학기_")
    source_sheet = output_sheets[0] if not prefix else prefix + output_sheets[0]
    columns = read_template_columns(template, sheet_name=source_sheet)
    relationship_target = next(
        column for column in columns if normalized(column) == "아동과의관계"
    )
    defaults = invoice_default_mapping(Path(template), meal_type, is_vacation_month)
    total_target = defaults["__calc_total"]
    assert normalized(total_target) == "계"

    custom_relationship = "custom:relationship"
    mapping = effective_mapping(
        defaults,
        {
            relationship_target: custom_relationship,
            total_target: "아동과의 관계",
            "제공기관": "학교명",
        },
    )
    assert mapping[relationship_target] == custom_relationship
    assert mapping["제공기관"] == "제공기관"
    assert total_target not in mapping

    batch = MonthlyDeliveryBatch(
        2026,
        7,
        MealPrices(10_000, 10_000, 10_000),
        is_vacation_month,
        (
            MonthlyDelivery(
                RosterEntry(
                    2,
                    {
                        "성명": "홍길동",
                        "학교명": "오래된 기관",
                        "아동과의 관계": "기본 관계",
                        custom_relationship: "할머니",
                        "제공기관": "공통센터",
                        "비고": "",
                    },
                ),
                "",
                "기사A",
                "001",
                1,
                1,
                1,
                (),
                household_key="household-1",
            ),
        ),
    )
    output = tmp_path / f"{meal_type}-{is_vacation_month}.xlsx"

    make_invoice_publication(batch, mapping, str(output), template, meal_type=meal_type)

    workbook = openpyxl.load_workbook(output, data_only=False)
    try:
        assert tuple(workbook.sheetnames) == output_sheets
        for output_sheet in output_sheets:
            original_sheet = output_sheet if not prefix else prefix + output_sheet
            original_columns = read_template_columns(
                template, sheet_name=original_sheet
            )
            relation_column = next(
                index
                for index, column in enumerate(original_columns, start=1)
                if normalized(column) == "아동과의관계"
            )
            total_column = next(
                index
                for index, column in enumerate(original_columns, start=1)
                if normalized(column) == "계"
            )
            provider_column = original_columns.index("제공기관") + 1
            sheet = workbook[output_sheet]
            assert sheet.cell(4, relation_column).value == "할머니"
            assert sheet.cell(4, provider_column).value == "공통센터"
            total_formula = sheet.cell(4, total_column).value
            assert isinstance(total_formula, str) and total_formula.startswith("=")
    finally:
        workbook.close()


@pytest.mark.parametrize("profile,vacation,expected", (
    ("급식", False, {"청구서": ("석식 대상", "중식 대상", "조식 대상")}),
    ("행복도시락", False, {"총괄": ("석식 대상", "중식 대상", "조식 대상"), "학기중": ("석식 대상", "중식 대상", "조식 대상")}),
    ("행복도시락", True, {"총괄": ("석식 대상", "중식 대상", "조식 대상"), "학기중": ("석식 대상", "조식 대상"), "방중": ("중식 대상",)}),
))
def test_invoice_ordinals_restart_per_filtered_sheet(tmp_path, profile, vacation, expected):
    template = Path(f"assets/{profile}/{profile}_청구서템플릿.xlsx")
    deliveries = tuple(MonthlyDelivery(
        RosterEntry(position, {"성명": name}), "", "합성 기사", sequence,
        breakfast, lunch, dinner, (), billing_amount=(breakfast + lunch + dinner) * 10000,
    ) for position, name, sequence, breakfast, lunch, dinner in (
        (99, "제외 대상", "001", 0, 0, 0),
        (500, "석식 대상", "009 / 002", 0, 0, 1),
        (3, "중식 대상", "", 0, 2, 0),
        (80, "조식 대상", "009", 3, 0, 0),
    ))
    batch = MonthlyDeliveryBatch(2026, 8, MealPrices(10000, 10000, 10000), vacation, deliveries)
    mapping = invoice_default_mapping(template, profile, vacation)
    mapping["연번"] = "성명"  # Neither saved mappings nor fallbacks may replace automatic numbering.
    output = tmp_path / "numbered.xlsx"
    count, _ = make_invoice_publication(batch, mapping, str(output), str(template),
        meal_type=profile, column_fallbacks={"연번": "성명"})
    assert count == 3
    book = openpyxl.load_workbook(output, data_only=False)
    try:
        for sheet_name, names in expected.items():
            source_sheet = "청구서" if profile == "급식" else ("방학_" if vacation else "학기_") + sheet_name
            columns = read_template_columns(str(template), sheet_name=source_sheet)
            serial, name_col = columns.index("연번") + 1, columns.index("성명") + 1
            sheet = book[sheet_name]
            assert tuple(sheet.cell(row, name_col).value for row in range(4, 4 + len(names))) == names
            assert tuple(sheet.cell(row, serial).value for row in range(4, 4 + len(names))) == tuple(range(1, len(names) + 1))
            assert all(sheet.cell(row, serial).data_type == "n" and sheet.cell(row, serial).number_format == "0"
                       for row in range(4, 4 + len(names)))
        assert deliveries[1].delivery_sequence == "009 / 002"
    finally:
        book.close()
