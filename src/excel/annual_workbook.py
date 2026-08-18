"""선택한 Excel의 ``db`` 테이블을 검증해 배송 도메인으로 변환한다."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
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
from openpyxl.packaging.relationship import get_dependents, get_rels_path
from openpyxl.utils import get_column_letter, range_boundaries
from openpyxl.utils.exceptions import InvalidFileException
from openpyxl.workbook.workbook import Workbook
from openpyxl.worksheet._read_only import ReadOnlyWorksheet
from openpyxl.worksheet._reader import WorkSheetParser
from openpyxl.worksheet.formula import ArrayFormula, DataTableFormula
from openpyxl.worksheet.table import Table
from openpyxl.xml.functions import fromstring

from ..core.delivery_matrix import (
    ALLOWED_MEALS,
    AnnualDeliveryBook,
    DeliveryCellValue,
    DeliveryRow,
    ProfileConflict,
    RosterEntry,
)
from ._parsing import parse_annual_date_header

TABLE_NAME = "db"
CONTROL_HEADERS = ("식종", "배송담당", "연번")
PERSON_HEADER_ROLES = (
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
)
ROW_VARYING_PROFILE_HEADER_ROLES = (
    "비고",
)
RELOCATION_PROFILE_HEADER_ROLES = (
    "도로명주소",
    "관할동",
    "상세주소",
)
NON_REVIEW_PROFILE_HEADER_ROLES = (
    "핸드폰",
    *RELOCATION_PROFILE_HEADER_ROLES,
)
PROFILE_HEADER_ROLES = (*PERSON_HEADER_ROLES, *ROW_VARYING_PROFILE_HEADER_ROLES)
HEADER_MAPPING_ROLES = (*CONTROL_HEADERS, *PROFILE_HEADER_ROLES)
CORE_REQUIRED_HEADER_ROLES = ("식종", "배송담당")
REQUIRED_HEADER_ROLES = (*CORE_REQUIRED_HEADER_ROLES, *PROFILE_HEADER_ROLES)

_SparseRow = dict[int, Any]
_MaterializedRow = tuple[int, tuple[Any, ...]]


@dataclass(frozen=True)
class _TableRegion:
    """Read-only 워크시트 안에서 실제로 읽을 구조화 테이블의 범위."""

    worksheet: ReadOnlyWorksheet
    min_column: int
    min_row: int
    max_column: int
    max_data_row: int

    @property
    def source_name(self) -> str:
        return f"'{TABLE_NAME}' 테이블({self.worksheet.title!r} 시트)"


class WorkbookValidationError(ValueError):
    """연간 배송 워크북의 계약 위반을 사용자에게 설명하는 오류."""


class WorkbookPasswordRequiredError(WorkbookValidationError):
    """암호화된 워크북을 열기 위해 비밀번호 입력이 필요하다."""


class WorkbookPasswordError(WorkbookValidationError):
    """입력된 비밀번호로 암호화된 워크북을 열 수 없다."""


def _normalize_header_mapping(
    mapping: Mapping[str, str] | None,
) -> dict[str, str]:
    resolved = {role: role for role in HEADER_MAPPING_ROLES}
    if mapping is None:
        return resolved
    if not isinstance(mapping, Mapping):
        raise TypeError("헤더 매핑은 역할과 헤더 이름의 매핑이어야 합니다.")

    unknown_roles = sorted(set(mapping) - set(HEADER_MAPPING_ROLES))
    if unknown_roles:
        raise ValueError(
            "지원하지 않는 헤더 역할입니다: " + ", ".join(unknown_roles)
        )

    for role in HEADER_MAPPING_ROLES:
        if role not in mapping:
            continue
        selected = mapping[role]
        if not isinstance(selected, str):
            raise TypeError(f"'{role}'에 매핑할 헤더 이름은 문자열이어야 합니다.")
        resolved[role] = _header_text(selected)

    missing_required = [
        role for role in CORE_REQUIRED_HEADER_ROLES if not resolved[role]
    ]
    if missing_required:
        raise ValueError(
            "필수 헤더 역할은 비워 둘 수 없습니다: "
            + ", ".join(missing_required)
        )

    selected_headers = tuple(header for header in resolved.values() if header)
    if len(set(selected_headers)) != len(selected_headers):
        raise ValueError("선택한 프로그램 필드는 서로 다른 헤더에 매핑해야 합니다.")
    return resolved


def _normalize_selected_year(value: int | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("선택 연도는 정수여야 합니다.")
    if value < 1:
        raise ValueError("선택 연도는 1 이상의 정수여야 합니다.")
    return value


class AnnualWorkbook:
    """xlsx 로드, 선택적 암호 해제, 검증 및 도메인 객체 캐시를 담당한다."""

    def __init__(
        self,
        path: str | Path,
        password: str | None = None,
        header_mapping: Mapping[str, str] | None = None,
        selected_year: int | None = None,
    ):
        self._path = Path(path)
        self._password = password or ""
        self._header_mapping = _normalize_header_mapping(header_mapping)
        self._selected_year = _normalize_selected_year(selected_year)
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

    @property
    def header_mapping(self) -> dict[str, str]:
        """프로그램 역할 → 선택한 ``db`` 테이블 헤더 매핑 복사본."""
        return dict(self._header_mapping)

    @header_mapping.setter
    def header_mapping(self, value: Mapping[str, str] | None) -> None:
        new_mapping = _normalize_header_mapping(value)
        if new_mapping != self._header_mapping:
            self._header_mapping = new_mapping
            self.clear_cache()

    @property
    def selected_year(self) -> int | None:
        """여러 날짜 연도 중 읽을 연도. 단일 연도 파일은 로드 시 자동 확정된다."""
        return self._selected_year

    @selected_year.setter
    def selected_year(self, value: int | None) -> None:
        new_year = _normalize_selected_year(value)
        if new_year != self._selected_year:
            self._selected_year = new_year
            self.clear_cache()

    def table_headers(self) -> tuple[str, ...]:
        """GUI 매핑에 표시할 ``db`` 테이블 헤더를 물리적 순서로 읽는다."""
        with self._open_workbook() as workbook:
            region = _find_table_region(workbook)
            raw_headers = _read_region_headers(region)
        return tuple(_display_header(value) for value in raw_headers)

    def load(self, *, force: bool = False) -> AnnualDeliveryBook:
        if self._cached_book is not None and not force:
            return self._cached_book

        with self._open_workbook() as workbook:
            book = self._parse_workbook(workbook)
        self._selected_year = book.year
        self._cached_book = book
        return book

    def clear_cache(self) -> None:
        self._cached_book = None

    def forget_password(self) -> None:
        """파싱 결과 캐시는 유지하면서 보유 중인 비밀번호 참조를 제거한다."""
        self._password = ""

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
        ):
            pass
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
                    try:
                        office_file = msoffcrypto.OfficeFile(encrypted)
                        is_encrypted = office_file.is_encrypted()
                    except (FileFormatError, ParseError) as format_error:
                        raise WorkbookValidationError(
                            "Excel 파일을 해석할 수 없습니다. 파일이 "
                            f"손상되었거나 지원하지 않는 형식입니다: {self._path}"
                        ) from format_error

                    if not is_encrypted:
                        raise WorkbookValidationError(
                            "Excel 파일을 해석할 수 없습니다. 파일이 "
                            f"손상되었습니다: {self._path}"
                        )
                    if not self._password:
                        raise WorkbookPasswordRequiredError(
                            "암호로 보호된 db 파일입니다. 비밀번호를 입력해 주세요."
                        )

                    try:
                        office_file.load_key(password=self._password)
                        office_file.decrypt(decrypted)
                    except (
                        DecryptionError,
                        InvalidKeyError,
                        ValueError,
                    ) as password_error:
                        raise WorkbookPasswordError(
                            "비밀번호가 올바르지 않거나 암호화 파일이 "
                            "손상되었습니다."
                        ) from password_error
                decrypted.seek(0)
                workbook = openpyxl.load_workbook(
                    decrypted, data_only=False, read_only=True
                )
            except OSError:
                raise
            except (WorkbookPasswordRequiredError, WorkbookPasswordError):
                raise
            except WorkbookValidationError:
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
                    "암호 해제된 Excel 파일을 해석할 수 없습니다. 파일이 "
                    f"손상되었습니다: {self._path}"
                ) from decrypt_error

            try:
                yield workbook
            finally:
                workbook.close()
        finally:
            decrypted.close()

    def _parse_workbook(self, workbook: Workbook) -> AnnualDeliveryBook:
        region = _find_table_region(workbook)
        roster, delivery_rows, dates, year, profile_conflicts = _parse_roster_matrix(
            region,
            header_mapping=self._header_mapping,
            selected_year=self._selected_year,
        )
        try:
            return AnnualDeliveryBook(
                year=year,
                roster=roster,
                delivery_rows=delivery_rows,
                dates=dates,
                profile_conflicts=profile_conflicts,
            )
        except (TypeError, ValueError) as error:
            raise WorkbookValidationError(
                f"연간 배송 도메인 구성에 실패했습니다: {error}"
            ) from error


def _find_table_region(workbook: Workbook) -> _TableRegion:
    matches: list[tuple[ReadOnlyWorksheet, Table]] = []
    for raw_worksheet in workbook.worksheets:
        worksheet = cast(ReadOnlyWorksheet, raw_worksheet)
        for table in _read_worksheet_tables(worksheet):
            names = {
                value.casefold()
                for value in (table.name, table.displayName)
                if isinstance(value, str)
            }
            if TABLE_NAME.casefold() in names:
                matches.append((worksheet, table))

    if not matches:
        raise WorkbookValidationError(
            f"선택한 Excel 파일에 이름이 '{TABLE_NAME}'인 "
            "표(테이블)가 없습니다."
        )
    if len(matches) > 1:
        sheets = ", ".join(repr(worksheet.title) for worksheet, _table in matches)
        raise WorkbookValidationError(
            f"이름이 '{TABLE_NAME}'인 테이블이 여러 개입니다. "
            f"확인할 시트: {sheets}"
        )

    worksheet, table = matches[0]
    if table.headerRowCount != 1:
        raise WorkbookValidationError(
            f"'{TABLE_NAME}' 테이블({worksheet.title!r} 시트)은 "
            "헤더 행을 정확히 1개 사용해야 합니다."
        )

    try:
        boundaries = range_boundaries(table.ref)
        if not all(isinstance(value, int) and value >= 1 for value in boundaries):
            raise ValueError("테이블 범위는 셀 좌표의 직사각형이어야 합니다.")
        min_column, min_row, max_column, max_row = cast(
            tuple[int, int, int, int], boundaries
        )
        if min_column > max_column or min_row > max_row:
            raise ValueError("테이블 범위의 시작 좌표가 끝 좌표보다 큽니다.")
    except (TypeError, ValueError) as error:
        raise WorkbookValidationError(
            f"'{TABLE_NAME}' 테이블({worksheet.title!r} 시트)의 "
            f"범위가 올바르지 않습니다: {table.ref!r}"
        ) from error

    totals_rows = table.totalsRowCount
    if totals_rows is None:
        totals_rows = 1 if table.totalsRowShown else 0
    max_data_row = max_row - totals_rows
    if max_data_row < min_row:
        raise WorkbookValidationError(
            f"'{TABLE_NAME}' 테이블({worksheet.title!r} 시트)의 "
            "헤더·합계행 범위가 올바르지 않습니다."
        )

    return _TableRegion(
        worksheet=worksheet,
        min_column=min_column,
        min_row=min_row,
        max_column=max_column,
        max_data_row=max_data_row,
    )


def _read_worksheet_tables(worksheet: ReadOnlyWorksheet) -> Iterator[Table]:
    """read-only 모드에서 시트 관계 XML을 통해 구조화 테이블을 읽는다."""
    internal = cast(Any, worksheet)
    archive = internal.parent._archive
    relationships_path = get_rels_path(internal._worksheet_path)
    if relationships_path not in archive.namelist():
        return

    try:
        relationships = get_dependents(archive, relationships_path)
        for relationship in relationships.find(Table._rel_type):
            xml = fromstring(archive.read(relationship.target))
            yield Table.from_tree(xml)
    except (KeyError, TypeError, ValueError) as error:
        raise WorkbookValidationError(
            f"{worksheet.title!r} 시트의 Excel 테이블 정보를 "
            "해석할 수 없습니다."
        ) from error


def _read_region_headers(region: _TableRegion) -> list[Any]:
    with _open_nonblank_rows(region) as rows:
        return _read_header(
            rows,
            region.source_name,
            region.min_row,
            region.max_column - region.min_column + 1,
        )


def _parse_roster_matrix(
    region: _TableRegion,
    *,
    header_mapping: Mapping[str, str],
    selected_year: int | None,
) -> tuple[
    tuple[RosterEntry, ...],
    tuple[DeliveryRow, ...],
    tuple[date, ...],
    int,
    tuple[ProfileConflict, ...],
]:
    roster: list[RosterEntry] = []
    delivery_rows: list[DeliveryRow] = []
    profile_conflicts: list[ProfileConflict] = []
    source_name = region.source_name

    with _open_nonblank_rows(region) as rows:
        raw_headers = _read_header(
            rows,
            source_name,
            region.min_row,
            region.max_column - region.min_column + 1,
        )
        profile_columns, control_columns, date_columns = _validate_headers(
            raw_headers,
            source_name=source_name,
            header_row=region.min_row,
            first_column=region.min_column,
            header_mapping=header_mapping,
        )
        years = sorted({day.year for day in date_columns})
        if selected_year is None and len(years) != 1:
            raise WorkbookValidationError(
                f"{source_name} 날짜 헤더에 여러 연도가 있습니다. GUI에서 "
                "사용할 연도를 선택해 주세요. 발견 연도: "
                + ", ".join(str(year) for year in years)
            )
        resolved_year = years[0] if selected_year is None else selected_year
        if resolved_year not in years:
            raise WorkbookValidationError(
                f"{source_name}에 선택한 {resolved_year}년 날짜 헤더가 없습니다. "
                "발견 연도: " + ", ".join(str(year) for year in years)
            )
        date_columns = {
            day: column
            for day, column in date_columns.items()
            if day.year == resolved_year
        }
        dates = tuple(sorted(date_columns))
        mapped_data_columns = frozenset(
            [column for _header, column in profile_columns]
            + [
                column
                for column in control_columns.values()
                if column is not None
            ]
            + list(date_columns.values())
        )

        groups: list[list[_MaterializedRow]] = []
        groups_by_identity: dict[tuple[str, ...], list[_MaterializedRow]] = {}
        for row_number, sparse_row in rows:
            row = _materialize_row(
                sparse_row,
                len(raw_headers),
            )
            if not any(
                not _is_blank(_cell(row, column))
                for column in mapped_data_columns
            ):
                continue
            profile = _profile_values(row, profile_columns)
            identities = _person_identity_keys(profile)
            group = next(
                (
                    groups_by_identity[identity]
                    for identity in identities
                    if identity in groups_by_identity
                ),
                None,
            )
            if group is None:
                group = []
                groups.append(group)
                for identity in identities:
                    groups_by_identity[identity] = group
            group.append((row_number, row))

        for group in groups:
            person, person_rows, person_conflicts = _parse_person_group(
                group,
                profile_columns=profile_columns,
                control_columns=control_columns,
                date_columns=date_columns,
                source_name=source_name,
            )
            roster.append(person)
            delivery_rows.extend(person_rows)
            profile_conflicts.extend(person_conflicts)

    return (
        tuple(roster),
        tuple(delivery_rows),
        dates,
        resolved_year,
        tuple(profile_conflicts),
    )


def _parse_person_group(
    group: Sequence[_MaterializedRow],
    *,
    profile_columns: Sequence[tuple[str, int]],
    control_columns: Mapping[str, int | None],
    date_columns: dict[date, int],
    source_name: str,
) -> tuple[
    RosterEntry,
    tuple[DeliveryRow, ...],
    tuple[ProfileConflict, ...],
]:
    row_numbers = tuple(row_number for row_number, _row in group)
    profiles = tuple(
        _profile_values(row, profile_columns)
        for _row_number, row in group
    )
    merged_profile, profile_conflicts = _merge_profiles(row_numbers, profiles)

    meal_column = cast(int, control_columns["식종"])
    raw_meals = tuple(_text(_cell(row, meal_column)) for _row_number, row in group)
    matched_meals = tuple(_match_meal_label(value) for value in raw_meals)
    if any(meal is None for meal in matched_meals):
        shown = ", ".join(meal or "빈칸" for meal in raw_meals)
        raise WorkbookValidationError(
            f"{source_name} {_format_row_numbers(row_numbers)}: "
            "각 식종 값에는 조식, 중식, 석식 중 하나가 포함되어야 합니다. "
            f"현재값: {shown}"
        )
    meals = cast(tuple[str, ...], matched_meals)

    person = RosterEntry(
        source_row=row_numbers[0],
        extra=merged_profile,
        source_end_row=row_numbers[-1],
        source_rows=row_numbers,
    )
    parsed_rows_by_meal: dict[str, DeliveryRow] = {}
    staff_column = cast(int, control_columns["배송담당"])
    sequence_column = control_columns["연번"]
    for (row_number, row), meal in zip(group, meals):
        values = {
            day: _parse_delivery_value(
                _cell(row, column),
                row_number=row_number,
                day=day,
                source_name=source_name,
            )
            for day, column in date_columns.items()
        }
        candidate = DeliveryRow(
            person=person,
            meal=meal,
            staff=_text(_cell(row, staff_column)),
            sequence=(
                None
                if sequence_column is None
                else _cell(row, sequence_column)
            ),
            values=values,
            source_row=row_number,
        )
        previous = parsed_rows_by_meal.get(meal)
        parsed_rows_by_meal[meal] = (
            candidate
            if previous is None
            else _merge_duplicate_meal_rows(previous, candidate)
        )

    return person, tuple(parsed_rows_by_meal.values()), profile_conflicts


def _validate_headers(
    raw_headers: list[Any],
    *,
    source_name: str,
    header_row: int,
    first_column: int,
    header_mapping: Mapping[str, str],
) -> tuple[
    list[tuple[str, int]],
    dict[str, int | None],
    dict[date, int],
]:
    parsed_headers: list[str | date] = []
    first_column_by_header: dict[str, int] = {}
    first_column_by_date: dict[date, int] = {}

    for column_index, raw_header in enumerate(raw_headers):
        column = first_column + column_index
        if _is_blank(raw_header):
            raise WorkbookValidationError(
                f"{source_name} {header_row}행 {get_column_letter(column)}열: "
                "헤더는 빈칸일 수 없습니다."
            )

        parsed_date = parse_annual_date_header(raw_header)
        if parsed_date is not None:
            if parsed_date in first_column_by_date:
                raise WorkbookValidationError(
                    f"{source_name} {header_row}행: 날짜 헤더 "
                    f"'{parsed_date:%Y%m%d}'가 중복되었습니다. "
                    f"({get_column_letter(first_column_by_date[parsed_date])}열, "
                    f"{get_column_letter(column)}열)"
                )
            parsed_headers.append(parsed_date)
            first_column_by_date[parsed_date] = column
            continue

        header = _header_text(raw_header)
        if header in first_column_by_header:
            raise WorkbookValidationError(
                f"{source_name} {header_row}행: 헤더 '{header}'가 "
                "중복되었습니다. "
                f"({get_column_letter(first_column_by_header[header])}열, "
                f"{get_column_letter(column)}열)"
            )
        parsed_headers.append(header)
        first_column_by_header[header] = column

    control_columns: dict[str, int | None] = {}
    for role in CONTROL_HEADERS:
        selected_header = _header_text(header_mapping.get(role, role))
        if not selected_header:
            if role in CORE_REQUIRED_HEADER_ROLES:
                raise WorkbookValidationError(
                    f"{source_name}의 필수 '{role}' 역할이 매핑되지 않았습니다."
                )
            control_columns[role] = None
            continue
        absolute_column = first_column_by_header.get(selected_header)
        if absolute_column is None:
            if role not in CORE_REQUIRED_HEADER_ROLES and selected_header == role:
                control_columns[role] = None
                continue
            raise WorkbookValidationError(
                f"{source_name}의 '{role}' 역할에 매핑한 "
                f"헤더 '{selected_header}'을(를) 찾을 수 없습니다. "
                "헤더 매핑에서 다시 선택해 주세요."
            )
        control_columns[role] = absolute_column - first_column

    profile_role_by_source: dict[str, str] = {}
    for role in PROFILE_HEADER_ROLES:
        selected_header = _header_text(header_mapping.get(role, role))
        if not selected_header:
            if role in REQUIRED_HEADER_ROLES:
                raise WorkbookValidationError(
                    f"{source_name}의 필수 인적사항 '{role}' 역할이 "
                    "매핑되지 않았습니다. DB 가져오기 설정에서 "
                    "해당 Excel 헤더를 선택해 주세요."
                )
            continue
        if selected_header not in first_column_by_header:
            # 선택 필드이므로 표준명 헤더 자체가 없는 파일에서는 생략한다.
            # 표준명이 아닌 사용자의 명시적 매핑이 사라졌다면 오설정을 알린다.
            if selected_header == role:
                continue
            raise WorkbookValidationError(
                f"{source_name}의 '{role}' 역할에 매핑한 "
                f"헤더 '{selected_header}'을(를) 찾을 수 없습니다. "
                "헤더 매핑에서 다시 선택해 주세요."
            )
        profile_role_by_source[selected_header] = role

    control_column_indexes = frozenset(
        column for column in control_columns.values() if column is not None
    )
    profile_columns: list[tuple[str, int]] = []
    first_column_by_profile: dict[str, int] = {}
    for column, header in enumerate(parsed_headers):
        if not isinstance(header, str) or column in control_column_indexes:
            continue

        mapped_role = profile_role_by_source.get(header)
        if mapped_role is None:
            # 합계·수식 등 매핑하지 않은 열은 인적사항으로 간주하지 않는다.
            continue
        profile_header = mapped_role

        previous_column = first_column_by_profile.get(profile_header)
        if previous_column is not None:
            raise WorkbookValidationError(
                f"{source_name} {header_row}행: 헤더 매핑 결과 "
                f"'{profile_header}' 필드가 중복되었습니다. "
                f"({get_column_letter(first_column + previous_column)}열, "
                f"{get_column_letter(first_column + column)}열)"
            )
        first_column_by_profile[profile_header] = column
        profile_columns.append((profile_header, column))
    if not profile_columns:
        raise WorkbookValidationError(
            f"{source_name}에는 매핑한 필수 헤더 외에 성명 등 "
            "인적사항 헤더가 하나 이상 "
            "필요합니다."
        )

    if not first_column_by_date:
        raise WorkbookValidationError(
            f"{source_name}에 YYYYMMDD 날짜 헤더가 하나 이상 필요합니다."
        )
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
    source_name: str,
) -> DeliveryCellValue:
    if _is_formula(value):
        raise WorkbookValidationError(
            f"{source_name} {row_number}행, "
            f"'{day:%Y%m%d}' 열: 배송 값에는 "
            "수식을 사용할 수 없습니다. 1, 0 또는 빈칸(null)을 "
            f"입력하세요. 현재값={value!r}"
        )
    if _is_blank(value):
        return DeliveryCellValue.BLANK
    if isinstance(value, str):
        normalized = value.strip()
        if normalized in {"`", "'"} or normalized.casefold() in {"null", "none"}:
            return DeliveryCellValue.BLANK
        if normalized == "1":
            return DeliveryCellValue.DELIVERED
        if normalized == "0":
            return DeliveryCellValue.PAUSED
    if (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and value in (0, 1)
    ):
        return DeliveryCellValue(int(value))
    raise WorkbookValidationError(
        f"{source_name} {row_number}행, '{day:%Y%m%d}' 열: 배송 값은 "
        f"1, 0 또는 빈칸(null)만 허용됩니다. "
        f"현재값={value!r}"
    )


@contextmanager
def _open_nonblank_rows(
    region: _TableRegion,
) -> Iterator[Iterator[tuple[int, _SparseRow]]]:
    """``db`` 테이블 안의 실제 비어 있지 않은 행만 스트리밍한다."""
    internal = cast(Any, region.worksheet)
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
                if row_number < region.min_row:
                    continue
                if row_number > region.max_data_row:
                    break
                values = {
                    cell["column"] - region.min_column + 1: cell["value"]
                    for cell in cells
                    if region.min_column
                    <= cell["column"]
                    <= region.max_column
                    and not _is_blank(cell["value"])
                }
                if values:
                    yield row_number, values

        yield nonblank_rows()


def _read_header(
    rows: Iterator[tuple[int, _SparseRow]],
    source_name: str,
    header_row: int,
    header_width: int,
) -> list[Any]:
    first_row = next(rows, None)
    if first_row is None or first_row[0] != header_row:
        raise WorkbookValidationError(
            f"{source_name}의 헤더 행({header_row}행)이 비어 있습니다."
        )
    values = first_row[1]
    return [values.get(column) for column in range(1, header_width + 1)]


def _materialize_row(
    sparse_row: _SparseRow,
    header_width: int,
) -> tuple[Any, ...]:
    return tuple(sparse_row.get(column) for column in range(1, header_width + 1))


def _profile_values(
    row: Sequence[Any],
    profile_columns: Sequence[tuple[str, int]],
) -> dict[str, Any]:
    return {header: _cell(row, column) for header, column in profile_columns}


def _person_identity_keys(
    profile: Mapping[str, Any],
) -> tuple[tuple[str, ...], ...]:
    """VBA와 같이 성명·관할동에 생년월일 또는 핸드폰을 조합한다."""
    name = _person_name_key(profile.get("성명"))
    if not name:
        return ()
    ward = _profile_text_key(profile.get("관할동"))
    if not any(
        header in profile for header in ("생년월일", "핸드폰", "전화번호")
    ):
        # 간소화된 테스트/API 입력은 VBA 식별 열 자체가 없으므로 이름으로 묶는다.
        return ((name, ward, "N", "__name_only__"),)
    birth_prefix = _birth_date_prefix_key(profile.get("생년월일"))
    phone_header = "핸드폰" if "핸드폰" in profile else "전화번호"
    phone = _phone_key(profile.get(phone_header))
    identities: list[tuple[str, ...]] = []
    if birth_prefix:
        identities.append((name, ward, "J", birth_prefix))
    if phone:
        identities.append((name, ward, "M", phone))
    return tuple(identities)


def _person_name_key(value: Any) -> str:
    if _is_blank(value):
        return ""
    return " ".join(_text(value).split()).casefold()


def _profile_text_key(value: Any) -> str:
    if _is_blank(value):
        return ""
    return " ".join(_text(value).split()).casefold()


def _phone_key(value: Any) -> str:
    if _is_blank(value):
        return ""
    text = "".join(_text(value).split()).casefold()
    digits = "".join(character for character in text if character.isdigit())
    if digits and all(
        character.isdigit() or character in "-().+"
        for character in text
    ):
        return digits
    return text


def _birth_date_key(value: Any) -> str:
    if _is_blank(value):
        return ""
    if isinstance(value, date):
        return value.strftime("%Y%m%d")
    text = _text(value).strip()
    digits = "".join(character for character in text if character.isdigit())
    if len(digits) == 8:
        try:
            return date(
                int(digits[:4]),
                int(digits[4:6]),
                int(digits[6:]),
            ).strftime("%Y%m%d")
        except ValueError:
            pass
    return " ".join(text.split()).casefold()


def _birth_date_prefix_key(value: Any) -> str:
    """생년월일·주민번호를 비교할 때 정규화한 앞 6자리만 사용한다."""
    return _birth_date_key(value)[:6]


def _merge_profiles(
    row_numbers: Sequence[int],
    profiles: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], tuple[ProfileConflict, ...]]:
    first_profile = profiles[0]
    display_name = _text(
        _first_nonblank_value(profile.get("성명") for profile in profiles)
    )
    display_birth_date = _profile_display_text(
        _first_nonblank_value(
            profile.get("생년월일") for profile in profiles
        )
    )
    display_phone = _text(
        _first_nonblank_value(
            profile.get("핸드폰") for profile in profiles
        )
    )
    merged: dict[str, Any] = {}
    conflicts: list[ProfileConflict] = []
    for header in first_profile:
        values_by_row = tuple(
            (row_number, profile[header])
            for row_number, profile in zip(row_numbers, profiles)
        )
        if header in ROW_VARYING_PROFILE_HEADER_ROLES:
            merged[header] = _merge_row_varying_values(
                value for _row, value in values_by_row
            )
            continue

        selected = _first_nonblank_value(
            value for _row, value in values_by_row
        )
        merged[header] = selected
        nonblank = tuple(
            (row, value)
            for row, value in values_by_row
            if not _is_blank(value)
        )
        distinct_keys: list[Any] = []
        for _row, value in nonblank:
            key = _profile_value_key(header, value)
            if key not in distinct_keys:
                distinct_keys.append(key)
        if (
            len(distinct_keys) > 1
            and header not in NON_REVIEW_PROFILE_HEADER_ROLES
        ):
            conflicts.append(
                ProfileConflict(
                    name=display_name,
                    birth_date=display_birth_date,
                    phone=display_phone,
                    field=header,
                    selected_value=selected,
                    values_by_row=nonblank,
                )
            )
    return merged, tuple(conflicts)


def _profile_value_key(header: str, value: Any) -> Any:
    if header in {"성명", "세대주 성명"}:
        return _person_name_key(value)
    if header == "생년월일":
        return _birth_date_prefix_key(value)
    if header in {"핸드폰", "전화번호"}:
        return _phone_key(value)
    if header in RELOCATION_PROFILE_HEADER_ROLES:
        return _profile_text_key(value)
    numeric_key = _profile_numeric_key(value)
    if numeric_key is not None:
        return numeric_key
    if isinstance(value, str):
        return _profile_text_key(value)
    return value


def _profile_numeric_key(value: Any) -> tuple[str, Decimal] | None:
    """Excel 숫자와 같은 내용의 숫자 문자열을 동일한 비교 키로 만든다."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        return None
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite():
        return None
    return "number", number.normalize()


def _profile_display_text(value: Any) -> str:
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    return _text(value)


def _format_row_numbers(row_numbers: Sequence[int]) -> str:
    if len(row_numbers) == 1:
        return f"{row_numbers[0]}행"
    if tuple(row_numbers) == tuple(range(row_numbers[0], row_numbers[-1] + 1)):
        return f"{row_numbers[0]}~{row_numbers[-1]}행"
    return ", ".join(str(row) for row in row_numbers) + "행"


def _match_meal_label(value: str) -> str | None:
    """설명이 붙은 식종 문자열에서 유일한 표준 식종을 부분 일치로 찾는다."""
    matches = tuple(meal for meal in ALLOWED_MEALS if meal in value)
    return matches[0] if len(matches) == 1 else None


def _merge_duplicate_meal_rows(
    first: DeliveryRow,
    second: DeliveryRow,
) -> DeliveryRow:
    """같은 사람·식종의 중복 행을 배송 누락 없이 하나로 합친다."""
    first_staff = first.staff.strip()
    first_sequence = first.sequence_display.strip()
    values = {
        day: (
            DeliveryCellValue.DELIVERED
            if first.values[day] is DeliveryCellValue.DELIVERED
            or second.values[day] is DeliveryCellValue.DELIVERED
            else DeliveryCellValue.PAUSED
            if first.values[day] is DeliveryCellValue.PAUSED
            or second.values[day] is DeliveryCellValue.PAUSED
            else DeliveryCellValue.BLANK
        )
        for day in first.values
    }
    return DeliveryRow(
        person=first.person,
        meal=first.meal,
        staff=first.staff if first_staff else second.staff,
        sequence=first.sequence if first_sequence else second.sequence,
        values=values,
        source_row=first.source_row,
    )


def _merge_row_varying_values(values: Iterator[Any]) -> Any:
    """행별 비고의 비어 있지 않은 고유 내용을 입력 순서대로 합친다."""
    resolved = tuple(values)
    notes: list[str] = []
    for value in resolved:
        if _is_blank(value):
            continue
        note = str(value).strip()
        if note not in notes:
            notes.append(note)
    if notes:
        return "\n".join(notes)
    return resolved[0] if resolved else None


def _first_nonblank_value(values: Iterator[Any]) -> Any:
    """같은 사람의 행들에서 처음 입력된 인적사항 값을 사용한다."""
    resolved = tuple(values)
    for value in resolved:
        if not _is_blank(value):
            return value
    return resolved[0] if resolved else None


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


def _display_header(value: Any) -> str:
    parsed_date = parse_annual_date_header(value)
    if parsed_date is not None:
        return parsed_date.strftime("%Y%m%d")
    return _header_text(value)


def _text(value: Any) -> str:
    if _is_blank(value):
        return ""
    if isinstance(value, str):
        return value.strip().replace("\n", " ")
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()
