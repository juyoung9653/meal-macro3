"""연간 배송 Excel 파일을 검증해 도메인 모델로 변환하는 어댑터."""

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

from ..core.delivery_matrix import (
    ALLOWED_MEALS,
    AnnualDeliveryBook,
    DeliveryCellValue,
    DeliveryRow,
    RosterEntry,
)
from ._parsing import parse_annual_date_header

REQUIRED_SHEETS = ("명단", "배송현황")
FIXED_DELIVERY_HEADERS = ("ID", "식종", "배송담당", "연번")

_SparseRow = dict[int, Any]


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
                self._path, data_only=True, read_only=True
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
                    decrypted, data_only=True, read_only=True
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
        missing = [name for name in REQUIRED_SHEETS if name not in workbook.sheetnames]
        if missing:
            raise WorkbookValidationError(
                f"필수 시트가 없습니다: {', '.join(missing)} "
                "(필수 시트: 명단, 배송현황)"
            )

        roster_sheet = cast(ReadOnlyWorksheet, workbook["명단"])
        delivery_sheet = cast(ReadOnlyWorksheet, workbook["배송현황"])
        roster, roster_ids = _parse_roster_sheet(roster_sheet)
        delivery_rows, dates, year = _parse_delivery_sheet(
            delivery_sheet, roster_ids
        )
        try:
            return AnnualDeliveryBook(
                year=year,
                roster=roster,
                delivery_rows=delivery_rows,
                dates=dates,
            )
        except ValueError as error:
            raise WorkbookValidationError(
                f"연간 배송 도메인 구성에 실패했습니다: {error}"
            ) from error


def _parse_roster_sheet(
    worksheet: ReadOnlyWorksheet,
) -> tuple[tuple[RosterEntry, ...], set[str]]:
    roster: list[RosterEntry] = []
    first_row_by_id: dict[str, int] = {}

    with _open_nonblank_rows(worksheet) as rows:
        raw_headers = _read_header(rows, "명단")
        headers = _validate_roster_headers(raw_headers)
        id_column = headers.index("ID")

        for row_number, sparse_row in rows:
            row = _materialize_row(sparse_row, len(headers), "명단", row_number)
            person_id = _identifier(_cell(row, id_column))
            if not person_id:
                raise WorkbookValidationError(
                    f"명단 시트 {row_number}행: ID는 빈칸일 수 없습니다."
                )
            if person_id in first_row_by_id:
                raise WorkbookValidationError(
                    f"명단 시트 {row_number}행: ID '{person_id}'이 중복되었습니다. "
                    f"(최초 {first_row_by_id[person_id]}행)"
                )

            extra = {
                header: _cell(row, column)
                for column, header in enumerate(headers)
                if header != "ID"
            }
            roster.append(RosterEntry(person_id=person_id, extra=extra))
            first_row_by_id[person_id] = row_number

    return tuple(roster), set(first_row_by_id)


def _validate_roster_headers(raw_headers: list[Any]) -> list[str]:
    headers: list[str] = []
    first_column_by_header: dict[str, int] = {}
    for column, raw_header in enumerate(raw_headers, start=1):
        header = _text(raw_header)
        if not header:
            raise WorkbookValidationError(
                f"명단 시트 1행 {column}열: 헤더는 빈칸일 수 없습니다."
            )
        if header in first_column_by_header:
            raise WorkbookValidationError(
                f"명단 시트 1행: 헤더 '{header}'가 중복되었습니다. "
                f"({first_column_by_header[header]}열, {column}열)"
            )
        headers.append(header)
        first_column_by_header[header] = column

    if "ID" not in first_column_by_header:
        raise WorkbookValidationError("명단 시트에 필수 헤더 'ID'가 없습니다.")
    return headers


def _parse_delivery_sheet(
    worksheet: ReadOnlyWorksheet,
    roster_ids: set[str],
) -> tuple[tuple[DeliveryRow, ...], tuple[date, ...], int]:
    delivery_rows: list[DeliveryRow] = []
    first_row_by_key: dict[tuple[str, str], int] = {}

    with _open_nonblank_rows(worksheet) as rows:
        raw_headers = _read_header(rows, "배송현황")
        fixed_columns, date_columns = _validate_delivery_headers(raw_headers)
        dates = tuple(sorted(date_columns))
        years = sorted({day.year for day in dates})
        if len(years) != 1:
            raise WorkbookValidationError(
                "배송현황 날짜 헤더는 한 연도여야 합니다. 발견 연도: "
                + ", ".join(str(year) for year in years)
            )

        for row_number, sparse_row in rows:
            row = _materialize_row(
                sparse_row, len(raw_headers), "배송현황", row_number
            )
            person_id = _identifier(_cell(row, fixed_columns["ID"]))
            if not person_id:
                raise WorkbookValidationError(
                    f"배송현황 시트 {row_number}행: ID는 빈칸일 수 없습니다."
                )
            if person_id not in roster_ids:
                raise WorkbookValidationError(
                    f"배송현황 시트 {row_number}행: 명단에 없는 ID '{person_id}'입니다."
                )

            meal = _text(_cell(row, fixed_columns["식종"]))
            if meal not in ALLOWED_MEALS:
                shown = meal if meal else "빈칸"
                raise WorkbookValidationError(
                    f"배송현황 시트 {row_number}행: 식종 '{shown}'은 허용되지 "
                    "않습니다. 허용값: 조식, 중식, 석식"
                )

            key = (person_id, meal)
            if key in first_row_by_key:
                raise WorkbookValidationError(
                    f"배송현황 시트 {row_number}행: (ID, 식종) 키 {key!r}가 "
                    f"중복되었습니다. (최초 {first_row_by_key[key]}행)"
                )

            values = {
                day: _parse_delivery_value(
                    _cell(row, column), row_number=row_number, day=day
                )
                for day, column in date_columns.items()
            }
            delivery_rows.append(
                DeliveryRow(
                    person_id=person_id,
                    meal=meal,
                    staff=_text(_cell(row, fixed_columns["배송담당"])),
                    sequence=_cell(row, fixed_columns["연번"]),
                    values=values,
                )
            )
            first_row_by_key[key] = row_number

    return tuple(delivery_rows), dates, years[0]


def _validate_delivery_headers(
    raw_headers: list[Any],
) -> tuple[dict[str, int], dict[date, int]]:
    fixed_columns: dict[str, int] = {}
    date_columns: dict[date, int] = {}
    for column, raw_header in enumerate(raw_headers):
        header = _text(raw_header)
        if header in FIXED_DELIVERY_HEADERS:
            if header in fixed_columns:
                raise WorkbookValidationError(
                    f"배송현황 시트 1행: 고정 헤더 '{header}'가 중복되었습니다."
                )
            fixed_columns[header] = column
            continue

        parsed_date = parse_annual_date_header(raw_header)
        if parsed_date is None:
            shown = "빈칸" if _is_blank(raw_header) else repr(raw_header)
            raise WorkbookValidationError(
                f"배송현황 시트 1행 {column + 1}열: 헤더 {shown}은 지원하지 "
                "않습니다. 날짜 헤더는 YYYYMMDD 형식이어야 합니다."
            )
        if parsed_date in date_columns:
            raise WorkbookValidationError(
                "배송현황 시트 1행: 날짜 헤더 "
                f"'{parsed_date:%Y%m%d}'가 중복되었습니다."
            )
        date_columns[parsed_date] = column

    missing = [header for header in FIXED_DELIVERY_HEADERS if header not in fixed_columns]
    if missing:
        raise WorkbookValidationError(
            "배송현황 시트에 필수 고정 헤더가 없습니다: " + ", ".join(missing)
        )
    if not date_columns:
        raise WorkbookValidationError(
            "배송현황 시트에 YYYYMMDD 날짜 헤더가 하나 이상 필요합니다."
        )
    return fixed_columns, date_columns


def _parse_delivery_value(
    value: Any, *, row_number: int, day: date
) -> DeliveryCellValue:
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
        f"배송현황 시트 {row_number}행, '{day:%Y%m%d}' 열: 배송 값은 "
        f"1, 0 또는 빈칸만 허용됩니다. 현재값={value!r}"
    )


@contextmanager
def _open_nonblank_rows(
    worksheet: ReadOnlyWorksheet,
) -> Iterator[Iterator[tuple[int, _SparseRow]]]:
    """XML에 실제로 존재하는 행만 읽어 빈 서식 차원과 행 간격을 건너뛴다."""
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
    rows: Iterator[tuple[int, _SparseRow]], sheet_name: str
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


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _cell(row: Sequence[Any], column: int) -> Any:
    return row[column] if column < len(row) else None


def _identifier(value: Any) -> str:
    if _is_blank(value):
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _text(value: Any) -> str:
    if _is_blank(value):
        return ""
    if isinstance(value, str):
        return value.strip().replace("\n", " ")
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()
