"""실제 xlsx 파일을 사용하는 연간 배송 워크북 어댑터 테스트."""

from datetime import date, datetime
from pathlib import Path
from typing import cast

import msoffcrypto
import openpyxl
import pytest
from msoffcrypto.format.ooxml import OOXMLFile

from src.core.delivery_matrix import DayState, DeliveryCellValue, MonthState
from src.excel._parsing import parse_annual_date_header
from src.excel.annual_workbook import AnnualWorkbook, WorkbookValidationError

FIXED_HEADERS = ["ID", "식종", "배송담당", "연번"]


def _write_workbook(
    path: Path,
    *,
    roster_headers: list | None = None,
    roster_rows: list[list] | None = None,
    delivery_headers: list | None = None,
    delivery_rows: list[list] | None = None,
) -> Path:
    wb = openpyxl.Workbook()
    roster_ws = wb.worksheets[0]
    roster_ws.title = "명단"
    resolved_roster_headers = (
        ["ID", "성명", "전화"] if roster_headers is None else roster_headers
    )
    if resolved_roster_headers:
        roster_ws.append(resolved_roster_headers)
    resolved_roster_rows = (
        [["P001", "홍길동", "010-1111-1111"]]
        if roster_rows is None
        else roster_rows
    )
    for row in resolved_roster_rows:
        roster_ws.append(row)

    delivery_ws = wb.create_sheet("배송현황")
    resolved_delivery_headers = (
        [*FIXED_HEADERS, "20260728"]
        if delivery_headers is None
        else delivery_headers
    )
    if resolved_delivery_headers:
        delivery_ws.append(resolved_delivery_headers)
    resolved_delivery_rows = (
        [["P001", "중식", "김기사", "001", 1]]
        if delivery_rows is None
        else delivery_rows
    )
    for row in resolved_delivery_rows:
        delivery_ws.append(row)

    wb.save(path)
    wb.close()
    return path


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


def test_loads_reverse_month_columns_and_supported_header_types(tmp_path: Path):
    path = _write_workbook(
        tmp_path / "2026_delivery.xlsx",
        roster_rows=[
            ["P001", "홍길동", "010-1111-1111"],
            ["P002", "김영희", "010-2222-2222"],
        ],
        # 물리적 순서는 8월 → 7월 → 6월이지만 도메인은 actual date로 정렬해야 한다.
        delivery_headers=[
            *FIXED_HEADERS,
            date(2026, 8, 1),
            datetime.fromisoformat("2026-07-29T08:30:00"),
            "20260728",
            20260616,
            date(2026, 6, 15),
        ],
        delivery_rows=[
            ["P001", "중식", "김기사", "010", None, None, 1, None, 1],
            ["P002", "중식", "김기사", 2, None, None, 0, None, 1],
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

    assert book.roster_by_id["P001"].extra == {
        "성명": "홍길동",
        "전화": "010-1111-1111",
    }
    p001 = book.delivery_rows_by_key[("P001", "중식")]
    assert p001.sequence == "010"
    assert p001.sequence_display == "010"
    assert p001.raw_value_on(date(2026, 7, 29)) is DeliveryCellValue.BLANK
    assert p001.raw_value_on(date(2026, 7, 28)) is DeliveryCellValue.DELIVERED

    delivered = book.delivered_rows_on(
        date(2026, 6, 15), meal="중식", staff="김기사"
    )
    assert [row.person_id for row in delivered] == ["P002", "P001"]
    assert book.monthly_delivery_count("P001", "중식", 2026, 6) == 1
    assert book.previous_delivery_date(date(2026, 8, 1), "중식") == date(
        2026, 7, 28
    )


def test_cached_roster_entries_cannot_be_mutated(tmp_path: Path):
    adapter = AnnualWorkbook(_write_workbook(tmp_path / "immutable.xlsx"))
    book = adapter.load()
    entry = book.roster_by_id["P001"]

    with pytest.raises(TypeError):
        cast(dict[str, object], entry.extra)["성명"] = "변조"

    mutable_copy = entry.as_dict()
    mutable_copy["성명"] = "복사본만 변경"
    cached_book = adapter.load()
    assert cached_book is book
    assert cached_book.roster_by_id["P001"].extra["성명"] == "홍길동"


def test_zero_only_date_and_month_are_entered(tmp_path: Path):
    path = _write_workbook(
        tmp_path / "zero_only.xlsx",
        delivery_rows=[["P001", "중식", "김기사", 1, 0]],
    )
    book = AnnualWorkbook(path).load()
    day = date(2026, 7, 28)

    assert book.month_state(2026, 7) is MonthState.ENTERED
    assert book.day_state(day) is DayState.ENTERED
    assert book.raw_delivery_value("P001", "중식", day) is DeliveryCellValue.PAUSED
    assert book.delivered_rows_on(day, meal="중식", staff="김기사") == ()


def test_loads_password_protected_workbook(tmp_path: Path):
    plain_path = _write_workbook(tmp_path / "plain.xlsx")
    encrypted_path = tmp_path / "encrypted.xlsx"

    with plain_path.open("rb") as plain, encrypted_path.open("wb") as encrypted:
        office_file = msoffcrypto.OfficeFile(plain)
        assert isinstance(office_file, OOXMLFile)
        office_file.encrypt("secret", encrypted)

    book = AnnualWorkbook(encrypted_path, password="secret").load()
    assert book.year == 2026
    assert book.get_delivery_row("P001", "중식") is not None

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


def test_streams_inflated_dimensions_and_preserves_interior_blank_rows(
    tmp_path: Path,
):
    path = _write_workbook(
        tmp_path / "inflated_dimensions.xlsx",
        roster_rows=[
            ["P001", "홍길동", "010-1111-1111"],
            [None, None, None],
            ["P002", "김영희", "010-2222-2222"],
        ],
        delivery_rows=[
            [None, None, None, None, None],
            ["P002", "중식", "김기사", 2, 1],
        ],
    )
    wb = openpyxl.load_workbook(path)
    wb["배송현황"].cell(row=1_048_576, column=16_384).number_format = "0"
    wb.save(path)
    wb.close()

    book = AnnualWorkbook(path).load()
    assert set(book.roster_by_id) == {"P001", "P002"}
    assert book.get_delivery_row("P002", "중식") is not None


def test_reads_actual_nonblank_row_after_previous_safety_bound(tmp_path: Path):
    path = _write_workbook(
        tmp_path / "row_after_bound.xlsx",
        delivery_rows=[],
    )
    wb = openpyxl.load_workbook(path)
    ws = wb["배송현황"]
    for column, value in enumerate(
        ["P001", "중식", "김기사", 1, 1], start=1
    ):
        ws.cell(row=100_001, column=column, value=value)
    wb.save(path)
    wb.close()

    book = AnnualWorkbook(path).load()
    assert book.get_delivery_row("P001", "중식") is not None


def test_rejects_nonblank_value_beyond_final_header(tmp_path: Path):
    path = _write_workbook(tmp_path / "value_beyond_header.xlsx")
    wb = openpyxl.load_workbook(path)
    wb["배송현황"]["G2"] = "숨은 값"
    wb.save(path)
    wb.close()

    _assert_invalid(path, "헤더가 없는 G열")
    _assert_invalid(path, "숨은 값")


def test_validation_keeps_excel_row_number_after_blank_row(tmp_path: Path):
    path = _write_workbook(
        tmp_path / "row_number.xlsx",
        delivery_rows=[
            [None, None, None, None, None],
            ["P999", "중식", "김기사", 1, 1],
        ],
    )
    _assert_invalid(path, "3행")
    _assert_invalid(path, "명단에 없는 ID 'P999'")


def test_rejects_empty_delivery_sheet(tmp_path: Path):
    path = _write_workbook(
        tmp_path / "empty_delivery.xlsx",
        delivery_headers=[],
        delivery_rows=[],
    )
    _assert_invalid(path, "배송현황 시트가 비어")


def test_rejects_delivery_headers_without_dates(tmp_path: Path):
    path = _write_workbook(
        tmp_path / "no_date_headers.xlsx",
        delivery_headers=FIXED_HEADERS,
        delivery_rows=[],
    )
    _assert_invalid(path, "YYYYMMDD 날짜 헤더가 하나 이상")


def test_requires_exact_minimum_sheet_names(tmp_path: Path):
    path = tmp_path / "missing_sheet.xlsx"
    wb = openpyxl.Workbook()
    wb.worksheets[0].title = "명부"
    wb.create_sheet("배송")
    wb.save(path)
    wb.close()

    _assert_invalid(path, "필수 시트")
    _assert_invalid(path, "명단")
    _assert_invalid(path, "배송현황")


def test_requires_roster_id_header(tmp_path: Path):
    path = _write_workbook(
        tmp_path / "missing_roster_id_header.xlsx",
        roster_headers=["성명", "전화"],
        roster_rows=[["홍길동", "010-1111-1111"]],
    )
    _assert_invalid(path, "필수 헤더 'ID'")


def test_rejects_duplicate_normalized_roster_headers(tmp_path: Path):
    path = _write_workbook(
        tmp_path / "duplicate_roster_header.xlsx",
        roster_headers=["ID", "성명", " 성명 "],
        roster_rows=[["P001", "홍길동", "중복"]],
    )
    _assert_invalid(path, "헤더 '성명'가 중복")


def test_rejects_duplicate_fixed_delivery_header(tmp_path: Path):
    path = _write_workbook(
        tmp_path / "duplicate_fixed_header.xlsx",
        delivery_headers=[*FIXED_HEADERS, "ID", "20260728"],
        delivery_rows=[["P001", "중식", "김기사", 1, "P001", 1]],
    )
    _assert_invalid(path, "고정 헤더 'ID'가 중복")


def test_rejects_duplicate_normalized_date_headers(tmp_path: Path):
    path = _write_workbook(
        tmp_path / "duplicate_date_header.xlsx",
        delivery_headers=[*FIXED_HEADERS, "20260728", 20260728],
        delivery_rows=[["P001", "중식", "김기사", 1, 1, 1]],
    )
    _assert_invalid(path, "날짜 헤더 '20260728'가 중복")


@pytest.mark.parametrize(
    ("roster_rows", "message"),
    [
        ([[None, "홍길동", "010"]], "ID는 빈칸"),
        (
            [["P001", "홍길동", "010"], ["P001", "김영희", "011"]],
            "ID 'P001'이 중복",
        ),
    ],
)
def test_validates_roster_primary_key(
    tmp_path: Path, roster_rows: list[list], message: str
):
    path = _write_workbook(tmp_path / "invalid_roster.xlsx", roster_rows=roster_rows)
    _assert_invalid(path, message)


def test_validates_delivery_foreign_key(tmp_path: Path):
    path = _write_workbook(
        tmp_path / "invalid_fk.xlsx",
        delivery_rows=[["P999", "중식", "김기사", 1, 1]],
    )
    _assert_invalid(path, "명단에 없는 ID 'P999'")


def test_validates_delivery_composite_key(tmp_path: Path):
    path = _write_workbook(
        tmp_path / "duplicate_key.xlsx",
        delivery_rows=[
            ["P001", "중식", "김기사", 1, 1],
            ["P001", "중식", "이기사", 2, 0],
        ],
    )
    _assert_invalid(path, "(ID, 식종) 키 ('P001', '중식')가 중복")


@pytest.mark.parametrize("meal", ["야식", "방중", ""])
def test_allows_only_three_exact_meal_names(tmp_path: Path, meal: str):
    path = _write_workbook(
        tmp_path / f"invalid_meal_{meal or 'blank'}.xlsx",
        delivery_rows=[["P001", meal, "김기사", 1, 1]],
    )
    _assert_invalid(path, "식종")
    _assert_invalid(path, "조식, 중식, 석식")


@pytest.mark.parametrize("invalid_value", ["O", "X", "Y", 2, "1"])
def test_rejects_invalid_delivery_cell_values(tmp_path: Path, invalid_value):
    path = _write_workbook(
        tmp_path / f"invalid_value_{invalid_value}.xlsx",
        delivery_rows=[["P001", "중식", "김기사", 1, invalid_value]],
    )
    _assert_invalid(path, "1, 0 또는 빈칸")
    _assert_invalid(path, repr(invalid_value))


def test_rejects_mixed_year_date_headers(tmp_path: Path):
    path = _write_workbook(
        tmp_path / "mixed_year.xlsx",
        delivery_headers=[*FIXED_HEADERS, "20261231", 20270101],
        delivery_rows=[["P001", "중식", "김기사", 1, 1, 1]],
    )
    _assert_invalid(path, "날짜 헤더는 한 연도")
    _assert_invalid(path, "2026, 2027")


def test_rejects_legacy_month_day_header(tmp_path: Path):
    path = _write_workbook(
        tmp_path / "legacy_header.xlsx",
        delivery_headers=[*FIXED_HEADERS, "6/15"],
        delivery_rows=[["P001", "중식", "김기사", 1, 1]],
    )
    _assert_invalid(path, "6/15")
    _assert_invalid(path, "YYYYMMDD")
