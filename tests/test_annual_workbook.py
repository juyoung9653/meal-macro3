"""실제 xlsx 파일을 사용하는 연간 배송 워크북 어댑터 테스트."""

from datetime import date, datetime
from pathlib import Path
from typing import cast
from zipfile import ZipFile

import msoffcrypto
import openpyxl
import pytest
from msoffcrypto.format.ooxml import OOXMLFile
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table

from src.core.delivery_matrix import (
    DayState,
    DeliveryCellValue,
    EffectiveDeliveryState,
    MonthState,
)
from src.excel._parsing import parse_annual_date_header
from src.excel.annual_workbook import (
    TABLE_NAME,
    AnnualWorkbook,
    WorkbookPasswordError,
    WorkbookPasswordRequiredError,
    WorkbookValidationError,
)

PROFILE_HEADERS = ["성명", "전화번호"]
IDENTITY_PROFILE_HEADERS = [
    "성명",
    "생년월일",
    "핸드폰",
    "도로명주소",
    "관할동",
    "상세주소",
]
CONTROL_HEADERS = ["식종", "배송담당", "연번"]
DEFAULT_HEADERS = [*PROFILE_HEADERS, *CONTROL_HEADERS, "20260728"]
WORKSHEET_NAME = "원본 데이터"


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
    table_name: str | None = TABLE_NAME,
    sheet_name: str = WORKSHEET_NAME,
) -> Path:
    wb = openpyxl.Workbook()
    ws = wb.worksheets[0]
    ws.title = sheet_name

    resolved_headers = DEFAULT_HEADERS if headers is None else headers
    if resolved_headers:
        ws.append(resolved_headers)

    resolved_rows = _person_rows() if rows is None else rows
    for row in resolved_rows:
        ws.append(row)

    if table_name is not None and resolved_headers:
        last_column = get_column_letter(len(resolved_headers))
        last_row = 1 + len(resolved_rows)
        ws.add_table(
            Table(
                displayName=table_name,
                ref=f"A1:{last_column}{last_row}",
            )
        )

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
            ["김영희", "010-2222-2222", "중식", "김기사", 2, None, None, None, None, 1],
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
        "전화번호": "010-1111-1111",
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


def test_reads_table_headers_for_manual_gui_mapping(tmp_path: Path):
    path = _write_workbook(
        tmp_path / "header_preview.xlsx",
        headers=[
            "세대주\n성명",
            "급식 구분",
            "담당 기사",
            "배달 순서",
            date(2026, 7, 28),
        ],
        rows=[],
    )

    assert AnnualWorkbook(path).table_headers() == (
        "세대주 성명",
        "급식 구분",
        "담당 기사",
        "배달 순서",
        "20260728",
    )


def test_loads_user_selected_control_header_mapping(tmp_path: Path):
    path = _write_workbook(
        tmp_path / "manual_header_mapping.xlsx",
        headers=[
            "배달 순서",
            "성명",
            "20260727",
            "담당 기사",
            "전화",
            "20260728",
            "급식 구분",
            "비고",
        ],
        rows=[
            ["001", "홍길동", None, "김기사", "010-1111-1111", None, "조식", "메모"],
            ["001", "홍길동", None, "김기사", "010-1111-1111", 1, "중식", "메모"],
            ["001", "홍길동", None, "김기사", "010-1111-1111", None, "석식", "메모"],
        ],
    )

    adapter = AnnualWorkbook(
        path,
        header_mapping={
            "식종": "급식 구분",
            "배송담당": "담당 기사",
            "연번": "배달 순서",
            "전화번호": "전화",
        },
    )
    book = adapter.load()
    person = book.roster[0]
    lunch = book.get_delivery_row(person, "중식")

    expected_controls = {
        "식종": "급식 구분",
        "배송담당": "담당 기사",
        "연번": "배달 순서",
    }
    assert {
        role: adapter.header_mapping[role] for role in expected_controls
    } == expected_controls
    assert adapter.header_mapping["성명"] == "성명"
    assert lunch is not None
    assert lunch.staff == "김기사"
    assert lunch.sequence_display == "001"
    assert person.extra == {
        "성명": "홍길동",
        "전화번호": "010-1111-1111",
        "비고": "메모",
    }
    assert "급식 구분" not in person.extra
    assert book.dates == (date(2026, 7, 27), date(2026, 7, 28))


def test_maps_every_profile_role_and_allows_omitted_delivery_sequence(
    tmp_path: Path,
):
    profile_roles = (
        "취학유형",
        "학교명",
        "학년",
        "성명",
        "생년월일",
        "세대주 성명",
        "아동과의 관계",
        "관할동",
        "도로명주소",
        "상세주소",
        "전화번호",
        "핸드폰",
        "제공기관",
        "비고",
    )
    source_headers = tuple(f"원본 {role}" for role in profile_roles)
    profile_values = tuple(f"{role} 값" for role in profile_roles)
    path = _write_workbook(
        tmp_path / "all_profile_mapping.xlsx",
        headers=[
            *source_headers,
            "성명",  # 표준명이어도 매핑에서 선택하지 않은 열은 사용하지 않는다.
            "담당 기사",
            "급식 구분",
            "20251231",
            "20260101",
        ],
        rows=[
            [*profile_values, "무시할 성명", "김기사", meal, old, new]
            for meal, old, new in (
                ("조식", 1, None),
                ("중식", None, 1),
                ("석식", None, None),
            )
        ],
    )
    mapping = {
        "식종": "급식 구분",
        "배송담당": "담당 기사",
        "연번": "",
        **dict(zip(profile_roles, source_headers)),
    }

    adapter = AnnualWorkbook(path, header_mapping=mapping, selected_year=2026)
    book = adapter.load()

    assert adapter.selected_year == 2026
    assert book.year == 2026
    assert book.dates == (date(2026, 1, 1),)
    assert book.roster[0].extra == dict(zip(profile_roles, profile_values))
    assert all(row.sequence is None for row in book.delivery_rows)
    assert (
        book.get_delivery_row(book.roster[0], "중식").raw_value_on(
            date(2026, 1, 1)
        )
        is DeliveryCellValue.DELIVERED
    )


def test_loads_without_delivery_sequence_column_or_explicit_mapping(
    tmp_path: Path,
):
    path = _write_workbook(
        tmp_path / "no_sequence_column.xlsx",
        headers=["성명", "전화번호", "식종", "배송담당", "20260728"],
        rows=[
            ["홍길동", "010-1111-1111", "조식", "김기사", None],
            ["홍길동", "010-1111-1111", "중식", "김기사", 1],
            ["홍길동", "010-1111-1111", "석식", "김기사", None],
        ],
    )

    book = AnnualWorkbook(path).load()

    assert all(row.sequence is None for row in book.delivery_rows)


def test_rejects_header_mapping_to_same_source_column(tmp_path: Path):
    path = _write_workbook(tmp_path / "duplicate_header_mapping.xlsx")

    with pytest.raises(ValueError, match="서로 다른 헤더"):
        AnnualWorkbook(
            path,
            header_mapping={
                "식종": "식종",
                "배송담당": "식종",
                "연번": "연번",
            },
        )


def test_reports_missing_manually_mapped_header(tmp_path: Path):
    path = _write_workbook(tmp_path / "missing_mapped_header.xlsx")

    with pytest.raises(WorkbookValidationError, match="급식 구분.*찾을 수 없습니다"):
        AnnualWorkbook(path, header_mapping={"식종": "급식 구분"}).load()


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


def test_matches_decorated_meal_labels_by_substring(tmp_path: Path):
    path = _write_workbook(
        tmp_path / "decorated_meal_labels.xlsx",
        rows=_person_rows(
            meals=(
                "조식(금요일)",
                "석식(월,화,수,목)",
                "2025_겨울중식(월,화,수,목,금)",
            )
        ),
    )

    book = AnnualWorkbook(path).load()

    assert tuple(row.meal for row in book.delivery_rows) == (
        "조식",
        "석식",
        "중식",
    )


def test_rejects_meal_label_containing_multiple_standard_meals(tmp_path: Path):
    path = _write_workbook(
        tmp_path / "ambiguous_meal_label.xlsx",
        rows=_person_rows(meals=("조식/중식", "중식", "석식")),
    )

    _assert_invalid(path, "조식/중식")


def test_preserves_formula_expression_in_mapped_profile_extra(tmp_path: Path):
    path = _write_workbook(
        tmp_path / "profile_formula.xlsx",
        headers=["비고", *CONTROL_HEADERS, "20260728"],
        rows=[
            ["=1+1", "조식", "김기사", "001", None],
            ["=1+1", "중식", "김기사", "001", 1],
            ["=1+1", "석식", "김기사", "001", None],
        ],
    )

    book = AnnualWorkbook(path).load()

    assert book.roster[0].extra["비고"] == "=1+1"


def test_allows_note_to_differ_across_meal_rows_and_keeps_nonblank_note(
    tmp_path: Path,
):
    note = "2025 겨울방학\n지원 대상자\n(심의필요)"
    path = _write_workbook(
        tmp_path / "row_specific_note.xlsx",
        headers=["성명", "비고", *CONTROL_HEADERS, "20260728"],
        rows=[
            ["홍길동", None, "조식", "김기사", "001", None],
            ["홍길동", None, "중식", "김기사", "001", 1],
            ["홍길동", note, "석식", "김기사", "001", None],
        ],
    )

    book = AnnualWorkbook(path).load()

    assert book.roster[0].extra["비고"] == note


def test_combines_distinct_notes_across_meal_rows_without_duplicates(
    tmp_path: Path,
):
    path = _write_workbook(
        tmp_path / "multiple_row_notes.xlsx",
        headers=["성명", "비고", *CONTROL_HEADERS, "20260728"],
        rows=[
            ["홍길동", "공통 메모", "조식", "김기사", "001", None],
            ["홍길동", "공통 메모", "중식", "김기사", "001", 1],
            ["홍길동", "석식 메모", "석식", "김기사", "001", None],
        ],
    )

    book = AnnualWorkbook(path).load()

    assert book.roster[0].extra["비고"] == "공통 메모\n석식 메모"


def test_ignores_unmapped_calculation_columns_when_formulas_differ_by_row(
    tmp_path: Path,
):
    path = _write_workbook(
        tmp_path / "unmapped_monthly_sum.xlsx",
        headers=["성명", "8월SUM", *CONTROL_HEADERS, "20260728"],
        rows=[
            ["홍길동", "=SUM(Y2:BB2)", "조식", "김기사", "001", None],
            ["홍길동", "=SUM(Y3:BB3)", "중식", "김기사", "001", 1],
            ["홍길동", "=SUM(Y4:BB4)", "석식", "김기사", "001", None],
        ],
    )

    book = AnnualWorkbook(path).load()

    assert book.roster[0].extra == {"성명": "홍길동"}


def test_ignores_formula_only_rows_in_unmapped_columns(tmp_path: Path):
    path = _write_workbook(
        tmp_path / "unmapped_formula_only_tail.xlsx",
        headers=["성명", "8월SUM", *CONTROL_HEADERS, "20260728"],
        rows=[
            ["홍길동", "=SUM(G2:G2)", "중식", "김기사", "001", 1],
            [None, "=SUM(G3:G3)", None, None, None, None],
        ],
    )

    book = AnnualWorkbook(path).load()

    assert tuple(person.display_name for person in book.roster) == ("홍길동",)
    assert len(book.delivery_rows) == 1


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


def test_accepts_numeric_zero_as_explicit_pause(tmp_path: Path):
    path = _write_workbook(
        tmp_path / "numeric_zero.xlsx",
        rows=_person_rows(date_values=[[0], [None], [None]]),
    )

    book = AnnualWorkbook(path).load()
    person = book.roster[0]

    assert (
        book.raw_delivery_value(person, "조식", date(2026, 7, 28))
        is DeliveryCellValue.PAUSED
    )
    assert (
        book.effective_delivery_state(person, "조식", date(2026, 7, 28))
        is EffectiveDeliveryState.PAUSED
    )
    assert book.month_state(2026, 7) is MonthState.ENTERED
    assert book.day_state(date(2026, 7, 28)) is DayState.ENTERED


def test_loads_password_protected_workbook(tmp_path: Path):
    plain_path = _write_workbook(tmp_path / "plain.xlsx")
    encrypted_path = _encrypt_workbook(
        plain_path, tmp_path / "encrypted.xlsx", password="secret"
    )

    with pytest.raises(WorkbookPasswordRequiredError, match="비밀번호"):
        AnnualWorkbook(encrypted_path).load()

    adapter = AnnualWorkbook(encrypted_path, password="secret")
    book = adapter.load()
    assert book.year == 2026
    assert book.get_delivery_row(book.roster[0], "중식") is not None
    adapter.forget_password()
    assert adapter.password == ""
    assert adapter.load() is book

    with pytest.raises(WorkbookPasswordError, match="비밀번호"):
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


def test_corrupt_file_error_mentions_corruption_without_requesting_password(
    tmp_path: Path,
):
    path = tmp_path / "corrupt.xlsx"
    path.write_bytes(b"not an xlsx file")

    with pytest.raises(WorkbookValidationError) as exc_info:
        AnnualWorkbook(path).load()
    message = str(exc_info.value)
    assert "손상" in message
    assert not isinstance(exc_info.value, WorkbookPasswordRequiredError)


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
    wb[WORKSHEET_NAME].cell(row=1_048_576, column=16_384).number_format = "0"
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
    ws = wb[WORKSHEET_NAME]
    for row_number, values in enumerate(_person_rows(), start=100_001):
        for column, value in enumerate(values, start=1):
            ws.cell(row=row_number, column=column, value=value)
    ws.tables[TABLE_NAME].ref = "A1:F100003"
    wb.save(path)
    wb.close()

    book = AnnualWorkbook(path).load()
    person = book.roster[0]
    assert person.source_row == 100_001
    assert book.get_delivery_row(person, "중식") is not None


def test_ignores_nonblank_value_outside_db_table(tmp_path: Path):
    path = _write_workbook(tmp_path / "value_beyond_header.xlsx")
    wb = openpyxl.load_workbook(path)
    wb[WORKSHEET_NAME]["G2"] = "테이블 밖 값"
    wb.save(path)
    wb.close()

    book = AnnualWorkbook(path).load()

    assert tuple(person.display_name for person in book.roster) == ("홍길동",)


def test_validation_keeps_excel_row_number_after_blank_row(tmp_path: Path):
    path = _write_workbook(
        tmp_path / "row_number.xlsx",
        rows=[
            [None] * len(DEFAULT_HEADERS),
            *_person_rows(date_values=[[2], [None], [None]]),
        ],
    )
    _assert_invalid(path, "3행")
    _assert_invalid(path, "1, 0 또는 빈칸(null)")


def test_requires_db_table_in_selected_workbook(tmp_path: Path):
    path = _write_workbook(
        tmp_path / "missing_db_table.xlsx",
        table_name=None,
    )
    _assert_invalid(path, "이름이 'db'인 표(테이블)가 없습니다")


def test_rejects_headers_without_dates(tmp_path: Path):
    path = _write_workbook(
        tmp_path / "no_date_headers.xlsx",
        headers=[*PROFILE_HEADERS, *CONTROL_HEADERS],
        rows=[],
    )
    _assert_invalid(path, "YYYYMMDD 날짜 헤더가 하나 이상")


@pytest.mark.parametrize(
    "wrong_table_name",
    ["명단", f"{TABLE_NAME}_backup"],
)
def test_requires_named_db_table(
    tmp_path: Path,
    wrong_table_name: str,
):
    path = _write_workbook(
        tmp_path / "wrong_table_name.xlsx",
        table_name=wrong_table_name,
    )

    _assert_invalid(path, "이름이 'db'인 표(테이블)가 없습니다")


def test_finds_db_table_on_any_sheet_and_ignores_other_sheets(tmp_path: Path):
    path = _write_workbook(
        tmp_path / "extra_sheet.xlsx",
        sheet_name="사용자가 정한 시트명",
    )
    wb = openpyxl.load_workbook(path)
    business = wb.create_sheet("일일식수매크로", 0)
    business["A1"] = "별도 형식은 파싱하지 않음"
    backup = wb.create_sheet("백업")
    backup.append(["무시할 값"])
    backup.append(["무시할 값"])
    backup.add_table(Table(displayName="db_backup", ref="A1:A2"))
    wb.save(path)
    wb.close()

    book = AnnualWorkbook(path).load()

    assert book.year == 2026
    assert tuple(person.display_name for person in book.roster) == ("홍길동",)


def test_reads_only_db_table_range_at_non_a1_position(tmp_path: Path):
    path = tmp_path / "offset_db_table.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.worksheets[0]
    ws.title = "운영 시트"
    ws["A1"] = "테이블 밖 머리말"
    ws["C4"] = "성명"
    ws["D4"] = "전화번호"
    ws["E4"] = "식종"
    ws["F4"] = "배송담당"
    ws["G4"] = "연번"
    ws["H4"] = "20260728"
    for row_number, values in enumerate(_person_rows(), start=5):
        for column, value in enumerate(values, start=3):
            ws.cell(row=row_number, column=column, value=value)
    ws["C9"] = "테이블 밖 잘못된 행"
    ws["H9"] = 2
    ws.add_table(Table(displayName=TABLE_NAME, ref="C4:H7"))
    wb.save(path)
    wb.close()

    book = AnnualWorkbook(path).load()

    assert tuple(person.display_name for person in book.roster) == ("홍길동",)
    assert book.roster[0].source_row == 5


def test_ignores_unmapped_id_header(tmp_path: Path):
    path = _write_workbook(
        tmp_path / "id_header.xlsx",
        headers=["ID", *PROFILE_HEADERS, *CONTROL_HEADERS, "20260728"],
        rows=[
            [
                "외부-001",
                "홍길동",
                "010-1111-1111",
                "중식",
                "김기사",
                "001",
                1,
            ]
        ],
    )

    book = AnnualWorkbook(path).load()

    assert tuple(person.display_name for person in book.roster) == ("홍길동",)
    assert "ID" not in book.roster[0].extra


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


def test_accepts_control_headers_in_any_order(tmp_path: Path):
    path = _write_workbook(
        tmp_path / "control_header_order.xlsx",
        headers=["성명", "배송담당", "식종", "연번", "20260728"],
        rows=[],
    )

    book = AnnualWorkbook(path).load()

    assert book.year == 2026


def test_accepts_date_header_anywhere_in_table(tmp_path: Path):
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

    book = AnnualWorkbook(path).load()

    assert book.dates == (date(2026, 7, 27), date(2026, 7, 28))


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


def test_accepts_person_with_only_selected_meal_rows(tmp_path: Path):
    path = _write_workbook(
        tmp_path / "incomplete_person.xlsx",
        rows=_person_rows()[:2],
    )

    book = AnnualWorkbook(path).load()

    assert len(book.roster) == 1
    person = book.roster[0]
    assert person.source_row == 2
    assert person.source_end_row == 3
    assert tuple(row.meal for row in book.rows_by_person[person]) == (
        "조식",
        "중식",
    )


def test_same_name_and_birth_merge_even_when_ward_is_blank(tmp_path: Path):
    headers = [*IDENTITY_PROFILE_HEADERS, *CONTROL_HEADERS, "20260728"]
    rows = [
        [
            "홍길동",
            "2015-03-02",
            "010-1111-1111",
            "성남대로 1",
            "정자동",
            "101호",
            "조식",
            "김기사",
            "001",
            None,
        ],
        [
            "홍길동",
            20150302,
            "01011111111",
            "성남대로 1",
            None,
            "101호",
            "중식",
            "김기사",
            "001",
            1,
        ],
        [
            "홍길동",
            date(2015, 3, 2),
            None,
            "성남대로 1",
            "정자동",
            None,
            "석식",
            "김기사",
            "001",
            None,
        ],
    ]
    path = _write_workbook(
        tmp_path / "noncontiguous_person.xlsx",
        headers=headers,
        rows=[rows[0], [None] * len(headers), rows[1], rows[2]],
    )

    book = AnnualWorkbook(path).load()

    assert len(book.roster) == 1
    person = book.roster[0]
    assert person.source_rows == (2, 4, 5)
    assert tuple(row.meal for row in book.rows_by_person[person]) == (
        "조식",
        "중식",
        "석식",
    )


def test_nonconsecutive_matching_identity_merges_across_other_people(
    tmp_path: Path,
):
    path = _write_workbook(
        tmp_path / "interleaved_people.xlsx",
        headers=[*IDENTITY_PROFILE_HEADERS, *CONTROL_HEADERS, "20260728"],
        rows=[
            [
                "홍길동",
                "2015-03-02",
                "010-1111-1111",
                "성남대로 1",
                "정자동",
                "101호",
                "조식",
                "김기사",
                1,
                1,
            ],
            [
                "김영희",
                "2016-04-03",
                "010-3333-3333",
                "성남대로 2",
                "야탑동",
                "201호",
                "중식",
                "이기사",
                2,
                1,
            ],
            [
                "홍길동",
                20150302,
                "01011111111",
                "성남대로 1",
                "정자동",
                "101호",
                "중식",
                "김기사",
                1,
                1,
            ],
            [
                "홍길동",
                date(2015, 3, 2),
                "010-1111-1111",
                "성남대로 1",
                "정자동",
                "101호",
                "석식",
                "김기사",
                1,
                1,
            ],
        ],
    )

    book = AnnualWorkbook(path).load()

    assert tuple(person.display_name for person in book.roster) == (
        "홍길동",
        "김영희",
    )
    hong = book.roster[0]
    assert hong.source_rows == (2, 4, 5)
    assert tuple(row.meal for row in book.rows_by_person[hong]) == (
        "조식",
        "중식",
        "석식",
    )


def test_same_phone_does_not_merge_different_birth_dates(
    tmp_path: Path,
):
    path = _write_workbook(
        tmp_path / "same_name_different_identity.xlsx",
        headers=[*IDENTITY_PROFILE_HEADERS, *CONTROL_HEADERS, "20260728"],
        rows=[
            [
                "홍길동",
                "2015-03-02",
                "010-1111-1111",
                "성남대로 1",
                "정자동",
                "101호",
                "조식",
                "김기사",
                1,
                1,
            ],
            [
                "홍길동",
                20150302,
                "010-2222-2222",
                "성남대로 1",
                "정자동",
                "101호",
                "중식",
                "김기사",
                2,
                1,
            ],
            [
                "홍길동",
                "2016-04-03",
                "010-1111-1111",
                "성남대로 1",
                "정자동",
                "101호",
                "석식",
                "김기사",
                3,
                1,
            ],
        ],
    )

    book = AnnualWorkbook(path).load()

    assert len(book.roster) == 2
    first = book.roster[0]
    second = book.roster[1]
    assert first.source_rows == (2, 3)
    assert tuple(row.meal for row in book.rows_by_person[first]) == (
        "조식",
        "중식",
    )
    assert second.source_rows == (4,)
    assert tuple(row.meal for row in book.rows_by_person[second]) == ("석식",)
    assert book.profile_conflicts == ()


def test_birth_and_resident_number_with_same_first_six_digits_do_not_conflict(
    tmp_path: Path,
):
    path = _write_workbook(
        tmp_path / "birth_and_resident_number.xlsx",
        headers=[*IDENTITY_PROFILE_HEADERS, *CONTROL_HEADERS, "20260728"],
        rows=[
            [
                "홍길동",
                "150302",
                "010-1111-1111",
                "성남대로 1",
                "정자동",
                "101호",
                "조식",
                "김기사",
                1,
                1,
            ],
            [
                "홍길동",
                "150302-3123456",
                "010-2222-2222",
                "성남대로 1",
                "정자동",
                "101호",
                "중식",
                "김기사",
                1,
                1,
            ],
        ],
    )

    book = AnnualWorkbook(path).load()

    assert len(book.roster) == 1
    assert book.roster[0].source_rows == (2, 3)
    assert book.profile_conflicts == ()


@pytest.mark.parametrize(
    ("first_birth", "second_birth", "expected_people"),
    [
        ("1503021234567", "1503027654321", 2),
        ("20150302", "2015031234567", 1),
    ],
)
def test_same_name_birth_uses_full_value_only_when_long_lengths_match(
    tmp_path: Path,
    first_birth: str,
    second_birth: str,
    expected_people: int,
):
    path = _write_workbook(
        tmp_path / f"birth_length_rule_{expected_people}.xlsx",
        headers=[*IDENTITY_PROFILE_HEADERS, *CONTROL_HEADERS, "20260728"],
        rows=[
            [
                "홍길동",
                first_birth,
                "010-1111-1111",
                "성남대로 1",
                "정자동",
                "101호",
                "조식",
                "김기사",
                1,
                1,
            ],
            [
                "홍길동",
                second_birth,
                "010-1111-1111",
                "성남대로 1",
                "정자동",
                "101호",
                "중식",
                "김기사",
                1,
                1,
            ],
        ],
    )

    book = AnnualWorkbook(path).load()

    assert len(book.roster) == expected_people


def test_same_name_and_birth_ignore_ward_and_address_for_grouping(
    tmp_path: Path,
):
    path = _write_workbook(
        tmp_path / "moved_person.xlsx",
        headers=[*IDENTITY_PROFILE_HEADERS, *CONTROL_HEADERS, "20260728"],
        rows=[
            [
                "홍길동",
                "2015-03-02",
                "010-1111-1111",
                "성남대로 1",
                "정자동",
                "101호",
                "조식",
                "김기사",
                1,
                1,
            ],
            [
                "홍길동",
                20150302,
                "010-1111-1111",
                "성남대로 1",
                "야탑동",
                "101호",
                "중식",
                "김기사",
                2,
                1,
            ],
            [
                "홍길동",
                date(2015, 3, 2),
                "010-1111-1111",
                "성남대로 1",
                "야탑동",
                "201호",
                "석식",
                "김기사",
                3,
                1,
            ],
            [
                "홍길동",
                "2015-03-02",
                "010-1111-1111",
                "성남대로 2",
                "서현동",
                "301호",
                "조식",
                "김기사",
                4,
                1,
            ],
        ],
    )

    book = AnnualWorkbook(path).load()

    assert len(book.roster) == 1
    assert book.roster[0].source_rows == (2, 3, 4, 5)
    assert book.profile_conflicts == ()


def test_road_address_containment_counts_as_same_address(tmp_path: Path):
    path = _write_workbook(
        tmp_path / "partially_matching_road_address.xlsx",
        headers=[*IDENTITY_PROFILE_HEADERS, *CONTROL_HEADERS, "20260728"],
        rows=[
            [
                "홍길동",
                "2015-03-02",
                "010-1111-1111",
                "경기도 성남시 분당구 성남대로 1",
                "정자동",
                "101호",
                "조식",
                "김기사",
                1,
                1,
            ],
            [
                "홍길동",
                20150302,
                "01011111111",
                "성남대로 1",
                "정자동",
                "101호",
                "중식",
                "김기사",
                1,
                1,
            ],
        ],
    )

    book = AnnualWorkbook(path).load()

    assert len(book.roster) == 1
    person = book.roster[0]
    assert tuple(row.meal for row in book.rows_by_person[person]) == (
        "조식",
        "중식",
    )
    assert book.profile_conflicts == ()


def test_different_names_are_separate_one_row_people(tmp_path: Path):
    path = _write_workbook(
        tmp_path / "one_row_people.xlsx",
        rows=[
            ["김아침", "010-1111-1111", "조식(금요일)", "김기사", 1, 1],
            ["이저녁", "010-2222-2222", "석식(월,화,수,목)", "이기사", 2, 1],
            [
                "박점심",
                "010-3333-3333",
                "2025_겨울중식(월,화,수,목,금)",
                "박기사",
                3,
                1,
            ],
        ],
    )

    book = AnnualWorkbook(path).load()

    assert tuple(person.display_name for person in book.roster) == (
        "김아침",
        "이저녁",
        "박점심",
    )
    assert tuple(
        book.rows_by_person[person][0].meal for person in book.roster
    ) == ("조식", "석식", "중식")
    assert all(len(book.rows_by_person[person]) == 1 for person in book.roster)


def test_same_name_meal_rows_merge_despite_other_profile_differences(
    tmp_path: Path,
):
    path = _write_workbook(
        tmp_path / "same_name_different_profiles.xlsx",
        headers=[
            "도로명주소",
            "성명",
            "세대주 성명",
            "생년월일",
            "핸드폰",
            "학교명",
            "전화번호",
            "비고",
            *CONTROL_HEADERS,
            "20260728",
        ],
        rows=[
            [
                "성남대로 1",
                " 홍길동 ",
                "홍보호",
                "2015-03-02",
                "010-1234-5678",
                None,
                None,
                "첫 비고",
                "조식(금요일)",
                "김기사",
                "001",
                1,
            ],
            [
                "성남대로 1",
                "홍길동",
                "홍보호",
                20150302,
                "010-1234-5678",
                "분당초",
                "010-1111-1111",
                None,
                "2025_겨울중식(월,화,수,목,금)",
                "김기사",
                "001",
                1,
            ],
            [
                "성남대로 1",
                "홍길동",
                "홍보호",
                date(2015, 3, 2),
                "010-1234-5678",
                "다른 학교 표기",
                "010-9999-9999",
                "둘째 비고",
                "석식(월,화,수,목)",
                "김기사",
                "001",
                1,
            ],
        ],
    )

    book = AnnualWorkbook(path).load()

    assert len(book.roster) == 1
    person = book.roster[0]
    assert (person.source_row, person.source_end_row) == (2, 4)
    assert person.extra["학교명"] == "분당초"
    assert person.extra["전화번호"] == "010-1111-1111"
    assert person.extra["비고"] == "첫 비고\n둘째 비고"
    assert len(book.profile_conflicts) == 2
    assert tuple(item.field for item in book.profile_conflicts) == (
        "학교명",
        "전화번호",
    )
    assert book.profile_conflicts[0].selected_value == "분당초"
    assert book.profile_conflicts[0].values_by_row == (
        (3, "분당초"),
        (4, "다른 학교 표기"),
    )
    assert tuple(row.meal for row in book.rows_by_person[person]) == (
        "조식",
        "중식",
        "석식",
    )


def test_numeric_and_text_grade_are_not_reported_as_profile_conflict(
    tmp_path: Path,
):
    path = _write_workbook(
        tmp_path / "equivalent_grade_types.xlsx",
        headers=[
            *IDENTITY_PROFILE_HEADERS,
            "학년",
            *CONTROL_HEADERS,
            "20260728",
        ],
        rows=[
            [
                "홍길동",
                "2015-03-02",
                "010-1111-1111",
                "성남대로 1",
                "정자동",
                "101호",
                1,
                "조식",
                "김기사",
                1,
                1,
            ],
            [
                "홍길동",
                20150302,
                "01011111111",
                "성남대로 1",
                "정자동",
                "101호",
                "1",
                "중식",
                "김기사",
                1,
                1,
            ],
            [
                "홍길동",
                date(2015, 3, 2),
                None,
                "성남대로 1",
                "정자동",
                "101호",
                1.0,
                "석식",
                "김기사",
                1,
                1,
            ],
        ],
    )

    book = AnnualWorkbook(path).load()

    assert len(book.roster) == 1
    assert book.roster[0].extra["학년"] == 1
    assert book.profile_conflicts == ()


@pytest.mark.parametrize("invalid_meal", ["야식", ""])
def test_rejects_row_without_exactly_one_standard_meal(
    tmp_path: Path,
    invalid_meal: str,
):
    path = _write_workbook(
        tmp_path / f"invalid_meals_{invalid_meal or 'blank'}.xlsx",
        rows=_person_rows(meals=(invalid_meal, "중식", "석식")),
    )
    _assert_invalid(path, "각 식종 값에는 조식, 중식, 석식 중 하나")


def test_merges_duplicate_meal_rows_for_same_person(tmp_path: Path):
    path = _write_workbook(
        tmp_path / "duplicate_person_meal.xlsx",
        headers=[
            *PROFILE_HEADERS,
            *CONTROL_HEADERS,
            "20260728",
            "20260729",
            "20260730",
        ],
        rows=[
            [
                "홍길동",
                "010-1111-1111",
                "조식(금요일)",
                "김기사",
                "001",
                1,
                None,
                None,
            ],
            [
                "홍길동",
                "010-1111-1111",
                "석식(월,화,수,목)",
                "김기사",
                "001",
                1,
                None,
                0,
            ],
            [
                "홍길동",
                "010-1111-1111",
                "석식(월,화,수,목)",
                "다른 입력값",
                "999",
                None,
                1,
                None,
            ],
        ],
    )

    book = AnnualWorkbook(path).load()

    assert len(book.roster) == 1
    person = book.roster[0]
    assert tuple(row.meal for row in book.rows_by_person[person]) == (
        "조식",
        "석식",
    )
    dinner = book.get_delivery_row(person, "석식")
    assert dinner is not None
    assert dinner.staff == "김기사"
    assert dinner.sequence_display == "001"
    assert dinner.raw_value_on(date(2026, 7, 28)) is DeliveryCellValue.DELIVERED
    assert dinner.raw_value_on(date(2026, 7, 29)) is DeliveryCellValue.DELIVERED
    assert dinner.raw_value_on(date(2026, 7, 30)) is DeliveryCellValue.PAUSED
    assert book.monthly_delivery_count(person, "석식", 2026, 7) == 2


def test_preserves_explicit_delivered_paused_and_blank_values(tmp_path: Path):
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
                [None, 1, None],
                [1, 0, 1],
                [None, None, None],
            ]
        ),
    )

    book = AnnualWorkbook(path).load()
    row = book.get_delivery_row(book.roster[0], "중식")
    assert row is not None
    assert row.raw_value_on(date(2026, 7, 28)) is DeliveryCellValue.DELIVERED
    assert row.raw_value_on(date(2026, 7, 29)) is DeliveryCellValue.PAUSED
    assert (
        book.effective_delivery_state(
            book.roster[0], "중식", date(2026, 7, 29)
        )
        is EffectiveDeliveryState.PAUSED
    )
    assert row.raw_value_on(date(2026, 7, 30)) is DeliveryCellValue.DELIVERED


@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [
        (1, DeliveryCellValue.DELIVERED),
        ("1", DeliveryCellValue.DELIVERED),
        (0, DeliveryCellValue.PAUSED),
        ("0", DeliveryCellValue.PAUSED),
        (None, DeliveryCellValue.BLANK),
        ("`", DeliveryCellValue.BLANK),
        ("'", DeliveryCellValue.BLANK),
        ("null", DeliveryCellValue.BLANK),
        ("NULL", DeliveryCellValue.BLANK),
        ("none", DeliveryCellValue.BLANK),
    ],
)
def test_normalizes_common_three_state_delivery_values(
    tmp_path: Path,
    raw_value: object,
    expected: DeliveryCellValue,
):
    path = _write_workbook(
        tmp_path / f"normalized_{expected.name}_{raw_value!s}.xlsx",
        rows=_person_rows(date_values=[[raw_value], [None], [None]]),
    )

    book = AnnualWorkbook(path).load()

    assert (
        book.raw_delivery_value(book.roster[0], "조식", date(2026, 7, 28))
        is expected
    )


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
        assert cached_workbook[WORKSHEET_NAME]["F2"].value == 1
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


@pytest.mark.parametrize("invalid_value", ["O", "X", "Y", 2, "2", True, False])
def test_rejects_invalid_delivery_cell_values(tmp_path: Path, invalid_value):
    path = _write_workbook(
        tmp_path / f"invalid_value_{invalid_value}.xlsx",
        rows=_person_rows(date_values=[[invalid_value], [None], [None]]),
    )
    _assert_invalid(path, "1, 0 또는 빈칸(null)")
    _assert_invalid(path, repr(invalid_value))


def test_mixed_year_date_headers_require_and_accept_gui_year_selection(
    tmp_path: Path,
):
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
    _assert_invalid(path, "GUI에서 사용할 연도를 선택")
    _assert_invalid(path, "2026, 2027")

    adapter = AnnualWorkbook(path, selected_year=2027)
    book = adapter.load()

    assert adapter.selected_year == 2027
    assert book.year == 2027
    assert book.dates == (date(2027, 1, 1),)
    assert all(tuple(row.values) == (date(2027, 1, 1),) for row in book.delivery_rows)


def test_rejects_selected_year_without_date_header(tmp_path: Path):
    path = _write_workbook(tmp_path / "missing_selected_year.xlsx")

    with pytest.raises(WorkbookValidationError, match="2025년 날짜 헤더가 없습니다"):
        AnnualWorkbook(path, selected_year=2025).load()


def test_does_not_treat_legacy_month_day_header_as_date(tmp_path: Path):
    path = _write_workbook(
        tmp_path / "legacy_header.xlsx",
        headers=[*PROFILE_HEADERS, *CONTROL_HEADERS, "6/15"],
        rows=_person_rows(),
    )
    _assert_invalid(path, "YYYYMMDD")
