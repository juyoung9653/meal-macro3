"""Excel Adapter that emits typed observations without retaining source bytes."""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import date, datetime
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from stat import S_ISREG
from types import MappingProxyType
from typing import Any, cast
from uuid import UUID, uuid5
from zipfile import BadZipFile

import msoffcrypto
import openpyxl
from msoffcrypto.exceptions import (
    DecryptionError,
    FileFormatError,
    InvalidKeyError,
    ParseError,
)
from openpyxl.cell.cell import Cell
from openpyxl.packaging.relationship import get_dependents, get_rels_path
from openpyxl.utils import column_index_from_string, get_column_letter, range_boundaries
from openpyxl.utils.datetime import from_excel
from openpyxl.utils.exceptions import InvalidFileException
from openpyxl.workbook.workbook import Workbook
from openpyxl.worksheet._read_only import ReadOnlyWorksheet
from openpyxl.worksheet.table import Table
from openpyxl.xml.functions import fromstring

from src.domain.roster import CustomFieldDefinition, CustomFieldType, Meal, normalize_phone

from .contracts import (
    DateNormalization,
    ImportMappingProfile,
    ObservedDeliveryAssignment,
    PersonObservation,
    PhoneNormalization,
    ProfileField,
    SourceReference,
)

_MAX_COLUMNS = 256  # Legacy worksheet header discovery only.
_MAX_XLSX_COLUMN = column_index_from_string("XFD")
_MAX_DATA_ROWS = 50_000
_BLANK_ROW_STOP = 100
_OBSERVATION_NAMESPACE = UUID("bd5ecf05-6f24-47d8-b84d-a5de9b08d094")
_COLUMN_PATTERN = re.compile(r"^[A-Za-z]{1,3}$")
_RRN_DASHED_PATTERN = re.compile(
    r"^(?P<birth>\d{6})-(?P<discriminator>\d)[\d*xX●•]{0,6}$"
)
_RRN_COMPACT_PATTERN = re.compile(
    r"^(?P<birth>\d{6})(?P<discriminator>\d)[\d*xX●•]{6}$"
)
_RRN_MASKED_DISCRIMINATOR_PATTERN = re.compile(
    r"^\d{6}-[*xX●•][\d*xX●•]{0,6}$"
)
_RRN_CENTURY_BY_DISCRIMINATOR = {
    "0": 1800,
    "9": 1800,
    "1": 1900,
    "2": 1900,
    "5": 1900,
    "6": 1900,
    "3": 2000,
    "4": 2000,
    "7": 2000,
    "8": 2000,
}
_DATE_SEPARATORS = re.compile(r"[./년월\-]+")
_INVALID = object()
_PROFILE_FIELD_LABELS = {
    ProfileField.NAME: "성명",
    ProfileField.BIRTH_DATE: "생년월일",
    ProfileField.SCHOOL_TYPE: "학교 유형",
    ProfileField.SCHOOL_NAME: "학교명",
    ProfileField.GRADE: "학년",
    ProfileField.CLASS_NAME: "반",
    ProfileField.HOUSEHOLD_HEAD_NAME: "세대주 성명",
    ProfileField.RELATIONSHIP_TO_CHILD: "아동과의 관계",
    ProfileField.JURISDICTION: "관할동",
    ProfileField.PROVIDER: "제공기관",
    ProfileField.ROAD_ADDRESS: "도로명주소",
    ProfileField.DETAIL_ADDRESS: "상세주소",
    ProfileField.TELEPHONE: "전화번호",
    ProfileField.MOBILE_PHONE: "핸드폰",
    ProfileField.NOTE: "비고",
}
_SCHOOL_FIELDS = (
    ProfileField.SCHOOL_TYPE,
    ProfileField.SCHOOL_NAME,
    ProfileField.GRADE,
)
_ADDRESS_FIELDS = (
    ProfileField.ROAD_ADDRESS,
    ProfileField.DETAIL_ADDRESS,
)


class WorkbookImportError(ValueError):
    pass


class WorkbookImportPasswordRequiredError(WorkbookImportError):
    pass


class WorkbookImportPasswordError(WorkbookImportError):
    pass


class WorkbookStructureError(WorkbookImportError):
    def __init__(
        self, message: str, *, tables: tuple[WorkbookTable, ...] = ()
    ) -> None:
        super().__init__(message)
        self.tables = tables


@dataclass(frozen=True, slots=True)
class SourceColumn:
    index: int
    letter: str
    header: str

    def __post_init__(self) -> None:
        if isinstance(self.index, bool) or not isinstance(self.index, int):
            raise TypeError("원본 열 index는 정수여야 합니다.")
        if self.index < 1:
            raise ValueError("원본 열 index는 1 이상이어야 합니다.")
        if self.letter != get_column_letter(self.index):
            raise ValueError("원본 열 문자와 index가 일치하지 않습니다.")

    @property
    def label(self) -> str:
        return f"{self.letter} · {self.header or '(빈 헤더)'}"


def custom_column_names(columns: tuple[SourceColumn, ...]) -> dict[str, str]:
    """Preserve headers while distinguishing duplicate and unnamed source columns."""
    headers = Counter(column.header.strip().casefold() for column in columns)
    reserved = set(headers)
    used: set[str] = set()
    result = {}
    for column in columns:
        name = column.header.strip()
        if not name or headers[name.casefold()] > 1:
            name = f"{name} ({column.letter}열)" if name else f"추가 항목 ({column.letter}열)"
            while name.casefold() in reserved or name.casefold() in used:
                name += f" ({column.letter}열)"
        result[column.letter] = name
        used.add(name.casefold())
    return result


@dataclass(frozen=True, slots=True)
class WorkbookPreviewRow:
    row_number: int
    values: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WorkbookTable:
    name: str
    sheet_name: str
    ref: str
    header_row: int
    data_start_row: int
    data_end_row: int
    min_col: int
    max_col: int
    header_row_count: int = 1


@dataclass(frozen=True, slots=True)
class WorkbookInspection:
    source_filename: str
    sheet_names: tuple[str, ...]
    selected_sheet: str
    header_row: int
    columns: tuple[SourceColumn, ...]
    preview_rows: tuple[WorkbookPreviewRow, ...]
    header_fingerprint: str
    tables: tuple[WorkbookTable, ...] = ()
    selected_table: str | None = None


@dataclass(frozen=True, slots=True)
class ImportIssue:
    row_number: int | None
    column: str | None
    message: str
    blocking: bool = True
    current_value: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.blocking, bool):
            raise TypeError("blocking은 bool이어야 합니다.")
        if self.current_value is not None and not isinstance(
            self.current_value,
            str,
        ):
            raise TypeError("current_value는 문자열 또는 None이어야 합니다.")

    @property
    def location(self) -> str:
        parts = []
        if self.row_number is not None:
            parts.append(f"{self.row_number}행")
        if self.column is not None:
            parts.append(f"{self.column}열")
        return " ".join(parts) or "파일"


@dataclass(frozen=True, slots=True)
class AutomaticSourceResolution:
    """One conflicting source field resolved without inventing an effective date."""

    observation_id: UUID
    field: str
    field_label: str
    row_values: tuple[tuple[int, str], ...]
    selected_row: int
    selected_value: str
    selection_rule: str
    meal: Meal | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.observation_id, UUID):
            raise TypeError("observation_id는 UUID여야 합니다.")
        if not isinstance(self.field, str) or not self.field.strip():
            raise ValueError("field는 비어 있지 않은 문자열이어야 합니다.")
        if not isinstance(self.field_label, str) or not self.field_label.strip():
            raise ValueError("field_label은 비어 있지 않은 문자열이어야 합니다.")
        row_values = tuple(self.row_values)
        if len(row_values) < 2:
            raise ValueError("자동 정리 기록에는 둘 이상의 원본 행 값이 필요합니다.")
        if any(
            isinstance(row, bool)
            or not isinstance(row, int)
            or row < 1
            or not isinstance(value, str)
            or not value
            for row, value in row_values
        ):
            raise ValueError("자동 정리 원본 행과 값이 올바르지 않습니다.")
        rows = tuple(row for row, _value in row_values)
        if rows != tuple(sorted(rows)) or len(set(rows)) != len(rows):
            raise ValueError("자동 정리 원본 행은 중복 없이 오름차순이어야 합니다.")
        if (self.selected_row, self.selected_value) not in row_values:
            raise ValueError("선택한 현재값은 원본 행 값에 포함되어야 합니다.")
        if self.selection_rule not in {
            "later_excel_row",
            "more_complete_address",
        }:
            raise ValueError("알 수 없는 자동 정리 기준입니다.")
        if self.meal is not None and not isinstance(self.meal, Meal):
            raise TypeError("meal은 Meal 또는 None이어야 합니다.")
        object.__setattr__(self, "field", self.field.strip())
        object.__setattr__(self, "field_label", self.field_label.strip())
        object.__setattr__(self, "row_values", row_values)

    @property
    def superseded_row_values(self) -> tuple[tuple[int, str], ...]:
        return tuple(
            item for item in self.row_values if item[0] != self.selected_row
        )

    @property
    def previous_row_values(self) -> tuple[tuple[int, str], ...]:
        if self.selection_rule != "later_excel_row":
            return ()
        return self.superseded_row_values

    def manifest(self) -> Mapping[str, object]:
        return {
            "observation_id": str(self.observation_id),
            "kind": (
                "delivery_assignment"
                if self.field in {"driver", "sequence"}
                else "contact"
                if self.field
                in {
                    ProfileField.TELEPHONE.value,
                    ProfileField.MOBILE_PHONE.value,
                }
                else "address"
            ),
            "field": self.field,
            "label": self.field_label,
            "meal": None if self.meal is None else self.meal.value,
            "source_rows": [row for row, _value in self.row_values],
            "selection_rule": self.selection_rule,
            "effective_on": None,
            "effective_on_status": "unknown_from_source",
            "superseded_source_values": [
                {"row": row, "value": value}
                for row, value in self.superseded_row_values
            ],
            "previous": [
                {"row": row, "value": value}
                for row, value in self.previous_row_values
            ],
            "current": {
                "row": self.selected_row,
                "value": self.selected_value,
            },
        }


@dataclass(frozen=True, slots=True)
class WorkbookImportPreview:
    source_filename: str
    sha256: str
    byte_size: int
    sheet_name: str
    header_fingerprint: str
    profile: ImportMappingProfile
    observations: tuple[PersonObservation, ...]
    issues: tuple[ImportIssue, ...]
    sample_rows: tuple[WorkbookPreviewRow, ...]
    corrected_cells: tuple[tuple[int, str], ...] = ()
    automatic_source_resolutions: tuple[AutomaticSourceResolution, ...] = ()
    custom_fields: tuple[CustomFieldDefinition, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.profile, ImportMappingProfile):
            raise TypeError("profile은 ImportMappingProfile이어야 합니다.")
        object.__setattr__(self, "observations", tuple(self.observations))
        object.__setattr__(self, "issues", tuple(self.issues))
        object.__setattr__(self, "sample_rows", tuple(self.sample_rows))
        object.__setattr__(self, "corrected_cells", tuple(self.corrected_cells))
        automatic = tuple(self.automatic_source_resolutions)
        if any(not isinstance(item, AutomaticSourceResolution) for item in automatic):
            raise TypeError(
                "automatic_source_resolutions에는 AutomaticSourceResolution만 허용됩니다."
            )
        object.__setattr__(self, "automatic_source_resolutions", automatic)
        custom_fields = tuple(self.custom_fields)
        if any(not isinstance(item, CustomFieldDefinition) for item in custom_fields):
            raise TypeError("custom_fields에는 CustomFieldDefinition만 허용됩니다.")
        object.__setattr__(self, "custom_fields", custom_fields)

    @property
    def can_continue(self) -> bool:
        return bool(self.observations) and not any(
            issue.blocking for issue in self.issues
        )

    @property
    def blocking_issues(self) -> tuple[ImportIssue, ...]:
        return tuple(issue for issue in self.issues if issue.blocking)

    @property
    def notices(self) -> tuple[ImportIssue, ...]:
        return tuple(issue for issue in self.issues if not issue.blocking)


@dataclass(slots=True)
class _RawObservation:
    row_number: int
    profile_values: dict[ProfileField, str | date | None]
    meals: tuple[Meal, ...]
    assignments: tuple[ObservedDeliveryAssignment, ...]
    external_reference: str | None
    custom_values: dict[UUID, str | None]


@dataclass(frozen=True, slots=True)
class _CellInput:
    value: object
    data_type: str | None
    number_format: str


def inspect_workbook(
    path: str | Path,
    *,
    password: str | None = None,
    sheet_name: str | None = None,
    header_row: int = 1,
    data_start_row: int | None = None,
    preview_limit: int = 8,
    require_table: bool = False,
    table_name: str | None = None,
) -> WorkbookInspection:
    """Read only structure and a small display preview for mapping."""

    source_path = _source_path(path)
    header_row = _positive_int(header_row, "헤더 행")
    preview_limit = _positive_int(preview_limit, "미리보기 행 수")
    start_row = header_row + 1 if data_start_row is None else _positive_int(
        data_start_row,
        "데이터 시작 행",
    )
    if start_row <= header_row:
        raise ValueError("데이터 시작 행은 헤더 행보다 뒤여야 합니다.")
    table_mode = require_table or table_name is not None
    with _open_workbook(source_path, password or "") as workbook:
        tables = _workbook_tables(workbook) if table_mode else ()
        try:
            selected_table = _select_table(tables, table_name) if table_mode else None
            if selected_table:
                _validate_table(selected_table)
            worksheet = (
                workbook[selected_table.sheet_name]
                if selected_table
                else _select_sheet(workbook, sheet_name)
            )
            if selected_table:
                header_row = selected_table.header_row
                start_row = selected_table.data_start_row
                columns = _read_columns(
                    worksheet, header_row, selected_table.min_col, selected_table.max_col
                )
            else:
                columns = _read_columns(worksheet, header_row)
        except WorkbookStructureError as error:
            error.tables = tables
            raise
        fingerprint = _header_fingerprint(header_row, columns)
        rows = _preview_rows(
            worksheet,
            start_row,
            len(columns),
            preview_limit,
            min_col=selected_table.min_col if selected_table else 1,
            end_row=selected_table.data_end_row if selected_table else None,
        )
        return WorkbookInspection(
            source_filename=source_path.name,
            sheet_names=tuple(workbook.sheetnames),
            selected_sheet=worksheet.title,
            header_row=header_row,
            columns=columns,
            preview_rows=rows,
            header_fingerprint=fingerprint,
            tables=tables,
            selected_table=selected_table.name if selected_table else None,
        )


def parse_workbook(
    path: str | Path,
    profile: ImportMappingProfile,
    *,
    sheet_name: str | None = None,
    password: str | None = None,
    sample_limit: int = 8,
    cell_overrides: Mapping[tuple[int, str], object] | None = None,
    table_name: str | None = None,
) -> WorkbookImportPreview:
    """Parse mapped cells with optional in-memory corrections."""

    if not isinstance(profile, ImportMappingProfile):
        raise TypeError("profile은 ImportMappingProfile이어야 합니다.")
    overrides = _normalize_cell_overrides(cell_overrides)
    source_path = _source_path(path)
    digest, byte_size = _file_identity(source_path)
    issues: list[ImportIssue] = []
    automatic_source_resolutions: list[AutomaticSourceResolution] = []
    with _open_workbook(source_path, password or "") as workbook:
        selected_table = (
            _select_table(_workbook_tables(workbook), table_name)
            if table_name is not None
            else None
        )
        if selected_table:
            _validate_table(selected_table)
        if (
            selected_table is not None
            and sheet_name is not None
            and sheet_name != selected_table.sheet_name
        ):
            raise WorkbookStructureError("선택한 표와 시트가 일치하지 않습니다.")
        worksheet = (
            workbook[selected_table.sheet_name]
            if selected_table
            else _select_sheet(workbook, sheet_name)
        )
        if selected_table:
            profile = replace(
                profile,
                header_row=selected_table.header_row,
                data_start_row=selected_table.data_start_row,
            )
            columns = _read_columns(
                worksheet,
                selected_table.header_row,
                selected_table.min_col,
                selected_table.max_col,
            )
            for row_number, letter in overrides:
                column_number = column_index_from_string(letter)
                inside_body = (
                    selected_table.data_start_row <= row_number <= selected_table.data_end_row
                    and selected_table.min_col <= column_number <= selected_table.max_col
                )
                if not inside_body:
                    raise WorkbookStructureError(
                        f"선택한 표의 데이터 범위 밖 셀은 수정할 수 없습니다: {letter}{row_number}"
                    )
        else:
            columns = _read_columns(worksheet, profile.header_row)
        fingerprint = _header_fingerprint(profile.header_row, columns)
        column_indexes = _resolve_profile_columns(profile, columns)
        names = custom_column_names(columns)
        custom_fields = tuple(
            CustomFieldDefinition(
                definition_id,
                profile.custom_field_names.get(
                    definition_id, names[get_column_letter(column_indexes[token])]
                ),
                CustomFieldType.TEXT,
                order,
            )
            for order, (definition_id, token) in enumerate(profile.custom_field_columns.items())
        )
        sample_rows = _preview_rows(
            worksheet,
            profile.data_start_row,
            len(columns),
            sample_limit,
            overrides,
            min_col=selected_table.min_col if selected_table else 1,
            end_row=selected_table.data_end_row if selected_table else None,
        )
        raw = _read_observations(
            worksheet,
            workbook,
            source_path.name,
            profile,
            column_indexes,
            issues,
            overrides,
            end_row=selected_table.data_end_row if selected_table else None,
        )
    observations = _merge_observations(
        raw,
        source_path.name,
        worksheet.title,
        digest,
        profile,
        column_indexes,
        issues,
        automatic_source_resolutions,
    )
    if not observations and not issues:
        issues.append(ImportIssue(None, None, "가져올 데이터 행이 없습니다."))
    return WorkbookImportPreview(
        source_filename=source_path.name,
        sha256=digest,
        byte_size=byte_size,
        sheet_name=worksheet.title,
        header_fingerprint=fingerprint,
        profile=profile,
        observations=observations,
        issues=tuple(issues),
        sample_rows=sample_rows,
        corrected_cells=tuple(sorted(overrides)),
        automatic_source_resolutions=tuple(automatic_source_resolutions),
        custom_fields=custom_fields,
    )


def proposed_profile(
    inspection: WorkbookInspection,
    profiles: Sequence[object],
) -> ImportMappingProfile | None:
    """Suggest one profile only when the complete header fingerprint is unique."""

    from .profile_history import StoredImportProfile

    matches = tuple(
        item.profile
        for item in profiles
        if isinstance(item, StoredImportProfile)
        and item.header_fingerprint == inspection.header_fingerprint
    )
    return matches[0] if len(matches) == 1 else None


@contextmanager
def _open_workbook(
    path: Path,
    password: str,
    *,
    read_only: bool = True,
) -> Iterator[Workbook]:
    try:
        workbook = openpyxl.load_workbook(
            path,
            data_only=False,
            read_only=read_only,
        )
    except OSError:
        raise
    except (BadZipFile, InvalidFileException, KeyError, ValueError):
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
            with path.open("rb") as encrypted:
                try:
                    office_file = msoffcrypto.OfficeFile(encrypted)
                    encrypted_format = office_file.is_encrypted()
                except (FileFormatError, ParseError) as error:
                    raise WorkbookImportError(
                        "Excel 파일이 손상되었거나 지원하지 않는 형식입니다."
                    ) from error
                if not encrypted_format:
                    raise WorkbookImportError("Excel 파일이 손상되었습니다.")
                if not password:
                    raise WorkbookImportPasswordRequiredError(
                        "암호로 보호된 Excel입니다. 파일 비밀번호를 입력해주세요."
                    )
                try:
                    office_file.load_key(password=password)
                    office_file.decrypt(decrypted)
                except (DecryptionError, InvalidKeyError, ValueError) as error:
                    raise WorkbookImportPasswordError(
                        "Excel 파일 비밀번호가 올바르지 않습니다."
                    ) from error
            decrypted.seek(0)
            workbook = openpyxl.load_workbook(
                decrypted,
                data_only=False,
                read_only=read_only,
            )
        except OSError:
            raise
        except (
            WorkbookImportPasswordRequiredError,
            WorkbookImportPasswordError,
            WorkbookImportError,
        ):
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
        ) as error:
            raise WorkbookImportError(
                "암호 해제된 Excel 파일이 손상되었습니다."
            ) from error
        try:
            yield workbook
        finally:
            workbook.close()
    finally:
        decrypted.close()


def _source_path(value: str | Path) -> Path:
    path = Path(value).resolve()
    try:
        file_stat = path.stat()
    except FileNotFoundError as error:
        raise FileNotFoundError(f"Excel 파일이 없습니다: {path.name}") from error
    if not S_ISREG(file_stat.st_mode):
        raise WorkbookImportError("선택한 Excel 경로가 파일이 아닙니다.")
    if path.suffix.casefold() not in {".xlsx", ".xlsm"}:
        raise WorkbookImportError(".xlsx 또는 .xlsm Excel 파일만 지원합니다.")
    return path


def _select_sheet(workbook: Workbook, sheet_name: str | None):
    if not workbook.sheetnames:
        raise WorkbookStructureError("Excel에 시트가 없습니다.")
    selected = workbook.sheetnames[0] if sheet_name is None else sheet_name
    if selected not in workbook.sheetnames:
        raise WorkbookStructureError(f"선택한 시트를 찾을 수 없습니다: {selected}")
    return workbook[selected]


def _workbook_tables(workbook: Workbook) -> tuple[WorkbookTable, ...]:
    result: list[WorkbookTable] = []
    for worksheet in workbook.worksheets:
        for table in _read_worksheet_tables(cast(ReadOnlyWorksheet, worksheet)):
            try:
                min_col, min_row, max_col, max_row = range_boundaries(table.ref)
                if not all(
                    isinstance(value, int) and value >= 1
                    for value in (min_col, min_row, max_col, max_row)
                ) or min_col > max_col or min_row > max_row:
                    raise ValueError("잘못된 표 범위")
            except (TypeError, ValueError) as error:
                raise WorkbookStructureError(
                    f"'{table.name}' 표 범위를 읽을 수 없습니다."
                ) from error
            totals_rows = table.totalsRowCount
            if totals_rows is None:
                totals_rows = 1 if table.totalsRowShown else 0
            data_end = max_row - totals_rows
            result.append(
                WorkbookTable(
                    table.name, worksheet.title, table.ref,
                    min_row, min_row + 1, data_end, min_col, max_col,
                    table.headerRowCount if table.headerRowCount is not None else 1,
                )
            )
    return tuple(result)


def _read_worksheet_tables(worksheet: ReadOnlyWorksheet) -> Iterator[Table]:
    """Read structured table definitions from read-only worksheet relationships."""
    internal = cast(Any, worksheet)
    archive = internal.parent._archive
    relationships_path = get_rels_path(internal._worksheet_path)
    if relationships_path not in archive.namelist():
        return
    try:
        relationships = get_dependents(archive, relationships_path)
        for relationship in relationships.find(Table._rel_type):
            yield Table.from_tree(fromstring(archive.read(relationship.target)))
    except (BadZipFile, KeyError, TypeError, ValueError) as error:
        raise WorkbookStructureError(
            f"'{worksheet.title}' 시트의 Excel 표 정보를 읽을 수 없습니다."
        ) from error


def _validate_table(table: WorkbookTable) -> None:
    if table.header_row_count != 1:
        raise WorkbookStructureError(f"'{table.name}' 표에 머리글이 없습니다. 머리글이 있는 Excel 표로 바꿔주세요.")
    if table.data_end_row < table.header_row:
        raise WorkbookStructureError(f"'{table.name}' 표의 머리글·합계행 범위가 올바르지 않습니다.")
    if table.max_col > _MAX_XLSX_COLUMN:
        raise WorkbookStructureError(
            f"'{table.name}' 표가 Excel의 마지막 열 XFD를 벗어났습니다."
        )
    if table.data_end_row - table.header_row > _MAX_DATA_ROWS:
        raise WorkbookStructureError(f"'{table.name}' 표는 {_MAX_DATA_ROWS:,}행까지만 가져올 수 있습니다.")


def _select_table(tables: tuple[WorkbookTable, ...], name: str | None) -> WorkbookTable:
    if not tables:
        raise WorkbookStructureError("Excel 표가 없습니다. Excel에서 명단 범위를 '삽입 → 표'로 만든 뒤 다시 선택해주세요.")
    if name is None:
        return next((table for table in tables if table.name == "명단"), tables[0])
    for table in tables:
        if table.name == name:
            return table
    raise WorkbookStructureError(f"선택한 Excel 표를 찾을 수 없습니다: {name}")


def _read_columns(
    worksheet, header_row: int, min_col: int = 1, max_col: int | None = None
) -> tuple[SourceColumn, ...]:
    bounded = max_col is not None
    max_col = _MAX_COLUMNS if max_col is None else max_col
    cells = next(
        worksheet.iter_rows(
            min_row=header_row,
            max_row=header_row,
            min_col=min_col,
            max_col=max_col,
        ),
        (),
    )
    headers = tuple(_display_value(cell.value) for cell in cells)
    last = max(
        (index for index, header in enumerate(headers, start=1) if header),
        default=0,
    )
    if last == 0:
        raise WorkbookStructureError(f"{header_row}행에서 헤더를 찾지 못했습니다.")
    last_column = max_col if bounded else min_col + last - 1
    return tuple(
        SourceColumn(index, get_column_letter(index), headers[index - min_col])
        for index in range(min_col, last_column + 1)
    )


def _header_fingerprint(
    header_row: int,
    columns: tuple[SourceColumn, ...],
) -> str:
    payload = {
        "header_row": header_row,
        "columns": [
            {"letter": column.letter, "header": column.header}
            for column in columns
        ],
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _preview_rows(
    worksheet,
    start_row: int,
    column_count: int,
    limit: int,
    overrides: Mapping[tuple[int, str], object] | None = None,
    *,
    min_col: int = 1,
    end_row: int | None = None,
) -> tuple[WorkbookPreviewRow, ...]:
    result: list[WorkbookPreviewRow] = []
    max_row = (
        end_row
        if end_row is not None
        else min(worksheet.max_row or start_row, start_row + max(limit * 5, 20))
    )
    if max_row < start_row:
        return ()
    rows = worksheet.iter_rows(
        min_row=start_row,
        max_row=max_row,
        min_col=min_col,
        max_col=min_col + column_count - 1,
    )
    for row_number, row in enumerate(rows, start=start_row):
        inputs = _cell_inputs(row, row_number, overrides or {}, min_col=min_col)
        values = tuple(_display_value(cell.value) for cell in inputs)
        if not any(values):
            continue
        # Read-only worksheets return EmptyCell for blank leading cells. EmptyCell
        # intentionally has no ``row`` attribute, so the iterator position is the
        # reliable source row number.
        result.append(WorkbookPreviewRow(row_number, values))
        if len(result) == limit:
            break
    return tuple(result)


def _normalize_cell_overrides(
    overrides: Mapping[tuple[int, str], object] | None,
) -> Mapping[tuple[int, str], object]:
    if overrides is None:
        return MappingProxyType({})
    if not isinstance(overrides, Mapping):
        raise TypeError("cell_overrides는 행·열 위치와 수정값의 mapping이어야 합니다.")
    normalized: dict[tuple[int, str], object] = {}
    for location, value in overrides.items():
        if not isinstance(location, tuple) or len(location) != 2:
            raise TypeError("수정할 셀 위치는 (행 번호, 열 문자)여야 합니다.")
        row_number, column = location
        if (
            isinstance(row_number, bool)
            or not isinstance(row_number, int)
            or row_number < 1
        ):
            raise ValueError("수정할 셀의 행 번호는 1 이상의 정수여야 합니다.")
        if not isinstance(column, str) or not _COLUMN_PATTERN.fullmatch(column):
            raise ValueError("수정할 셀의 열은 A, B 같은 Excel 열 문자여야 합니다.")
        normalized[(row_number, column.upper())] = value
    return MappingProxyType(normalized)


def _cell_inputs(
    cells: Sequence[Cell],
    row_number: int,
    overrides: Mapping[tuple[int, str], object],
    *,
    min_col: int = 1,
) -> tuple[_CellInput, ...]:
    result: list[_CellInput] = []
    for column_index, cell in enumerate(cells, start=min_col):
        key = (row_number, get_column_letter(column_index))
        if key in overrides:
            value = overrides[key]
            data_type = (
                "f"
                if isinstance(value, str) and value.lstrip().startswith("=")
                else "s"
            )
            result.append(_CellInput(value, data_type, "General"))
        else:
            result.append(
                _CellInput(
                    cell.value,
                    cell.data_type,
                    cell.number_format or "General",
                )
            )
    return tuple(result)


def _resolve_profile_columns(
    profile: ImportMappingProfile,
    columns: tuple[SourceColumn, ...],
) -> Mapping[str, int]:
    tokens = {
        *profile.profile_columns.values(),
        *(
            token
            for candidates in profile.fallback_profile_columns.values()
            for token in candidates
        ),
        *profile.meal_flag_columns.values(),
        *profile.custom_field_columns.values(),
        *profile.ignored_columns,
    }
    for optional in (
        profile.combined_meals_column,
        profile.driver_column,
        profile.sequence_column,
        profile.source_reference_column,
    ):
        if optional is not None:
            tokens.add(optional)
    resolved = {token: _resolve_column(token, columns) for token in tokens}
    return MappingProxyType(resolved)


def _resolve_column(token: str, columns: tuple[SourceColumn, ...]) -> int:
    if _COLUMN_PATTERN.fullmatch(token):
        index = column_index_from_string(token.upper())
        if any(column.index == index for column in columns):
            return index
    key = _text_key(token)
    matches = tuple(column.index for column in columns if _text_key(column.header) == key)
    if not matches:
        raise WorkbookStructureError(f"연결한 원본 열을 찾지 못했습니다: {token}")
    if len(matches) > 1:
        raise WorkbookStructureError(
            f"같은 헤더가 둘 이상이라 열 문자로 다시 연결해야 합니다: {token}"
        )
    return matches[0]


def source_column_letter(token: str, columns: tuple[SourceColumn, ...]) -> str:
    """Resolve stored header or letter mappings with the same rules as parsing."""
    return get_column_letter(_resolve_column(token, columns))


def _read_observations(
    worksheet,
    workbook: Workbook,
    source_filename: str,
    profile: ImportMappingProfile,
    indexes: Mapping[str, int],
    issues: list[ImportIssue],
    overrides: Mapping[tuple[int, str], object],
    *,
    end_row: int | None = None,
) -> list[_RawObservation]:
    mapped_indexes = tuple(sorted(set(indexes.values())))
    max_column = max(mapped_indexes)
    first_row = profile.data_start_row
    declared_last = end_row if end_row is not None else worksheet.max_row or first_row
    last_row = min(declared_last, first_row + _MAX_DATA_ROWS - 1)
    result: list[_RawObservation] = []
    blank_run = 0
    saw_data = False
    rows = worksheet.iter_rows(
        min_row=first_row,
        max_row=last_row,
        min_col=1,
        max_col=max_column,
    )
    for row_number, source_cells in enumerate(rows, start=first_row):
        cells = _cell_inputs(source_cells, row_number, overrides)
        if all(_is_blank(cells[index - 1].value) for index in mapped_indexes):
            blank_run += 1
            if end_row is None and saw_data and blank_run >= _BLANK_ROW_STOP:
                break
            continue
        saw_data = True
        blank_run = 0
        before_issue_count = len(issues)
        raw = _parse_row(
            row_number,
            cells,
            workbook,
            source_filename,
            profile,
            indexes,
            issues,
        )
        if raw is not None and len(issues) == before_issue_count:
            result.append(raw)
    if declared_last > last_row and blank_run < _BLANK_ROW_STOP:
        issues.append(
            ImportIssue(
                last_row,
                None,
                f"한 번에 {_MAX_DATA_ROWS:,}행까지만 가져올 수 있습니다.",
            )
        )
    return result


def _parse_row(
    row_number: int,
    cells: Sequence[_CellInput],
    workbook: Workbook,
    _source_filename: str,
    profile: ImportMappingProfile,
    indexes: Mapping[str, int],
    issues: list[ImportIssue],
) -> _RawObservation | None:
    name_token = profile.profile_columns[ProfileField.NAME]
    raw_name, name_source_column = _candidate_value(
        cells,
        (name_token, *profile.fallback_profile_columns.get(ProfileField.NAME, ())),
        indexes,
        row_number,
        issues,
    )
    if raw_name is _INVALID:
        return None
    name = _profile_value(raw_name, ProfileField.NAME, profile, workbook)
    if not isinstance(name, str) or not name:
        issues.append(
            ImportIssue(
                row_number,
                name_source_column,
                "성명이 없어 이 행은 가져오지 않습니다.",
                blocking=False,
                current_value="",
            )
        )
        return None

    values: dict[ProfileField, str | date | None] = {ProfileField.NAME: name}
    if profile.provider_value is not None:
        values[ProfileField.PROVIDER] = profile.provider_value
    for field, token in profile.profile_columns.items():
        if field is ProfileField.NAME:
            continue
        candidate_tokens = (token, *profile.fallback_profile_columns.get(field, ()))
        raw, source_column = _candidate_value(
            cells,
            candidate_tokens,
            indexes,
            row_number,
            issues,
        )
        if raw is _INVALID:
            continue
        try:
            values[field] = _profile_value(raw, field, profile, workbook)
        except ValueError as error:
            issues.append(
                ImportIssue(
                    row_number,
                    source_column,
                    str(error),
                    current_value=_display_value(raw),
                )
            )

    meals = _row_meals(cells, profile, indexes, row_number, issues)
    assignment = _row_assignment(cells, profile, indexes, meals, row_number, issues)
    external_reference = _optional_cell_text(
        cells,
        profile.source_reference_column,
        indexes,
        row_number,
        issues,
    )
    custom_values = {
        definition_id: _optional_cell_text(cells, token, indexes, row_number, issues, literal=True)
        for definition_id, token in profile.custom_field_columns.items()
    }
    if any(issue.row_number == row_number for issue in issues):
        return None
    return _RawObservation(
        row_number,
        values,
        meals,
        () if assignment is None else (assignment,),
        external_reference,
        custom_values,
    )


def _candidate_value(
    cells: Sequence[_CellInput],
    tokens: tuple[str, ...],
    indexes: Mapping[str, int],
    row_number: int,
    issues: list[ImportIssue],
) -> tuple[object, str]:
    primary = tokens[0]
    for token in tokens:
        column = get_column_letter(indexes[token])
        cell = cells[indexes[token] - 1]
        value = _safe_cell_value(cell, row_number, column, issues)
        if value is _INVALID:
            return _INVALID, column
        if not _is_blank(value):
            return value, column
    return None, get_column_letter(indexes[primary])


def _safe_cell_value(
    cell: _CellInput,
    row_number: int,
    column: str,
    issues: list[ImportIssue],
) -> object:
    if cell.data_type == "f":
        issues.append(
            ImportIssue(
                row_number,
                column,
                "수식 셀은 명단 값으로 가져올 수 없습니다. 값으로 붙여넣어 주세요.",
                current_value=_display_value(cell.value),
            )
        )
        return _INVALID
    return cell.value


def _profile_value(
    value: object,
    field: ProfileField,
    profile: ImportMappingProfile,
    workbook: Workbook,
) -> str | date | None:
    if field is ProfileField.BIRTH_DATE:
        return _date_value(value, profile.date_normalization, workbook)
    text = _optional_text(value)
    if text is None:
        return None
    if field in (ProfileField.TELEPHONE, ProfileField.MOBILE_PHONE):
        if not normalize_phone(text):
            return None
        return _phone_value(text, profile.phone_normalization)
    return text


def _date_value(
    value: object,
    mode: DateNormalization,
    workbook: Workbook,
) -> date | None:
    if _is_blank(value):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = _optional_text(value)
    assert text is not None
    rrn_birth_date = _rrn_birth_date(value, text)
    if rrn_birth_date is not None:
        return rrn_birth_date
    digits = "".join(character for character in text if character.isdigit())
    integer_number = (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and (not isinstance(value, float) or value.is_integer())
    )
    short_digits = (
        digits.zfill(6) if integer_number and len(digits) == 5 else digits
    )
    if len(short_digits) == 6:
        try:
            return _six_digit_birth_date(short_digits)
        except ValueError as error:
            # A five-digit numeric value can still be an unformatted Excel date
            # serial. Six explicit digits, however, are an invalid YYMMDD value.
            if not (integer_number and len(digits) == 5 and mode is DateNormalization.AUTO):
                raise ValueError(
                    "6자리 생년월일의 날짜가 올바르지 않습니다."
                ) from error
    if len(digits) == 8:
        try:
            return date(int(digits[:4]), int(digits[4:6]), int(digits[6:]))
        except ValueError as error:
            raise ValueError(f"생년월일 값이 올바르지 않습니다: {text}") from error
    if mode is DateNormalization.AUTO and isinstance(value, (int, float)):
        try:
            converted = from_excel(value, workbook.epoch)
        except (TypeError, ValueError, OverflowError) as error:
            raise ValueError(f"생년월일 값이 올바르지 않습니다: {text}") from error
        return converted.date() if isinstance(converted, datetime) else converted
    if mode is DateNormalization.AUTO:
        normalized = _DATE_SEPARATORS.sub("-", text).strip("-일 ")
        try:
            return date.fromisoformat(normalized)
        except ValueError:
            pass
    raise ValueError(
        "생년월일은 YYMMDD, YYYYMMDD, 날짜 셀 또는 주민등록번호 형식이어야 합니다."
    )


def _six_digit_birth_date(digits: str) -> date:
    current_year = date.today().year
    current_century = current_year - current_year % 100
    year = current_century + int(digits[:2])
    if year > current_year:
        year -= 100
    return date(year, int(digits[2:4]), int(digits[4:]))


def _rrn_birth_date(value: object, text: str) -> date | None:
    compact = "".join(text.split())
    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and (not isinstance(value, float) or value.is_integer())
        and len(compact) == 12
        and compact.isdigit()
    ):
        # Excel 숫자 셀은 2000~2009년생 주민번호의 첫 0을 버릴 수 있다.
        compact = "0" + compact

    match = _RRN_DASHED_PATTERN.fullmatch(compact)
    if match is None:
        match = _RRN_COMPACT_PATTERN.fullmatch(compact)
    if match is None:
        if _RRN_MASKED_DISCRIMINATOR_PATTERN.fullmatch(compact):
            raise ValueError(
                "주민등록번호 뒷자리 첫 숫자가 가려져 있어 출생연도를 알 수 없습니다."
            )
        return None

    birth_digits = match.group("birth")
    century = _RRN_CENTURY_BY_DISCRIMINATOR[match.group("discriminator")]
    try:
        return date(
            century + int(birth_digits[:2]),
            int(birth_digits[2:4]),
            int(birth_digits[4:]),
        )
    except ValueError as error:
        raise ValueError(
            "주민등록번호 앞 6자리의 날짜가 올바르지 않습니다."
        ) from error


def _phone_value(value: str, mode: PhoneNormalization) -> str:
    if mode is PhoneNormalization.PRESERVE:
        return value
    digits = normalize_phone(value)
    if not digits:
        raise ValueError(f"전화번호에 숫자가 없습니다: {value}")
    if mode is PhoneNormalization.DIGITS:
        return digits
    if len(digits) == 10 and digits.startswith("10"):
        digits = "0" + digits
    if len(digits) == 11:
        return f"{digits[:3]}-{digits[3:7]}-{digits[7:]}"
    if len(digits) == 10 and digits.startswith("02"):
        return f"{digits[:2]}-{digits[2:6]}-{digits[6:]}"
    if len(digits) == 10:
        return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"
    return digits


def _row_meals(
    cells: Sequence[_CellInput],
    profile: ImportMappingProfile,
    indexes: Mapping[str, int],
    row_number: int,
    issues: list[ImportIssue],
) -> tuple[Meal, ...]:
    if profile.default_meals:
        return profile.default_meals
    if profile.combined_meals_column is not None:
        token = profile.combined_meals_column
        column = get_column_letter(indexes[token])
        value = _safe_cell_value(
            cells[indexes[token] - 1],
            row_number,
            column,
            issues,
        )
        if value is _INVALID:
            return ()
        return _combined_meals(value, row_number, column, issues)
    selected: set[Meal] = set()
    true_keys = {_text_key(value) for value in profile.meal_true_values}
    false_keys = {_text_key(value) for value in profile.meal_false_values}
    for meal, token in profile.meal_flag_columns.items():
        column = get_column_letter(indexes[token])
        value = _safe_cell_value(
            cells[indexes[token] - 1],
            row_number,
            column,
            issues,
        )
        if value is _INVALID:
            continue
        key = _text_key(_display_value(value))
        if key in true_keys:
            selected.add(meal)
        elif key not in false_keys:
            issues.append(
                ImportIssue(
                    row_number,
                    column,
                    f"식종 값 '{_display_value(value)}'은 급식을 받는 값이나 받지 않는 값 설정에 없습니다.",
                    current_value=_display_value(value),
                )
            )
    return tuple(meal for meal in Meal if meal in selected)


def _combined_meals(
    value: object,
    row_number: int,
    column: str,
    issues: list[ImportIssue],
) -> tuple[Meal, ...]:
    text = _display_value(value)
    if not text:
        return ()
    aliases = {
        "조식": Meal.BREAKFAST,
        "아침": Meal.BREAKFAST,
        "중식": Meal.LUNCH,
        "점심": Meal.LUNCH,
        "석식": Meal.DINNER,
        "저녁": Meal.DINNER,
    }
    text_key = _text_key(text)
    selected = {
        meal
        for alias, meal in aliases.items()
        if _text_key(alias) in text_key
    }
    if not selected:
        issues.append(
            ImportIssue(
                row_number,
                column,
                "알 수 없는 식종 값입니다. 셀 안에 조식, 중식 또는 석식이 포함되어야 합니다.",
                current_value=text,
            )
        )
    return tuple(meal for meal in Meal if meal in selected)


def _row_assignment(
    cells: Sequence[_CellInput],
    profile: ImportMappingProfile,
    indexes: Mapping[str, int],
    meals: tuple[Meal, ...],
    row_number: int,
    issues: list[ImportIssue],
) -> ObservedDeliveryAssignment | None:
    driver = _optional_cell_text(
        cells,
        profile.driver_column,
        indexes,
        row_number,
        issues,
    )
    sequence = _optional_cell_text(
        cells,
        profile.sequence_column,
        indexes,
        row_number,
        issues,
        literal=True,
    )
    if driver is None and sequence is None:
        return None
    return ObservedDeliveryAssignment(
        meals[0] if len(meals) == 1 else None,
        driver,
        sequence,
    )


def _optional_cell_text(
    cells: Sequence[_CellInput],
    token: str | None,
    indexes: Mapping[str, int],
    row_number: int,
    issues: list[ImportIssue],
    *,
    literal: bool = False,
) -> str | None:
    if token is None:
        return None
    cell = cells[indexes[token] - 1]
    column = get_column_letter(indexes[token])
    value = _safe_cell_value(cell, row_number, column, issues)
    if value is _INVALID:
        return None
    text = _literal_cell_text(cell) if literal else _optional_text(value)
    return text or None


def _literal_cell_text(cell: _CellInput) -> str:
    value = cell.value
    if isinstance(value, int) and re.fullmatch(r"0+", cell.number_format or ""):
        return str(value).zfill(len(cell.number_format))
    return _display_value(value).strip()


def _merge_observations(
    rows: list[_RawObservation],
    source_filename: str,
    sheet_name: str,
    file_sha256: str,
    profile: ImportMappingProfile,
    indexes: Mapping[str, int],
    issues: list[ImportIssue],
    automatic_source_resolutions: list[AutomaticSourceResolution],
) -> tuple[PersonObservation, ...]:
    groups: dict[tuple[object, ...], list[_RawObservation]] = {}
    for row in rows:
        name = str(row.profile_values[ProfileField.NAME])
        birth = row.profile_values.get(ProfileField.BIRTH_DATE)
        key = (
            (_comparison_key(name), birth)
            if isinstance(birth, date)
            else ("row", row.row_number)
        )
        groups.setdefault(key, []).append(row)

    observations: list[PersonObservation] = []
    for grouped_rows in groups.values():
        row_numbers = tuple(item.row_number for item in grouped_rows)
        observation_id = uuid5(
            _OBSERVATION_NAMESPACE,
            f"{file_sha256}/{sheet_name}/"
            + ",".join(str(number) for number in row_numbers),
        )
        group_resolutions: list[AutomaticSourceResolution] = []
        merged_values: dict[ProfileField, str | date | None] = {}
        additional_mobile_phones: tuple[str, ...] = ()
        additional_telephones: tuple[str, ...] = ()
        conflicting = False
        school_values_differ = any(
            len(
                _unique_nonblank_values(
                    tuple(item.profile_values.get(field) for item in grouped_rows)
                )
            )
            > 1
            for field in _SCHOOL_FIELDS
        )
        preferred_school_row = _preferred_school_row(grouped_rows)
        fields = tuple(
            dict.fromkeys(
                field
                for item in grouped_rows
                for field in item.profile_values
            )
        )
        for field in fields:
            nonblank = _unique_nonblank_values(
                tuple(item.profile_values.get(field) for item in grouped_rows)
            )
            if field is ProfileField.NOTE:
                merged_values[field] = (
                    " / ".join(_display_value(value) for value in nonblank)
                    or None
                )
                continue
            if field in _SCHOOL_FIELDS and school_values_differ:
                merged_values[field] = preferred_school_row.profile_values.get(field)
                continue
            if field in {
                ProfileField.TELEPHONE,
                ProfileField.MOBILE_PHONE,
            } and len(nonblank) > 1:
                row_values = tuple(
                    (item.row_number, str(value))
                    for item in grouped_rows
                    if (value := item.profile_values.get(field)) is not None
                )
                latest_first: list[str] = []
                seen_phone_values: set[tuple[object, ...]] = set()
                for _row_number, value in sorted(
                    row_values,
                    key=lambda item: item[0],
                    reverse=True,
                ):
                    key = _phone_comparison_key(value)
                    if key in seen_phone_values:
                        continue
                    seen_phone_values.add(key)
                    latest_first.append(value)
                selected_row, selected_value = max(
                    row_values,
                    key=lambda item: item[0],
                )
                merged_values[field] = selected_value
                if field is ProfileField.MOBILE_PHONE:
                    additional_mobile_phones = tuple(latest_first[1:])
                    field_label = "핸드폰1"
                else:
                    additional_telephones = tuple(latest_first[1:])
                    field_label = "전화번호1"
                group_resolutions.append(
                    _automatic_source_resolution(
                        observation_id,
                        field=field.value,
                        field_label=field_label,
                        row_values=row_values,
                        selected_row=selected_row,
                        selected_value=selected_value,
                        selection_rule="later_excel_row",
                    )
                )
                continue
            if field is ProfileField.JURISDICTION and len(nonblank) > 1:
                row_values = tuple(
                    (item.row_number, value)
                    for item in grouped_rows
                    if (value := item.profile_values.get(field)) is not None
                )
                selected_row, selected_value = max(
                    row_values,
                    key=lambda item: item[0],
                )
                merged_values[field] = selected_value
                group_resolutions.append(
                    _automatic_source_resolution(
                        observation_id,
                        field=field.value,
                        field_label=_PROFILE_FIELD_LABELS[field],
                        row_values=row_values,
                        selected_row=selected_row,
                        selected_value=selected_value,
                        selection_rule="later_excel_row",
                    )
                )
                continue
            if field in _ADDRESS_FIELDS and len(nonblank) > 1:
                row_values = tuple(
                    (item.row_number, value)
                    for item in grouped_rows
                    if (value := item.profile_values.get(field)) is not None
                )
                preferred_address = _preferred_containing_address(nonblank)
                if preferred_address is not None:
                    selected_row = max(
                        row_number
                        for row_number, value in row_values
                        if _comparison_key(value)
                        == _comparison_key(preferred_address)
                    )
                    selected_value = preferred_address
                    selection_rule = "more_complete_address"
                else:
                    selected_row, selected_value = max(
                        row_values,
                        key=lambda item: item[0],
                    )
                    selection_rule = "later_excel_row"
                merged_values[field] = selected_value
                group_resolutions.append(
                    _automatic_source_resolution(
                        observation_id,
                        field=field.value,
                        field_label=_PROFILE_FIELD_LABELS[field],
                        row_values=row_values,
                        selected_row=selected_row,
                        selected_value=selected_value,
                        selection_rule=selection_rule,
                    )
                )
                continue
            if len(nonblank) > 1:
                conflicting = True
                token = profile.profile_columns.get(field)
                column = (
                    None if token is None else get_column_letter(indexes[token])
                )
                label = _PROFILE_FIELD_LABELS[field]
                message = (
                    f"같은 사람으로 묶일 행의 {label} 값이 서로 다릅니다. "
                    "현재 사용할 값 하나로 맞춰주세요. "
                    f"함께 확인할 행: {', '.join(map(str, row_numbers))}"
                )
                for item in grouped_rows:
                    value = item.profile_values.get(field)
                    if value is not None:
                        issues.append(
                            ImportIssue(
                                item.row_number,
                                column,
                                message,
                                current_value=_display_value(value),
                            )
                        )
            merged_values[field] = nonblank[0] if nonblank else None

        assignments: dict[Meal | None, ObservedDeliveryAssignment] = {}
        assignment_rows: dict[
            Meal | None,
            list[tuple[_RawObservation, ObservedDeliveryAssignment]],
        ] = {}
        for item in grouped_rows:
            for assignment in item.assignments:
                assignment_rows.setdefault(assignment.meal, []).append(
                    (item, assignment)
                )
        for meal, observed in assignment_rows.items():
            driver_values = _unique_nonblank_values(
                tuple(assignment.driver for _item, assignment in observed)
            )
            sequence_values = _unique_nonblank_values(
                tuple(assignment.sequence for _item, assignment in observed)
            )
            selected_assignment_values: dict[str, str | None] = {}
            for attribute, label, values in (
                ("driver", "배송 담당", driver_values),
                ("sequence", "배송 순서", sequence_values),
            ):
                if len(values) <= 1:
                    selected_assignment_values[attribute] = (
                        None if not values else str(values[0])
                    )
                    continue
                meal_label = "공통" if meal is None else meal.value
                row_values = tuple(
                    (item.row_number, str(value))
                    for item, assignment in observed
                    if (value := getattr(assignment, attribute)) is not None
                )
                selected_row, selected_value = max(
                    row_values,
                    key=lambda item: item[0],
                )
                selected_assignment_values[attribute] = selected_value
                group_resolutions.append(
                    _automatic_source_resolution(
                        observation_id,
                        field=attribute,
                        field_label=f"{meal_label} {label}",
                        row_values=row_values,
                        selected_row=selected_row,
                        selected_value=selected_value,
                        selection_rule="later_excel_row",
                        meal=meal,
                    )
                )
            assignments[meal] = ObservedDeliveryAssignment(
                meal,
                selected_assignment_values["driver"],
                selected_assignment_values["sequence"],
            )
        if conflicting:
            continue
        meal_set = {meal for item in grouped_rows for meal in item.meals}
        external_values = _unique_nonblank_values(
            tuple(item.external_reference for item in grouped_rows)
        )
        external_reference = (
            " / ".join(str(value) for value in external_values) or None
        )
        observations.append(
            PersonObservation(
                observation_id=observation_id,
                source=SourceReference(
                    source_filename,
                    sheet_name,
                    row_numbers,
                    external_reference,
                ),
                profile_values=merged_values,
                meals=tuple(meal for meal in Meal if meal in meal_set),
                assignments=tuple(
                    assignments[key]
                    for key in (None, *Meal)
                    if key in assignments
                ),
                additional_mobile_phones=additional_mobile_phones,
                additional_telephones=additional_telephones,
                custom_values={
                    definition_id: " / ".join(dict.fromkeys(
                        value for item in grouped_rows
                        if (value := item.custom_values.get(definition_id)) is not None
                    )) or None
                    for definition_id in profile.custom_field_columns
                },
            )
        )
        automatic_source_resolutions.extend(group_resolutions)
    return tuple(observations)


def _automatic_source_resolution(
    observation_id: UUID,
    *,
    field: str,
    field_label: str,
    row_values: Sequence[tuple[int, str | date]],
    selected_row: int,
    selected_value: str | date,
    selection_rule: str,
    meal: Meal | None = None,
) -> AutomaticSourceResolution:
    rendered_values = tuple(
        sorted(
            (
                (row_number, _display_value(value))
                for row_number, value in row_values
            ),
            key=lambda item: item[0],
        )
    )
    return AutomaticSourceResolution(
        observation_id=observation_id,
        field=field,
        field_label=field_label,
        row_values=rendered_values,
        selected_row=selected_row,
        selected_value=_display_value(selected_value),
        selection_rule=selection_rule,
        meal=meal,
    )


def _unique_nonblank_values(
    values: Sequence[str | date | None],
) -> tuple[str | date, ...]:
    result: list[str | date] = []
    seen: set[tuple[object, ...]] = set()
    for value in values:
        if value is None:
            continue
        key = _comparison_key(value)
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return tuple(result)


def _comparison_key(value: str | date) -> tuple[object, ...]:
    if isinstance(value, str):
        return ("text", _compact_comparison_text(value))
    return (type(value).__name__, value)


def _phone_comparison_key(value: str) -> tuple[object, ...]:
    digits = normalize_phone(value)
    return ("phone", digits) if digits else _comparison_key(value)


def _compact_comparison_text(value: str) -> str:
    return "".join(value.split()).casefold()


def _preferred_containing_address(
    values: Sequence[str | date],
) -> str | None:
    if any(not isinstance(value, str) for value in values):
        return None
    addresses = tuple(value for value in values if isinstance(value, str))
    candidate = max(addresses, key=lambda value: len(_compact_comparison_text(value)))
    candidate_key = _compact_comparison_text(candidate)
    if all(
        _address_contains(candidate_key, _compact_comparison_text(value))
        for value in addresses
    ):
        return candidate
    return None


def _address_contains(full: str, fragment: str) -> bool:
    start = full.find(fragment)
    while start >= 0:
        end = start + len(fragment)
        starts_inside_number = (
            start > 0 and full[start - 1].isdigit() and fragment[0].isdigit()
        )
        ends_inside_number = (
            end < len(full) and full[end].isdigit() and fragment[-1].isdigit()
        )
        if not starts_inside_number and not ends_inside_number:
            return True
        start = full.find(fragment, start + 1)
    return False


def _preferred_school_row(rows: Sequence[_RawObservation]) -> _RawObservation:
    return max(rows, key=_school_row_rank)


def _school_row_rank(row: _RawObservation) -> tuple[int, int, int]:
    school_text = " ".join(
        str(row.profile_values.get(field) or "")
        for field in (ProfileField.SCHOOL_TYPE, ProfileField.SCHOOL_NAME)
    )
    compact = "".join(school_text.split()).casefold()
    stage = -1
    for keyword, value in (
        ("대학", 5),
        ("고등", 4),
        ("중학", 3),
        ("중등", 3),
        ("초등", 2),
        ("유치", 1),
        ("어린이", 1),
        ("미취학", 0),
    ):
        if keyword in compact:
            stage = value
            break
    grade = _grade_rank(row.profile_values.get(ProfileField.GRADE))
    return stage, grade, row.row_number


def _grade_rank(value: str | date | None) -> int:
    if not isinstance(value, str):
        return -1
    compact = "".join(value.split()).casefold()
    match = re.search(r"\d+", compact)
    if match is not None:
        return int(match.group())
    korean_numbers = {
        "일학년": 1,
        "이학년": 2,
        "삼학년": 3,
        "사학년": 4,
        "오학년": 5,
        "육학년": 6,
    }
    return korean_numbers.get(compact, -1)


def _file_identity(path: Path) -> tuple[str, int]:
    digest = sha256()
    byte_size = 0
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
            byte_size += len(chunk)
    return digest.hexdigest(), byte_size


def _display_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _optional_text(value: object) -> str | None:
    if _is_blank(value):
        return None
    normalized = " ".join(_display_value(value).split())
    return normalized or None


def _text_key(value: str) -> str:
    return "".join(value.split()).casefold()


def _is_blank(value: object) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{label}은 정수여야 합니다.")
    if value < 1:
        raise ValueError(f"{label}은 1 이상이어야 합니다.")
    return value
