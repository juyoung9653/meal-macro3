"""실제 xlsx 파일을 사용하는 연간 배송 워크북 어댑터 테스트."""

from datetime import date, datetime
from pathlib import Path
from typing import cast
from zipfile import ZipFile

import msoffcrypto
import openpyxl
import pytest
from msoffcrypto.format.ooxml import OOXMLFile

from src.core.delivery_matrix import DayState, DeliveryCellValue, MonthState
from src.excel._parsing import parse_annual_date_header
from src.excel.annual_workbook import AnnualWorkbook, WorkbookValidationError

PROFILE_HEADERS = ["성명", "전화"]
CONTROL_HEADERS = ["식종", "배송담당", "연번"]
DEFAULT_HEADERS = [*PROFILE_HEADERS, *CONTROL_HEADERS, "20260728"]


def _person_rows(
    name: str = "홍길동",
    phone: str = "010-1111-1111",
    *,
    meals: tuple[str, str, str] = ("조식", "중식", "석식"),
    date_values: list[list] | None = None,
    staff: str = "김기사",
    sequence: object = "001",
) -> list[list]:
    resolved_values = (
        [[None], [1], [None]] if date_values is None else date_values
    )
    assert len(resolved_values) == len(meals) == 3
    return [
        [name, phone, meal, staff, sequence, *values]
        for meal, values in zip(meals, resolved_values)
    ]


def _write_workbook(
    path: Path,
    *,
    headers: list | None = None,
    rows: list[list] | None = None,
) -> Path:
    wb = openpyxl.Workbook()
    ws = wb.worksheets[0]
    ws.title = "명단"

    resolved_headers = DEFAULT_HEADERS if headers is None else headers
    if resolved_headers:
        ws.append(resolved_headers)

    resolved_rows = _person_rows() if rows is None else rows
    for row in resolved_rows:
        ws.append(row)

    wb.save(path)
    wb.close()
    return path


def _encrypt_workbook(
    plain_path: Path, encrypted_path: Path, *, password: str
) -> Path:
    with plain_path.open("rb") as plain, encrypted_path.open("wb") as encrypted:
        office_file = msoffcrypto.OfficeFile(plain)
        assert isinstance(office_file, OOXMLFile)
        office_file.encrypt(password, encrypted)
    return encrypted_path


def _cache_delivery_formula_result(
    path: Path, *, formula: str, cached_value: str
) -> Path:
    cached_path = path.with_name(f"{path.stem}_cached{path.suffix}")
    worksheet_path = "xl/worksheets/sheet1.xml"
    patched = False

    with ZipFile(path) as source, ZipFile(cached_path, "w") as target:
        for entry in source.infolist():
            content = source.read(entry.filename)
            if entry.filename == worksheet_path:
                formula_xml = f"<f>{formula}</f>"
                replacement = f"{formula_xml}<v>{cached_value}</v>".encode()
                for empty_cache in ("<v />", "<v/>", "<v></v>"):
                    marker = f"{formula_xml}{empty_cache}".encode()
                    if marker in content:
                        content = content.replace(marker, replacement, 1)
                        patched = True
                        break
            target.writestr(entry, content)

    assert patched
    return cached_path


def _assert_invalid(path: Path, message: str) -> None:
    with pytest.raises(WorkbookValidationError) as exc_info:
        AnnualWorkbook(path).load()
    assert message in str(exc_info.value)


def test_parse_annual_date_header_supports_only_new_contract():
    assert parse_annual_date_header("20260728") == date(2026, 7, 28)
    assert parse_annual_date_header(20260728) == date(2026, 7, 28)
    assert parse_annual_date_header(20260728.0) == date(2026, 7, 28)
    assert parse_annual_date_header(date(2026, 7, 28)) == date(2026, 7, 28)
    assert parse_annual_date_header(
        datetime.fromisoformat("2026-07-28T13:30:00")
    ) == date(2026, 7, 28)
    assert parse_annual_date_header("6/15") is None
    assert parse_annual_date_header("20260230") is None


def test_loads_single_sheet_reverse_month_columns_and_supported_header_types(
    tmp_path: Path,
):
    path = _write_workbook(
        tmp_path / "2026_delivery.xlsx",
        # 물리적 순서는 8월 → 7월 → 6월이지만 도메인은 actual date로 정렬해야 한다.
        headers=[
            *PROFILE_HEADERS,
            *CONTROL_HEADERS,
            date(2026, 8, 1),
            datetime.fromisoformat("2026-07-29T08:30:00"),
            "20260728",
            20260616,
            date(2026, 6, 15),
        ],
        rows=[
            ["홍길동", "010-1111-1111", "석식", "이기사", "A-1", None, None, None, None, None],
            ["홍길동", "010-1111-1111", "조식", "김기사", "001", None, None, None, None, None],
            ["홍길동", "010-1111-1111", "중식", "김기사", "010", None, None, 1, None, 1],
            ["김영희", "010-2222-2222", "중식", "김기사", 2, None, None, 0, None, 1],
            ["김영희", "010-2222-2222", "석식", "이기사", "A-2", None, None, None, None, None],
            ["김영희", "010-2222-2222", "조식", "김기사", "002", None, None, None, None, None],
        ],
    )

    adapter = AnnualWorkbook(path)
    book = adapter.load()

    assert adapter.load() is book  # 같은 어댑터 인스턴스의 파싱 결과 캐시
    assert book.year == 2026
    assert book.dates == (
        date(2026, 6, 15),
        date(2026, 6, 16),
        date(2026, 7, 28),
        date(2026, 7, 29),
        date(2026, 8, 1),
    )
    assert book.available_months == (6, 7, 8)
    assert book.month_state(2026, 8) is MonthState.UNENTERED
    assert book.day_state(date(2026, 7, 29)) is DayState.NO_DELIVERY
    assert book.day_state(date(2026, 7, 28)) is DayState.ENTERED

    hong, young = book.roster
    assert hong.source_row == 2
    assert hong.extra == {
        "성명": "홍길동",
        "전화": "010-1111-1111",
    }
    hong_lunch = book.get_delivery_row(hong, "중식")
    assert hong_lunch is not None
    assert hong_lunch.person is hong
    assert hong_lunch.source_row == 4
    assert hong_lunch.sequence == "010"
    assert hong_lunch.sequence_display == "010"
    assert hong_lunch.raw_value_on(date(2026, 7, 29)) is DeliveryCellValue.BLANK
    assert (
        hong_lunch.raw_value_on(date(2026, 7, 28))
        is DeliveryCellValue.DELIVERED
    )

    delivered = book.delivered_rows_on(
        date(2026, 6, 15), meal="중식", staff="김기사"
    )
    assert tuple(row.person for row in delivered) == (young, hong)
    assert book.monthly_delivery_count(hong, "중식", 2026, 6) == 1
    assert book.previous_delivery_date(date(2026, 8, 1), "중식") == date(
        2026, 7, 28
    )


def test_accepts_three_meals_in_any_order_with_repeated_profile(tmp_path: Path):
    path = _write_workbook(
        tmp_path / "meal_order.xlsx",
        rows=_person_rows(meals=("석식", "조식", "중식")),
    )

    book = AnnualWorkbook(path).load()
    person = book.roster[0]
    rows = book.rows_by_person[person]

    assert tuple(row.meal for row in rows) == ("석식", "조식", "중식")
    assert tuple(row.source_row for row in rows) == (2, 3, 4)
    assert all(row.person is person for row in rows)


def test_preserves_formula_expression_in_profile_extra(tmp_path: Path):
    path = _write_workbook(
        tmp_path / "profile_formula.xlsx",
        headers=["계산값", *CONTROL_HEADERS, "20260728"],
        rows=[
            ["=1+1", "조식", "김기사", "001", None],
            ["=1+1", "중식", "김기사", "001", 1],
            ["=1+1", "석식", "김기사", "001", None],
        ],
    )

    book = AnnualWorkbook(path).load()

    assert book.roster[0].extra["계산값"] == "=1+1"


def test_cached_roster_entries_cannot_be_mutated(tmp_path: Path):
    adapter = AnnualWorkbook(_write_workbook(tmp_path / "immutable.xlsx"))
    book = adapter.load()
    entry = book.roster[0]

    with pytest.raises(TypeError):
        cast(dict[str, object], entry.extra)["성명"] = "변조"

    mutable_copy = entry.as_dict()
    mutable_copy["성명"] = "복사본만 변경"
    cached_book = adapter.load()
    assert cached_book is book
    assert cached_book.roster[0].extra["성명"] == "홍길동"


def test_zero_only_date_and_month_are_entered(tmp_path: Path):
    path = _write_workbook(
        tmp_path / "zero_only.xlsx",
        rows=_person_rows(date_values=[[0], [None], [None]]),
    )
    book = AnnualWorkbook(path).load()
    person = book.roster[0]
    day = date(2026, 7, 28)

    assert book.month_state(2026, 7) is MonthState.ENTERED
    assert book.day_state(day) is DayState.ENTERED
    assert (
        book.raw_delivery_value(person, "조식", day)
        is DeliveryCellValue.PAUSED
    )
    assert book.delivered_rows_on(day, meal="조식", staff="김기사") == ()


def test_loads_password_protected_workbook(tmp_path: Path):
    plain_path = _write_workbook(tmp_path / "plain.xlsx")
    encrypted_path = _encrypt_workbook(
        plain_path, tmp_path / "encrypted.xlsx", password="secret"
    )

    book = AnnualWorkbook(encrypted_path, password="secret").load()
    assert book.year == 2026
    assert book.get_delivery_row(book.roster[0], "중식") is not None

    with pytest.raises(WorkbookValidationError, match="암호 해제에 실패"):
        AnnualWorkbook(encrypted_path, password="wrong").load()


@pytest.mark.parametrize("error_type", [PermissionError, OSError])
def test_preserves_file_access_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[OSError],
):
    path = _write_workbook(tmp_path / "locked.xlsx")

    def deny_access(*_args, **_kwargs):
        raise error_type("파일 접근 거부")

    monkeypatch.setattr(openpyxl, "load_workbook", deny_access)
    with pytest.raises(error_type, match="파일 접근 거부"):
        AnnualWorkbook(path).load()


def test_corrupt_file_error_mentions_corruption_or_password(tmp_path: Path):
    path = tmp_path / "corrupt.xlsx"
    path.write_bytes(b"not an xlsx file")

    with pytest.raises(WorkbookValidationError) as exc_info:
        AnnualWorkbook(path).load()
    message = str(exc_info.value)
    assert "손상" in message
    assert "암호" in message


def test_streams_inflated_dimensions_and_preserves_blank_between_people(
    tmp_path: Path,
):
    rows = [
        *_person_rows(),
        [None] * len(DEFAULT_HEADERS),
        *_person_rows(
            name="김영희",
            phone="010-2222-2222",
            sequence=2,
        ),
    ]
    path = _write_workbook(
        tmp_path / "inflated_dimensions.xlsx",
        rows=rows,
    )
    wb = openpyxl.load_workbook(path)
    wb["명단"].cell(row=1_048_576, column=16_384).number_format = "0"
    wb.save(path)
    wb.close()

    book = AnnualWorkbook(path).load()
    assert tuple(person.extra["성명"] for person in book.roster) == (
        "홍길동",
        "김영희",
    )
    assert tuple(person.source_row for person in book.roster) == (2, 6)
    assert book.get_delivery_row(book.roster[1], "중식") is not None


def test_reads_actual_nonblank_group_after_previous_safety_bound(tmp_path: Path):
    path = _write_workbook(
        tmp_path / "rows_after_bound.xlsx",
        rows=[],
    )
    wb = openpyxl.load_workbook(path)
    ws = wb["명단"]
    for row_number, values in enumerate(_person_rows(), start=100_001):
        for column, value in enumerate(values, start=1):
            ws.cell(row=row_number, column=column, value=value)
    wb.save(path)
    wb.close()

    book = AnnualWorkbook(path).load()
    person = book.roster[0]
    assert person.source_row == 100_001
    assert book.get_delivery_row(person, "중식") is not None


def test_rejects_nonblank_value_beyond_final_header(tmp_path: Path):
    path = _write_workbook(tmp_path / "value_beyond_header.xlsx")
    wb = openpyxl.load_workbook(path)
    wb["명단"]["G2"] = "숨은 값"
    wb.save(path)
    wb.close()

    _assert_invalid(path, "헤더가 없는 G열")
    _assert_invalid(path, "숨은 값")


def test_validation_keeps_excel_row_number_after_blank_row(tmp_path: Path):
    path = _write_workbook(
        tmp_path / "row_number.xlsx",
        rows=[
            [None] * len(DEFAULT_HEADERS),
            *_person_rows(date_values=[[2], [None], [None]]),
        ],
    )
    _assert_invalid(path, "3행")
    _assert_invalid(path, "1, 0 또는 빈칸")


def test_rejects_empty_roster_sheet(tmp_path: Path):
    path = _write_workbook(
        tmp_path / "empty_roster.xlsx",
        headers=[],
        rows=[],
    )
    _assert_invalid(path, "명단 시트가 비어")


def test_rejects_headers_without_dates(tmp_path: Path):
    path = _write_workbook(
        tmp_path / "no_date_headers.xlsx",
        headers=[*PROFILE_HEADERS, *CONTROL_HEADERS],
        rows=[],
    )
    _assert_invalid(path, "YYYYMMDD 날짜 헤더가 하나 이상")


def test_requires_named_roster_sheet(tmp_path: Path):
    path = tmp_path / "missing_roster_sheet.xlsx"
    wb = openpyxl.Workbook()
    wb.worksheets[0].title = "명부"
    wb.save(path)
    wb.close()

    _assert_invalid(path, "필수 시트 '명단'")


def test_requires_exactly_one_sheet(tmp_path: Path):
    path = _write_workbook(tmp_path / "extra_sheet.xlsx")
    wb = openpyxl.load_workbook(path)
    wb.create_sheet("배송현황")
    wb.save(path)
    wb.close()

    _assert_invalid(path, "'명단' 시트 하나만")
    _assert_invalid(path, "추가 시트: 배송현황")


def test_rejects_id_header(tmp_path: Path):
    path = _write_workbook(
        tmp_path / "id_header.xlsx",
        headers=["ID", *PROFILE_HEADERS, *CONTROL_HEADERS, "20260728"],
        rows=[],
    )
    _assert_invalid(path, "ID 열을 사용하지 않습니다")
    _assert_invalid(path, "ID 헤더를 삭제")


def test_requires_at_least_one_profile_header(tmp_path: Path):
    path = _write_workbook(
        tmp_path / "missing_profile_header.xlsx",
        headers=[*CONTROL_HEADERS, "20260728"],
        rows=[],
    )
    _assert_invalid(path, "인적사항 헤더가 하나 이상")


def test_rejects_duplicate_normalized_profile_headers(tmp_path: Path):
    path = _write_workbook(
        tmp_path / "duplicate_profile_header.xlsx",
        headers=["성명", " 성명 ", *CONTROL_HEADERS, "20260728"],
        rows=[],
    )
    _assert_invalid(path, "헤더 '성명'가 중복")


def test_rejects_duplicate_normalized_control_header(tmp_path: Path):
    path = _write_workbook(
        tmp_path / "duplicate_control_header.xlsx",
        headers=["성명", *CONTROL_HEADERS, "식종", "20260728"],
        rows=[],
    )
    _assert_invalid(path, "헤더 '식종'가 중복")


def test_requires_control_headers_in_exact_order(tmp_path: Path):
    path = _write_workbook(
        tmp_path / "control_header_order.xlsx",
        headers=["성명", "배송담당", "식종", "연번", "20260728"],
        rows=[],
    )
    _assert_invalid(path, "식종 | 배송담당 | 연번")
    _assert_invalid(path, "순서")


def test_rejects_date_header_before_control_headers(tmp_path: Path):
    path = _write_workbook(
        tmp_path / "date_before_controls.xlsx",
        headers=[
            "성명",
            "20260727",
            *CONTROL_HEADERS,
            "20260728",
        ],
        rows=[],
    )
    _assert_invalid(path, "날짜 헤더는 '식종 | 배송담당 | 연번' 오른쪽")


def test_rejects_blank_header(tmp_path: Path):
    path = _write_workbook(
        tmp_path / "blank_header.xlsx",
        headers=["성명", None, *CONTROL_HEADERS, "20260728"],
        rows=[],
    )
    _assert_invalid(path, "헤더는 빈칸")


def test_rejects_duplicate_normalized_date_headers(tmp_path: Path):
    path = _write_workbook(
        tmp_path / "duplicate_date_header.xlsx",
        headers=[*PROFILE_HEADERS, *CONTROL_HEADERS, "20260728", 20260728],
        rows=[],
    )
    _assert_invalid(path, "날짜 헤더 '20260728'가 중복")


def test_rejects_incomplete_three_row_person_group(tmp_path: Path):
    path = _write_workbook(
        tmp_path / "incomplete_person.xlsx",
        rows=_person_rows()[:2],
    )
    _assert_invalid(path, "한 사람당 조식·중식·석식 3행")
    _assert_invalid(path, "2행뿐")


def test_rejects_noncontiguous_person_rows(tmp_path: Path):
    rows = _person_rows()
    path = _write_workbook(
        tmp_path / "noncontiguous_person.xlsx",
        rows=[rows[0], [None] * len(DEFAULT_HEADERS), rows[1], rows[2]],
    )
    _assert_invalid(path, "빈 행 없이 연속")
    _assert_invalid(path, "2, 4, 5행")


def test_rejects_mismatched_profile_within_person_group(tmp_path: Path):
    rows = _person_rows()
    rows[1][1] = "010-9999-9999"
    path = _write_workbook(
        tmp_path / "profile_mismatch.xlsx",
        rows=rows,
    )
    _assert_invalid(path, "한 사람의 3행은 인적사항이 같아야")
    _assert_invalid(path, "'전화' 값")


@pytest.mark.parametrize(
    "meals",
    [
        ("야식", "중식", "석식"),
        ("", "중식", "석식"),
        ("조식", "중식", "중식"),
    ],
)
def test_requires_each_exact_meal_once(
    tmp_path: Path,
    meals: tuple[str, str, str],
):
    path = _write_workbook(
        tmp_path / f"invalid_meals_{meals[0] or 'blank'}.xlsx",
        rows=_person_rows(meals=meals),
    )
    _assert_invalid(path, "식종")
    _assert_invalid(path, "조식, 중식, 석식을 각각 한 번씩")


def test_accepts_only_literal_numeric_and_blank_delivery_values(tmp_path: Path):
    path = _write_workbook(
        tmp_path / "literal_values.xlsx",
        headers=[
            *PROFILE_HEADERS,
            *CONTROL_HEADERS,
            "20260728",
            "20260729",
            "20260730",
        ],
        rows=_person_rows(
            date_values=[
                [None, None, None],
                [1, 0, None],
                [None, None, None],
            ]
        ),
    )

    book = AnnualWorkbook(path).load()
    row = book.get_delivery_row(book.roster[0], "중식")
    assert row is not None
    assert row.raw_value_on(date(2026, 7, 28)) is DeliveryCellValue.DELIVERED
    assert row.raw_value_on(date(2026, 7, 29)) is DeliveryCellValue.PAUSED
    assert row.raw_value_on(date(2026, 7, 30)) is DeliveryCellValue.BLANK


@pytest.mark.parametrize("formula", ["=1", "=0", '=""'])
def test_rejects_formula_delivery_values_with_row_and_date(
    tmp_path: Path, formula: str
):
    path = _write_workbook(
        tmp_path / "formula.xlsx",
        rows=_person_rows(date_values=[[formula], [None], [None]]),
    )

    with pytest.raises(WorkbookValidationError) as exc_info:
        AnnualWorkbook(path).load()

    message = str(exc_info.value)
    assert "2행" in message
    assert "20260728" in message
    assert "수식" in message
    assert formula in message


def test_rejects_formula_even_when_cached_result_is_numeric(tmp_path: Path):
    formula_path = _write_workbook(
        tmp_path / "formula_with_cache.xlsx",
        rows=_person_rows(date_values=[["=1"], [None], [None]]),
    )
    path = _cache_delivery_formula_result(
        formula_path, formula="1", cached_value="1"
    )

    cached_workbook = openpyxl.load_workbook(path, data_only=True, read_only=True)
    try:
        assert cached_workbook["명단"]["F2"].value == 1
    finally:
        cached_workbook.close()

    with pytest.raises(WorkbookValidationError, match="2행.*20260728.*수식"):
        AnnualWorkbook(path).load()


def test_rejects_formula_in_password_protected_workbook(tmp_path: Path):
    plain_path = _write_workbook(
        tmp_path / "encrypted_formula_plain.xlsx",
        rows=_person_rows(date_values=[['=""'], [None], [None]]),
    )
    encrypted_path = _encrypt_workbook(
        plain_path, tmp_path / "encrypted_formula.xlsx", password="secret"
    )

    with pytest.raises(WorkbookValidationError, match="2행.*20260728.*수식"):
        AnnualWorkbook(encrypted_path, password="secret").load()


@pytest.mark.parametrize("invalid_value", ["O", "X", "Y", 2, "1"])
def test_rejects_invalid_delivery_cell_values(tmp_path: Path, invalid_value):
    path = _write_workbook(
        tmp_path / f"invalid_value_{invalid_value}.xlsx",
        rows=_person_rows(date_values=[[invalid_value], [None], [None]]),
    )
    _assert_invalid(path, "1, 0 또는 빈칸")
    _assert_invalid(path, repr(invalid_value))


def test_rejects_mixed_year_date_headers(tmp_path: Path):
    path = _write_workbook(
        tmp_path / "mixed_year.xlsx",
        headers=[
            *PROFILE_HEADERS,
            *CONTROL_HEADERS,
            "20261231",
            20270101,
        ],
        rows=_person_rows(
            date_values=[[1, 1], [None, None], [None, None]]
        ),
    )
    _assert_invalid(path, "날짜 헤더는 한 연도")
    _assert_invalid(path, "2026, 2027")


def test_rejects_legacy_month_day_header(tmp_path: Path):
    path = _write_workbook(
        tmp_path / "legacy_header.xlsx",
        headers=[*PROFILE_HEADERS, *CONTROL_HEADERS, "6/15"],
        rows=_person_rows(),
    )
    _assert_invalid(path, "6/15")
    _assert_invalid(path, "YYYYMMDD")
