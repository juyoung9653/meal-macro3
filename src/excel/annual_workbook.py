"""단일 명단 시트의 연간 배송 Excel을 검증해 도메인으로 변환한다."""

from __future__ import annotations

import math
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import date
from io import BytesIO
from pathlib import Path
from stat import S_ISREG
from typing import Any, cast
from zipfile import BadZipFile

import msoffcrypto
import openpyxl
from msoffcrypto.exceptions import (
    DecryptionError,
    FileFormatError,
    InvalidKeyError,
    ParseError,
)
from openpyxl.utils import get_column_letter
from openpyxl.utils.exceptions import InvalidFileException
from openpyxl.workbook.workbook import Workbook
from openpyxl.worksheet._read_only import ReadOnlyWorksheet
from openpyxl.worksheet._reader import WorkSheetParser
from openpyxl.worksheet.formula import ArrayFormula, DataTableFormula

from ..core.delivery_matrix import (
    ALLOWED_MEALS,
    AnnualDeliveryBook,
    DeliveryCellValue,
    DeliveryRow,
    RosterEntry,
)
from ._parsing import parse_annual_date_header

SHEET_NAME = "명단"
CONTROL_HEADERS = ("식종", "배송담당", "연번")

_SparseRow = dict[int, Any]
_MaterializedRow = tuple[int, tuple[Any, ...]]


class WorkbookValidationError(ValueError):
    """연간 배송 워크북의 계약 위반을 사용자에게 설명하는 오류."""


class AnnualWorkbook:
    """xlsx 로드, 선택적 암호 해제, 검증 및 도메인 객체 캐시를 담당한다."""

    def __init__(self, path: str | Path, password: str | None = None):
        self._path = Path(path)
        self._password = password or ""
        self._cached_book: AnnualDeliveryBook | None = None

    @property
    def path(self) -> Path:
        return self._path

    @path.setter
    def path(self, value: str | Path) -> None:
        new_path = Path(value)
        if new_path != self._path:
            self._path = new_path
            self.clear_cache()

    @property
    def password(self) -> str:
        return self._password

    @password.setter
    def password(self, value: str | None) -> None:
        new_password = value or ""
        if new_password != self._password:
            self._password = new_password
            self.clear_cache()

    def load(self, *, force: bool = False) -> AnnualDeliveryBook:
        if self._cached_book is not None and not force:
            return self._cached_book

        with self._open_workbook() as workbook:
            book = self._parse_workbook(workbook)
        self._cached_book = book
        return book

    def clear_cache(self) -> None:
        self._cached_book = None

    @contextmanager
    def _open_workbook(self) -> Iterator[Workbook]:
        try:
            file_stat = self._path.stat()
        except FileNotFoundError as error:
            raise FileNotFoundError(
                f"연간 배송 Excel 파일이 없습니다: {self._path}"
            ) from error
        if not S_ISREG(file_stat.st_mode):
            raise WorkbookValidationError(
                f"연간 배송 Excel 경로가 파일이 아닙니다: {self._path}"
            )

        try:
            workbook = openpyxl.load_workbook(
                self._path, data_only=False, read_only=True
            )
        except OSError:
            raise
        except (
            BadZipFile,
            InvalidFileException,
            KeyError,
            ValueError,
        ) as direct_error:
            if not self._password:
                raise WorkbookValidationError(
                    "Excel 파일을 해석할 수 없습니다. 파일이 손상되었거나 "
                    f"암호로 보호되어 있습니다: {self._path}"
                ) from direct_error
        else:
            try:
                yield workbook
            finally:
                workbook.close()
            return

        decrypted = BytesIO()
        try:
            try:
                with self._path.open("rb") as encrypted:
                    office_file = msoffcrypto.OfficeFile(encrypted)
                    office_file.load_key(password=self._password)
                    office_file.decrypt(decrypted)
                decrypted.seek(0)
                workbook = openpyxl.load_workbook(
                    decrypted, data_only=False, read_only=True
                )
            except OSError:
                raise
            except (
                BadZipFile,
                DecryptionError,
                FileFormatError,
                InvalidFileException,
                InvalidKeyError,
                KeyError,
                ParseError,
                ValueError,
            ) as decrypt_error:
                raise WorkbookValidationError(
                    "Excel 파일 암호 해제에 실패했습니다. 암호가 올바르지 "
                    f"않거나 파일이 손상되었습니다: {self._path}"
                ) from decrypt_error

            try:
                yield workbook
            finally:
                workbook.close()
        finally:
            decrypted.close()

    def _parse_workbook(self, workbook: Workbook) -> AnnualDeliveryBook:
        if SHEET_NAME not in workbook.sheetnames:
            raise WorkbookValidationError(
                "필수 시트 '명단'이 없습니다. 새 형식은 명단 시트 하나에 "
                "개인정보와 식종별 배송현황을 함께 입력합니다."
            )
        if workbook.sheetnames != [SHEET_NAME]:
            extras = ", ".join(
                name for name in workbook.sheetnames if name != SHEET_NAME
            )
            raise WorkbookValidationError(
                "새 형식은 '명단' 시트 하나만 사용합니다. 별도 배송현황 "
                f"시트를 제거하세요. 추가 시트: {extras or '(없음)'}"
            )

        worksheet = cast(ReadOnlyWorksheet, workbook[SHEET_NAME])
        roster, delivery_rows, dates, year = _parse_roster_matrix(worksheet)
        try:
            return AnnualDeliveryBook(
                year=year,
                roster=roster,
                delivery_rows=delivery_rows,
                dates=dates,
            )
        except (TypeError, ValueError) as error:
            raise WorkbookValidationError(
                f"연간 배송 도메인 구성에 실패했습니다: {error}"
            ) from error


def _parse_roster_matrix(
    worksheet: ReadOnlyWorksheet,
) -> tuple[
    tuple[RosterEntry, ...],
    tuple[DeliveryRow, ...],
    tuple[date, ...],
    int,
]:
    roster: list[RosterEntry] = []
    delivery_rows: list[DeliveryRow] = []

    with _open_nonblank_rows(worksheet) as rows:
        raw_headers = _read_header(rows, SHEET_NAME)
        profile_columns, control_columns, date_columns = _validate_headers(raw_headers)
        dates = tuple(sorted(date_columns))
        years = sorted({day.year for day in dates})
        if len(years) != 1:
            raise WorkbookValidationError(
                "명단 날짜 헤더는 한 연도여야 합니다. 발견 연도: "
                + ", ".join(str(year) for year in years)
            )

        group: list[_MaterializedRow] = []
        for row_number, sparse_row in rows:
            row = _materialize_row(
                sparse_row,
                len(raw_headers),
                SHEET_NAME,
                row_number,
            )
            group.append((row_number, row))
            if len(group) == len(ALLOWED_MEALS):
                person, person_rows = _parse_person_group(
                    group,
                    profile_columns=profile_columns,
                    control_columns=control_columns,
                    date_columns=date_columns,
                )
                roster.append(person)
                delivery_rows.extend(person_rows)
                group.clear()

        if group:
            row_numbers = ", ".join(str(row_number) for row_number, _row in group)
            raise WorkbookValidationError(
                "명단 데이터 행은 한 사람당 조식·중식·석식 3행씩 있어야 "
                f"합니다. 마지막 묶음이 {len(group)}행뿐입니다: {row_numbers}행"
            )

    return tuple(roster), tuple(delivery_rows), dates, years[0]


def _parse_person_group(
    group: Sequence[_MaterializedRow],
    *,
    profile_columns: Sequence[tuple[str, int]],
    control_columns: dict[str, int],
    date_columns: dict[date, int],
) -> tuple[RosterEntry, tuple[DeliveryRow, ...]]:
    row_numbers = tuple(row_number for row_number, _row in group)
    expected_rows = tuple(range(row_numbers[0], row_numbers[0] + len(ALLOWED_MEALS)))
    if row_numbers != expected_rows:
        shown = ", ".join(str(row_number) for row_number in row_numbers)
        raise WorkbookValidationError(
            "한 사람의 조식·중식·석식 행은 빈 행 없이 연속되어야 합니다. "
            f"확인할 행: {shown}행"
        )

    profiles = tuple(
        {
            header: _cell(row, column)
            for header, column in profile_columns
        }
        for _row_number, row in group
    )
    first_profile = profiles[0]
    for index, profile in enumerate(profiles[1:], start=1):
        for header, first_value in first_profile.items():
            other_value = profile[header]
            if _same_profile_value(first_value, other_value):
                continue
            raise WorkbookValidationError(
                f"명단 {row_numbers[0]}~{row_numbers[-1]}행: 한 사람의 3행은 "
                f"인적사항이 같아야 합니다. '{header}' 값이 "
                f"{row_numbers[0]}행={first_value!r}, "
                f"{row_numbers[index]}행={other_value!r}로 다릅니다."
            )

    meal_column = control_columns["식종"]
    meals = tuple(_text(_cell(row, meal_column)) for _row_number, row in group)
    if len(set(meals)) != len(ALLOWED_MEALS) or set(meals) != set(ALLOWED_MEALS):
        shown = ", ".join(meal or "빈칸" for meal in meals)
        raise WorkbookValidationError(
            f"명단 {row_numbers[0]}~{row_numbers[-1]}행: 식종은 조식, 중식, "
            f"석식을 각각 한 번씩 입력해야 합니다. 현재값: {shown}"
        )

    person = RosterEntry(source_row=row_numbers[0], extra=first_profile)
    parsed_rows: list[DeliveryRow] = []
    for (row_number, row), meal in zip(group, meals):
        values = {
            day: _parse_delivery_value(
                _cell(row, column),
                row_number=row_number,
                day=day,
            )
            for day, column in date_columns.items()
        }
        parsed_rows.append(
            DeliveryRow(
                person=person,
                meal=meal,
                staff=_text(_cell(row, control_columns["배송담당"])),
                sequence=_cell(row, control_columns["연번"]),
                values=values,
                source_row=row_number,
            )
        )

    return person, tuple(parsed_rows)


def _validate_headers(
    raw_headers: list[Any],
) -> tuple[list[tuple[str, int]], dict[str, int], dict[date, int]]:
    parsed_headers: list[str | date] = []
    first_column_by_header: dict[str, int] = {}
    first_column_by_date: dict[date, int] = {}

    for column, raw_header in enumerate(raw_headers, start=1):
        if _is_blank(raw_header):
            raise WorkbookValidationError(
                f"명단 시트 1행 {column}열: 헤더는 빈칸일 수 없습니다."
            )

        parsed_date = parse_annual_date_header(raw_header)
        if parsed_date is not None:
            if parsed_date in first_column_by_date:
                raise WorkbookValidationError(
                    "명단 시트 1행: 날짜 헤더 "
                    f"'{parsed_date:%Y%m%d}'가 중복되었습니다. "
                    f"({first_column_by_date[parsed_date]}열, {column}열)"
                )
            parsed_headers.append(parsed_date)
            first_column_by_date[parsed_date] = column
            continue

        header = _header_text(raw_header)
        if header in first_column_by_header:
            raise WorkbookValidationError(
                f"명단 시트 1행: 헤더 '{header}'가 중복되었습니다. "
                f"({first_column_by_header[header]}열, {column}열)"
            )
        if header == "ID":
            raise WorkbookValidationError(
                "명단 시트는 ID 열을 사용하지 않습니다. ID 헤더를 삭제하고 "
                "한 사람을 연속된 조식·중식·석식 3행으로 입력하세요."
            )
        parsed_headers.append(header)
        first_column_by_header[header] = column

    if "식종" not in first_column_by_header:
        raise WorkbookValidationError("명단 시트에 필수 헤더 '식종'이 없습니다.")

    meal_column = first_column_by_header["식종"] - 1
    expected_control_end = meal_column + len(CONTROL_HEADERS)
    actual_controls = parsed_headers[meal_column:expected_control_end]
    if actual_controls != list(CONTROL_HEADERS):
        shown = " | ".join(str(value) for value in actual_controls)
        raise WorkbookValidationError(
            "명단 시트의 고정 헤더는 연속해서 '식종 | 배송담당 | 연번' "
            f"순서여야 합니다. 현재값: {shown or '(없음)'}"
        )

    profile_headers = parsed_headers[:meal_column]
    if not profile_headers:
        raise WorkbookValidationError(
            "명단 시트에는 식종 앞에 성명 등 인적사항 헤더가 하나 이상 "
            "필요합니다."
        )
    if any(isinstance(header, date) for header in profile_headers):
        raise WorkbookValidationError(
            "YYYYMMDD 날짜 헤더는 '식종 | 배송담당 | 연번' 오른쪽에만 "
            "둘 수 있습니다."
        )

    raw_date_headers = parsed_headers[expected_control_end:]
    if not raw_date_headers:
        raise WorkbookValidationError(
            "명단 시트에 YYYYMMDD 날짜 헤더가 하나 이상 필요합니다."
        )
    for offset, header in enumerate(raw_date_headers, start=expected_control_end + 1):
        if isinstance(header, date):
            continue
        raise WorkbookValidationError(
            f"명단 시트 1행 {offset}열: 연번 오른쪽 헤더 {header!r}은 "
            "지원하지 않습니다. 날짜 헤더는 YYYYMMDD 형식이어야 합니다."
        )

    profile_columns = [
        (cast(str, header), column)
        for column, header in enumerate(profile_headers)
    ]
    control_columns = {
        header: meal_column + index
        for index, header in enumerate(CONTROL_HEADERS)
    }
    date_columns = {
        cast(date, header): column
        for column, header in enumerate(parsed_headers)
        if isinstance(header, date)
    }
    return profile_columns, control_columns, date_columns


def _parse_delivery_value(
    value: Any,
    *,
    row_number: int,
    day: date,
) -> DeliveryCellValue:
    if _is_formula(value):
        raise WorkbookValidationError(
            f"명단 시트 {row_number}행, '{day:%Y%m%d}' 열: 배송 값에는 "
            "수식을 사용할 수 없습니다. 실제 숫자 1, 0 또는 빈칸을 "
            f"입력하세요. 현재값={value!r}"
        )
    if _is_blank(value):
        return DeliveryCellValue.BLANK
    if (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and (not isinstance(value, float) or math.isfinite(value))
        and value in (0, 1)
    ):
        return DeliveryCellValue(int(value))
    raise WorkbookValidationError(
        f"명단 시트 {row_number}행, '{day:%Y%m%d}' 열: 배송 값은 "
        f"1, 0 또는 빈칸만 허용됩니다. 현재값={value!r}"
    )


@contextmanager
def _open_nonblank_rows(
    worksheet: ReadOnlyWorksheet,
) -> Iterator[Iterator[tuple[int, _SparseRow]]]:
    """XML에 실제로 존재하는 행만 읽어 과도한 서식 차원을 무시한다."""
    internal = cast(Any, worksheet)
    with internal._get_source() as source:
        parser = WorkSheetParser(
            source,
            internal._shared_strings,
            data_only=internal.parent.data_only,
            epoch=internal.parent.epoch,
            date_formats=internal.parent._date_formats,
            timedelta_formats=internal.parent._timedelta_formats,
        )

        def nonblank_rows() -> Iterator[tuple[int, _SparseRow]]:
            for row_number, cells in parser.parse():
                values = {
                    cell["column"]: cell["value"]
                    for cell in cells
                    if not _is_blank(cell["value"])
                }
                if values:
                    yield row_number, values

        yield nonblank_rows()


def _read_header(
    rows: Iterator[tuple[int, _SparseRow]],
    sheet_name: str,
) -> list[Any]:
    first_row = next(rows, None)
    if first_row is None or first_row[0] != 1:
        raise WorkbookValidationError(f"{sheet_name} 시트가 비어 있습니다.")
    values = first_row[1]
    last_column = max(values)
    return [values.get(column) for column in range(1, last_column + 1)]


def _materialize_row(
    sparse_row: _SparseRow,
    header_width: int,
    sheet_name: str,
    row_number: int,
) -> tuple[Any, ...]:
    extra_columns = sorted(column for column in sparse_row if column > header_width)
    if extra_columns:
        column = extra_columns[0]
        column_name = get_column_letter(column)
        value = sparse_row[column]
        remaining = len(extra_columns) - 1
        suffix = f" 외 {remaining}개 열" if remaining else ""
        raise WorkbookValidationError(
            f"{sheet_name} 시트 {row_number}행: 헤더가 없는 {column_name}열에 "
            f"값 {value!r}이 있습니다{suffix}."
        )
    return tuple(sparse_row.get(column) for column in range(1, header_width + 1))


def _same_profile_value(first: Any, second: Any) -> bool:
    if _is_blank(first) and _is_blank(second):
        return True
    return bool(first == second)


def _is_formula(value: Any) -> bool:
    return isinstance(value, (ArrayFormula, DataTableFormula)) or (
        isinstance(value, str) and value.startswith("=")
    )


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _cell(row: Sequence[Any], column: int) -> Any:
    return row[column] if column < len(row) else None


def _header_text(value: Any) -> str:
    return " ".join(_text(value).split())


def _text(value: Any) -> str:
    if _is_blank(value):
        return ""
    if isinstance(value, str):
        return value.strip().replace("\n", " ")
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()
