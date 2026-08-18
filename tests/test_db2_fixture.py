"""배포용 ``db`` 테이블 테스트 워크북 ``assets/db2.xlsx`` 회귀 테스트."""

from datetime import date
from pathlib import Path

import openpyxl

from src.core.delivery_matrix import (
    ALLOWED_MEALS,
    AnnualDeliveryBook,
    DayState,
    DeliveryCellValue,
    EffectiveDeliveryState,
    MonthState,
    RosterEntry,
)
from src.excel.annual_workbook import TABLE_NAME, AnnualWorkbook

FIXTURE_PATH = Path("assets/db2.xlsx")
FIXTURE_SHEET_NAME = "종합_jon(DB)"
PROFILE_HEADERS = (
    "취학유형",
    "학교명",
    "학년",
    "성명",
    "생년월일",
    "세대주\n성명",
    "아동과의\n관계",
    "관할동",
    "도로명주소",
    "상세주소",
    "전화번호",
    "핸드폰",
    "제공기관",
    "비고",
)
CONTROL_HEADERS = ("식종", "배송담당", "연번")
DATE_HEADERS = (
    "20260803",
    "20260804",
    "20260727",
    "20260728",
    "20260729",
    "20260730",
    "20260731",
    "20260629",
    "20260630",
)


def _person_named(book: AnnualDeliveryBook, name: str) -> RosterEntry:
    matches = [person for person in book.roster if person.extra.get("성명") == name]
    assert len(matches) == 1, f"성명 {name!r}인 fixture 사람은 정확히 한 명이어야 합니다."
    return matches[0]


def test_db2_exercises_the_db_table_annual_delivery_contract():
    book = AnnualWorkbook(FIXTURE_PATH).load()

    assert book.year == 2026
    assert len(book.roster) == 7
    assert len(book.delivery_rows) == 21
    assert tuple(person.display_name for person in book.roster) == tuple(
        f"가상아동{index}" for index in range(1, 8)
    )

    for index, person in enumerate(book.roster):
        assert person.source_row == 2 + index * 3
        rows = book.rows_by_person[person]
        assert tuple(row.meal for row in rows) == ALLOWED_MEALS
        assert tuple(row.source_row for row in rows) == tuple(
            range(person.source_row, person.source_row + 3)
        )
        assert all(row.person is person for row in rows)

    assert book.available_months == (6, 7, 8)
    assert book.month_state(2026, 6) is MonthState.ENTERED
    assert book.month_state(2026, 7) is MonthState.ENTERED
    assert book.month_state(2026, 8) is MonthState.UNENTERED

    no_delivery_day = date(2026, 7, 29)
    second_no_delivery_day = date(2026, 7, 30)
    mixed_day = date(2026, 7, 28)
    assert book.day_state(no_delivery_day) is DayState.NO_DELIVERY
    assert book.day_state(second_no_delivery_day) is DayState.NO_DELIVERY
    assert book.delivered_rows_on(second_no_delivery_day) == ()
    assert all(
        row.raw_value_on(no_delivery_day) is DeliveryCellValue.BLANK
        for row in book.delivery_rows
    )
    assert all(
        row.raw_value_on(second_no_delivery_day) is DeliveryCellValue.BLANK
        for row in book.delivery_rows
    )
    assert all(
        row.raw_value_on(day) is DeliveryCellValue.BLANK
        for row in book.delivery_rows
        for day in (date(2026, 8, 3), date(2026, 8, 4))
    )

    delivered_person = _person_named(book, "가상아동1")
    stopped_person = _person_named(book, "가상아동2")
    paused_person = _person_named(book, "가상아동3")
    delivered_row = book.get_delivery_row(delivered_person, "중식")
    assert delivered_row is not None
    assert delivered_row.person is delivered_person
    assert delivered_row in book.rows_by_person[delivered_person]
    assert delivered_row.sequence_display == "001"
    assert (
        book.raw_delivery_value(delivered_person, "중식", mixed_day)
        is DeliveryCellValue.DELIVERED
    )
    assert (
        book.effective_delivery_state(stopped_person, "조식", mixed_day)
        is EffectiveDeliveryState.STOPPED
    )
    assert (
        book.effective_delivery_state(paused_person, "중식", mixed_day)
        is EffectiveDeliveryState.PAUSED
    )

    substitute_rows = book.delivered_rows_on(
        mixed_day,
        meal="중식",
        staff="기사A",
    )
    assert [
        (row.person.extra["성명"], row.sequence_display) for row in substitute_rows
    ] == [("가상아동1", "001")]

    report_dates = {
        day.strftime("%Y%m%d")
        for day in book.dates
        if date(2026, 7, 27) <= day <= date(2026, 7, 31)
        and any(book.delivered_rows_on(day, meal=meal) for meal in ALLOWED_MEALS)
    }
    assert "20260729" not in report_dates
    assert "20260730" not in report_dates
    assert {"20260727", "20260728", "20260731"} <= report_dates

    entered_lunch_rows = book.delivered_rows_on(date(2026, 7, 27), meal="중식")
    assert {row.staff for row in entered_lunch_rows} == {"기사A", "기사B"}
    first_household = tuple(
        book.get_delivery_row(_person_named(book, name), "중식")
        for name in ("가상아동1", "가상아동2")
    )
    assert all(row is not None for row in first_household)
    assert {(row.staff, row.sequence_display) for row in first_household if row} == {
        ("기사A", "001")
    }
    assert {
        (row.person.extra["도로명주소"], row.person.extra["상세주소"])
        for row in first_household
        if row
    } == {("성남시 분당구 테스트로 1", "101호")}

    added_blank_meals = {
        "가상아동2": ("조식",),
        "가상아동3": ("조식",),
        "가상아동4": ("조식",),
        "가상아동5": ("조식",),
        "가상아동6": ("조식", "석식"),
        "가상아동7": ("조식", "석식"),
    }
    for name, meals in added_blank_meals.items():
        person = _person_named(book, name)
        for meal in meals:
            row = book.get_delivery_row(person, meal)
            assert row is not None
            assert all(
                row.raw_value_on(day) is DeliveryCellValue.BLANK
                for day in book.dates
            )


def test_db2_keeps_excel_editing_aids_and_exact_physical_layout():
    workbook = openpyxl.load_workbook(FIXTURE_PATH, data_only=False)
    try:
        assert workbook.sheetnames == [FIXTURE_SHEET_NAME]
        worksheet = workbook[FIXTURE_SHEET_NAME]
        assert tuple(worksheet.tables) == (TABLE_NAME,)
        assert worksheet.tables[TABLE_NAME].ref == "A1:Z22"
        expected_headers = (*PROFILE_HEADERS, *CONTROL_HEADERS, *DATE_HEADERS)
        actual_headers = tuple(
            worksheet.cell(row=1, column=column).value
            for column in range(1, worksheet.max_column + 1)
        )
        assert actual_headers == expected_headers
        assert "ID" not in actual_headers
        assert worksheet.max_row == 22
        assert worksheet.max_column == 26
        assert worksheet.freeze_panes == "R2"
        assert worksheet.auto_filter.ref == "A1:Z22"

        profile_end = len(PROFILE_HEADERS)
        meal_column = profile_end + 1
        staff_column = meal_column + 1
        sequence_column = staff_column + 1
        first_date_column = sequence_column + 1

        for start_row in range(2, worksheet.max_row + 1, 3):
            profiles = {
                tuple(
                    worksheet.cell(row=row, column=column).value
                    for column in range(1, profile_end + 1)
                )
                for row in range(start_row, start_row + 3)
            }
            assert len(profiles) == 1
            assert tuple(
                worksheet.cell(row=row, column=meal_column).value
                for row in range(start_row, start_row + 3)
            ) == ALLOWED_MEALS

        for row in range(2, worksheet.max_row + 1):
            assert worksheet.cell(row=row, column=staff_column).value in {
                "기사A",
                "기사B",
            }
            sequence_cell = worksheet.cell(row=row, column=sequence_column)
            assert sequence_cell.value in {"001", "002"}
            assert isinstance(sequence_cell.value, str)
            assert sequence_cell.number_format == "@"

        validations = {
            (validation.formula1, str(validation.sqref), validation.allow_blank)
            for validation in worksheet.data_validations.dataValidation
        }
        assert validations == {
            ('"조식,중식,석식"', "O2:O22", False),
            ('"1"', "R2:Z22", True),
        }

        conditional_ranges = tuple(worksheet.conditional_formatting)
        assert len(conditional_ranges) == 1
        conditional_formatting = conditional_ranges[0]
        assert str(conditional_formatting.sqref) == "R2:Z22"
        conditional_rules = tuple(
            worksheet.conditional_formatting[conditional_formatting]
        )
        assert {
            (rule.type, rule.operator, tuple(rule.formula))
            for rule in conditional_rules
        } == {
            ("cellIs", "equal", ("1",)),
        }
        fill_by_value = {
            rule.formula[0]: rule.dxf.fill.fgColor.rgb for rule in conditional_rules
        }
        assert fill_by_value == {"1": "00C6E0B4"}

        date_column_by_header = {
            worksheet.cell(row=1, column=column).value: column
            for column in range(first_date_column, worksheet.max_column + 1)
        }
        assert all(
            worksheet.cell(row=row, column=date_column_by_header["20260729"]).value
            is None
            for row in range(2, worksheet.max_row + 1)
        )
        assert all(
            worksheet.cell(row=row, column=date_column_by_header["20260730"]).value
            is None
            for row in range(2, worksheet.max_row + 1)
        )
        mixed_values = [
            worksheet.cell(row=row, column=date_column_by_header["20260728"]).value
            for row in range(2, worksheet.max_row + 1)
        ]
        assert all(value in (None, 1) for value in mixed_values)
        assert any(value is None for value in mixed_values)
        assert 1 in mixed_values
        assert all(
            worksheet.cell(row=row, column=date_column_by_header[header]).value
            is None
            for header in ("20260803", "20260804")
            for row in range(2, worksheet.max_row + 1)
        )
        assert all(
            cell.data_type != "f" and cell.value in (None, 1)
            for row in worksheet.iter_rows(
                min_row=2,
                min_col=first_date_column,
                max_row=worksheet.max_row,
                max_col=worksheet.max_column,
            )
            for cell in row
        )
    finally:
        workbook.close()
