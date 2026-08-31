"""Excel Adapter that emits typed observations without retaining source bytes."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from stat import S_ISREG
from types import MappingProxyType
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
from openpyxl.utils import column_index_from_string, get_column_letter
from openpyxl.utils.datetime import from_excel
from openpyxl.utils.exceptions import InvalidFileException
from openpyxl.workbook.workbook import Workbook

from src.domain.roster import Meal, normalize_phone

from .contracts import (
    DateNormalization,
    ImportMappingProfile,
    ObservedDeliveryAssignment,
    PersonObservation,
    PhoneNormalization,
    ProfileField,
    SourceReference,
)


_MAX_COLUMNS = 256
_MAX_DATA_ROWS = 50_000
_BLANK_ROW_STOP = 100
_OBSERVATION_NAMESPACE = UUID("bd5ecf05-6f24-47d8-b84d-a5de9b08d094")
_COLUMN_PATTERN = re.compile(r"^[A-Za-z]{1,3}$")
_DATE_SEPARATORS = re.compile(r"[./년월\-]+")
_MEAL_SEPARATORS = re.compile(r"[,/+·\s]+")
_INVALID = object()


class WorkbookImportError(ValueError):
    pass


class WorkbookImportPasswordRequiredError(WorkbookImportError):
    pass


class WorkbookImportPasswordError(WorkbookImportError):
    pass


class WorkbookStructureError(WorkbookImportError):
    pass


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


@dataclass(frozen=True, slots=True)
class WorkbookPreviewRow:
    row_number: int
    values: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WorkbookInspection:
    source_filename: str
    sheet_names: tuple[str, ...]
    selected_sheet: str
    header_row: int
    columns: tuple[SourceColumn, ...]
    preview_rows: tuple[WorkbookPreviewRow, ...]
    header_fingerprint: str


@dataclass(frozen=True, slots=True)
class ImportIssue:
    row_number: int | None
    column: str | None
    message: str

    @property
    def location(self) -> str:
        parts = []
        if self.row_number is not None:
            parts.append(f"{self.row_number}행")
        if self.column is not None:
            parts.append(f"{self.column}열")
        return " ".join(parts) or "파일"


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

    def __post_init__(self) -> None:
        if not isinstance(self.profile, ImportMappingProfile):
            raise TypeError("profile은 ImportMappingProfile이어야 합니다.")
        object.__setattr__(self, "observations", tuple(self.observations))
        object.__setattr__(self, "issues", tuple(self.issues))
        object.__setattr__(self, "sample_rows", tuple(self.sample_rows))

    @property
    def can_continue(self) -> bool:
        return bool(self.observations) and not self.issues


@dataclass(slots=True)
class _RawObservation:
    row_number: int
    profile_values: dict[ProfileField, str | date | None]
    meals: tuple[Meal, ...]
    assignments: tuple[ObservedDeliveryAssignment, ...]
    external_reference: str | None


def inspect_workbook(
    path: str | Path,
    *,
    password: str | None = None,
    sheet_name: str | None = None,
    header_row: int = 1,
    data_start_row: int | None = None,
    preview_limit: int = 8,
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
    with _open_workbook(source_path, password or "") as workbook:
        worksheet = _select_sheet(workbook, sheet_name)
        columns = _read_columns(worksheet, header_row)
        fingerprint = _header_fingerprint(header_row, columns)
        rows = _preview_rows(
            worksheet,
            start_row,
            len(columns),
            preview_limit,
        )
        return WorkbookInspection(
            source_filename=source_path.name,
            sheet_names=tuple(workbook.sheetnames),
            selected_sheet=worksheet.title,
            header_row=header_row,
            columns=columns,
            preview_rows=rows,
            header_fingerprint=fingerprint,
        )


def parse_workbook(
    path: str | Path,
    profile: ImportMappingProfile,
    *,
    sheet_name: str | None = None,
    password: str | None = None,
    sample_limit: int = 8,
) -> WorkbookImportPreview:
    """Parse mapped cells, blocking unknown flags and ambiguous row content."""

    if not isinstance(profile, ImportMappingProfile):
        raise TypeError("profile은 ImportMappingProfile이어야 합니다.")
    source_path = _source_path(path)
    digest, byte_size = _file_identity(source_path)
    issues: list[ImportIssue] = []
    with _open_workbook(source_path, password or "") as workbook:
        worksheet = _select_sheet(workbook, sheet_name)
        columns = _read_columns(worksheet, profile.header_row)
        fingerprint = _header_fingerprint(profile.header_row, columns)
        column_indexes = _resolve_profile_columns(profile, columns)
        sample_rows = _preview_rows(
            worksheet,
            profile.data_start_row,
            len(columns),
            sample_limit,
        )
        raw = _read_observations(
            worksheet,
            workbook,
            source_path.name,
            profile,
            column_indexes,
            issues,
        )
    observations = _merge_observations(
        raw,
        source_path.name,
        worksheet.title,
        digest,
        issues,
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


def _read_columns(worksheet, header_row: int) -> tuple[SourceColumn, ...]:
    cells = next(
        worksheet.iter_rows(
            min_row=header_row,
            max_row=header_row,
            min_col=1,
            max_col=_MAX_COLUMNS,
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
    return tuple(
        SourceColumn(index, get_column_letter(index), headers[index - 1])
        for index in range(1, last + 1)
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
) -> tuple[WorkbookPreviewRow, ...]:
    result: list[WorkbookPreviewRow] = []
    max_row = min(worksheet.max_row or start_row, start_row + max(limit * 5, 20))
    for row in worksheet.iter_rows(
        min_row=start_row,
        max_row=max_row,
        min_col=1,
        max_col=column_count,
    ):
        values = tuple(_display_value(cell.value) for cell in row)
        if not any(values):
            continue
        result.append(WorkbookPreviewRow(row[0].row, values))
        if len(result) == limit:
            break
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
        if index <= len(columns):
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


def _read_observations(
    worksheet,
    workbook: Workbook,
    source_filename: str,
    profile: ImportMappingProfile,
    indexes: Mapping[str, int],
    issues: list[ImportIssue],
) -> list[_RawObservation]:
    mapped_indexes = tuple(sorted(set(indexes.values())))
    max_column = max(mapped_indexes)
    first_row = profile.data_start_row
    declared_last = worksheet.max_row or first_row
    last_row = min(declared_last, first_row + _MAX_DATA_ROWS - 1)
    result: list[_RawObservation] = []
    blank_run = 0
    saw_data = False
    for cells in worksheet.iter_rows(
        min_row=first_row,
        max_row=last_row,
        min_col=1,
        max_col=max_column,
    ):
        row_number = cells[0].row
        if all(_is_blank(cells[index - 1].value) for index in mapped_indexes):
            blank_run += 1
            if saw_data and blank_run >= _BLANK_ROW_STOP:
                break
            continue
        saw_data = True
        blank_run = 0
        before_issue_count = len(issues)
        raw = _parse_row(
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
    cells: Sequence[Cell],
    workbook: Workbook,
    _source_filename: str,
    profile: ImportMappingProfile,
    indexes: Mapping[str, int],
    issues: list[ImportIssue],
) -> _RawObservation | None:
    row_number = cells[0].row
    values: dict[ProfileField, str | date | None] = {}
    if profile.provider_value is not None:
        values[ProfileField.PROVIDER] = profile.provider_value
    for field, token in profile.profile_columns.items():
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
            issues.append(ImportIssue(row_number, source_column, str(error)))

    name = values.get(ProfileField.NAME)
    if not isinstance(name, str) or not name:
        issues.append(ImportIssue(row_number, profile.profile_columns[ProfileField.NAME], "성명이 비어 있습니다."))

    meals = _row_meals(cells, profile, indexes, row_number, issues)
    assignment = _row_assignment(cells, profile, indexes, meals, row_number, issues)
    external_reference = _optional_cell_text(
        cells,
        profile.source_reference_column,
        indexes,
        row_number,
        issues,
    )
    if any(issue.row_number == row_number for issue in issues):
        return None
    return _RawObservation(
        row_number,
        values,
        meals,
        () if assignment is None else (assignment,),
        external_reference,
    )


def _candidate_value(
    cells: Sequence[Cell],
    tokens: tuple[str, ...],
    indexes: Mapping[str, int],
    row_number: int,
    issues: list[ImportIssue],
) -> tuple[object, str]:
    primary = tokens[0]
    for token in tokens:
        cell = cells[indexes[token] - 1]
        value = _safe_cell_value(cell, row_number, token, issues)
        if value is _INVALID:
            return _INVALID, token
        if not _is_blank(value):
            return value, token
    return None, primary


def _safe_cell_value(
    cell: Cell,
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
    digits = "".join(character for character in text if character.isdigit())
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
    raise ValueError(f"생년월일은 YYYYMMDD 또는 날짜 셀이어야 합니다: {text}")


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
    cells: Sequence[Cell],
    profile: ImportMappingProfile,
    indexes: Mapping[str, int],
    row_number: int,
    issues: list[ImportIssue],
) -> tuple[Meal, ...]:
    if profile.combined_meals_column is not None:
        token = profile.combined_meals_column
        value = _safe_cell_value(cells[indexes[token] - 1], row_number, token, issues)
        if value is _INVALID:
            return ()
        return _combined_meals(value, row_number, token, issues)
    selected: set[Meal] = set()
    true_keys = {_text_key(value) for value in profile.meal_true_values}
    false_keys = {_text_key(value) for value in profile.meal_false_values}
    for meal, token in profile.meal_flag_columns.items():
        value = _safe_cell_value(cells[indexes[token] - 1], row_number, token, issues)
        if value is _INVALID:
            continue
        key = _text_key(_display_value(value))
        if key in true_keys:
            selected.add(meal)
        elif key not in false_keys:
            issues.append(
                ImportIssue(
                    row_number,
                    token,
                    f"식종 값 '{_display_value(value)}'은 급식을 받는 값이나 받지 않는 값 설정에 없습니다.",
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
    tokens = tuple(token for token in _MEAL_SEPARATORS.split(text) if token)
    aliases = {
        "조식": Meal.BREAKFAST,
        "아침": Meal.BREAKFAST,
        "중식": Meal.LUNCH,
        "점심": Meal.LUNCH,
        "석식": Meal.DINNER,
        "저녁": Meal.DINNER,
    }
    selected: set[Meal] = set()
    unknown: list[str] = []
    for token in tokens:
        meal = aliases.get(_text_key(token))
        if meal is None:
            unknown.append(token)
        else:
            selected.add(meal)
    if unknown:
        issues.append(
            ImportIssue(
                row_number,
                column,
                "알 수 없는 식종 값입니다: " + ", ".join(unknown),
            )
        )
    return tuple(meal for meal in Meal if meal in selected)


def _row_assignment(
    cells: Sequence[Cell],
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
    cells: Sequence[Cell],
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
    value = _safe_cell_value(cell, row_number, token, issues)
    if value is _INVALID:
        return None
    text = _literal_cell_text(cell) if literal else _optional_text(value)
    return text or None


def _literal_cell_text(cell: Cell) -> str:
    value = cell.value
    if isinstance(value, int) and re.fullmatch(r"0+", cell.number_format or ""):
        return str(value).zfill(len(cell.number_format))
    return _display_value(value).strip()


def _merge_observations(
    rows: list[_RawObservation],
    source_filename: str,
    sheet_name: str,
    file_sha256: str,
    issues: list[ImportIssue],
) -> tuple[PersonObservation, ...]:
    groups: dict[tuple[object, ...], list[_RawObservation]] = {}
    for row in rows:
        name = str(row.profile_values[ProfileField.NAME])
        birth = row.profile_values.get(ProfileField.BIRTH_DATE)
        key = (
            (_text_key(name), birth)
            if isinstance(birth, date)
            else ("row", row.row_number)
        )
        groups.setdefault(key, []).append(row)

    observations: list[PersonObservation] = []
    for grouped_rows in groups.values():
        row_numbers = tuple(item.row_number for item in grouped_rows)
        merged_values: dict[ProfileField, str | date | None] = {}
        conflicting = False
        fields = tuple(
            dict.fromkeys(
                field
                for item in grouped_rows
                for field in item.profile_values
            )
        )
        for field in fields:
            nonblank = tuple(
                dict.fromkeys(
                    item.profile_values.get(field)
                    for item in grouped_rows
                    if item.profile_values.get(field) is not None
                )
            )
            if len(nonblank) > 1:
                conflicting = True
                issues.append(
                    ImportIssue(
                        row_numbers[0],
                        None,
                        f"같은 성명·생년월일 행의 {field.value} 값이 서로 다릅니다: "
                        + ", ".join(str(number) for number in row_numbers),
                    )
                )
            merged_values[field] = nonblank[0] if nonblank else None
        assignments: dict[Meal | None, ObservedDeliveryAssignment] = {}
        for item in grouped_rows:
            for assignment in item.assignments:
                existing = assignments.get(assignment.meal)
                if existing is not None and existing != assignment:
                    conflicting = True
                    issues.append(
                        ImportIssue(
                            item.row_number,
                            None,
                            "같은 사람·식종의 배송담당 또는 순서가 서로 다릅니다.",
                        )
                    )
                assignments[assignment.meal] = assignment
        if conflicting:
            continue
        meal_set = {meal for item in grouped_rows for meal in item.meals}
        external_values = tuple(
            dict.fromkeys(
                item.external_reference
                for item in grouped_rows
                if item.external_reference is not None
            )
        )
        external_reference = " / ".join(external_values) or None
        observation_id = uuid5(
            _OBSERVATION_NAMESPACE,
            f"{file_sha256}/{sheet_name}/"
            + ",".join(str(number) for number in row_numbers),
        )
        observations.append(
            PersonObservation(
                observation_id,
                SourceReference(
                    source_filename,
                    sheet_name,
                    row_numbers,
                    external_reference,
                ),
                merged_values,
                tuple(meal for meal in Meal if meal in meal_set),
                tuple(
                    assignments[key]
                    for key in (None, *Meal)
                    if key in assignments
                ),
            )
        )
    return tuple(observations)


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
    return " ".join(value.split()).casefold()


def _is_blank(value: object) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{label}은 정수여야 합니다.")
    if value < 1:
        raise ValueError(f"{label}은 1 이상이어야 합니다.")
    return value
