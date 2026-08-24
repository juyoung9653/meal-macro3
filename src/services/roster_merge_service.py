"""최신 명단을 연간 ``db`` 표에 안전하게 병합한다."""

from __future__ import annotations

import shutil
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from copy import copy
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from io import BytesIO
from pathlib import Path
from typing import Any
from zipfile import BadZipFile

import msoffcrypto
import openpyxl
from msoffcrypto.exceptions import (
    DecryptionError,
    FileFormatError,
    InvalidKeyError,
    ParseError,
)
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.utils.cell import range_boundaries
from openpyxl.utils.exceptions import InvalidFileException
from openpyxl.worksheet.table import Table, TableColumn

from ..core.delivery_matrix import ALLOWED_MEALS
from ..excel._parsing import parse_annual_date_header
from ..excel.annual_workbook import (
    CONTROL_HEADERS,
    PROFILE_HEADER_ROLES,
    TABLE_NAME,
    WorkbookPasswordError,
    WorkbookPasswordRequiredError,
)
from ..excel.excel_utils import CENTER_ALIGN, copy_style
from ..excel.publication import publish_workbook

MERGE_MEAL_ROLE = "합쳐진 식종"
MERGE_SOURCE_ROLES = (*PROFILE_HEADER_ROLES, "배송담당", "연번", MERGE_MEAL_ROLE)
REQUIRED_MERGE_SOURCE_ROLES = ("성명", "생년월일", MERGE_MEAL_ROLE)
STATUS_HEADER = "상태"


class RosterMergeError(ValueError):
    """명단 병합을 안전하게 완료할 수 없을 때 발생한다."""


@dataclass(frozen=True)
class SourceLayout:
    sheet_name: str
    header_row: int
    headers: tuple[str, ...]


@dataclass(frozen=True)
class RosterMergeRequest:
    target_path: Path
    source_path: Path
    target_header_mapping: Mapping[str, str]
    source_layout: SourceLayout
    source_columns: Mapping[str, int]
    source_password: str = field(default="", repr=False, compare=False)


@dataclass(frozen=True)
class MergeLogEntry:
    action: str
    name: str
    birth_prefix: str
    target_row: int | None
    source_rows: tuple[int, ...]
    field: str
    old_value: Any
    new_value: Any


@dataclass(frozen=True)
class RosterMergeResult:
    backup_path: Path
    log_path: Path
    updated_people: int
    existing_people: int
    new_people: int
    untouched_people: int
    changed_cells: int
    added_rows: int


@dataclass
class _SourcePerson:
    key: tuple[str, str]
    name: str
    birth_prefix: str
    source_rows: list[int]
    values_by_role: dict[str, list[Any]]
    meals: list[str]

    def latest_value(self, role: str) -> Any:
        values = self.values_by_role.get(role, [])
        for value in reversed(values):
            if not _is_blank(value):
                return value
        return None


def read_source_layout(
    path: str | Path,
    *,
    password: str = "",
) -> SourceLayout:
    """첫 50행에서 명단 헤더에 가장 가까운 행과 열 표시값을 읽는다."""
    source_path = Path(path)
    best_layout: SourceLayout | None = None
    best_score: tuple[int, int, int, int] | None = None
    with _open_source_workbook(
        source_path,
        password=password,
        data_only=False,
    ) as workbook:
        for sheet_index, worksheet in enumerate(workbook.worksheets):
            for row_number, row in enumerate(
                worksheet.iter_rows(min_row=1, max_row=min(50, worksheet.max_row)),
                start=1,
            ):
                values = tuple(cell.value for cell in row)
                nonblank = [value for value in values if not _is_blank(value)]
                if len(nonblank) < 2:
                    continue
                headers = tuple(_display_header(value) for value in values)
                score = (
                    _source_header_keyword_count(headers),
                    len(nonblank),
                    -sheet_index,
                    -row_number,
                )
                if best_score is None or score > best_score:
                    best_score = score
                    best_layout = SourceLayout(worksheet.title, row_number, headers)
    if best_layout is not None:
        return best_layout
    raise RosterMergeError("합칠 명단 파일에서 헤더 행을 찾지 못했습니다.")


@contextmanager
def _open_source_workbook(
    path: Path,
    *,
    password: str,
    data_only: bool,
):
    """일반·암호화 명단을 동일한 read-only 인터페이스로 연다."""
    try:
        workbook = openpyxl.load_workbook(
            path,
            read_only=True,
            data_only=data_only,
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
                    is_encrypted = office_file.is_encrypted()
                except (FileFormatError, ParseError) as format_error:
                    raise RosterMergeError(
                        f"합칠 명단 파일을 해석할 수 없습니다: {path}"
                    ) from format_error
                if not is_encrypted:
                    raise RosterMergeError(
                        f"합칠 명단 파일이 손상되었거나 지원하지 않는 형식입니다: {path}"
                    )
                if not password:
                    raise WorkbookPasswordRequiredError(
                        "암호로 보호된 합칠 명단 파일입니다. 비밀번호를 입력해 주세요."
                    )
                try:
                    office_file.load_key(password=password)
                    office_file.decrypt(decrypted)
                except (DecryptionError, InvalidKeyError, ValueError) as error:
                    raise WorkbookPasswordError(
                        "합칠 명단 파일의 비밀번호가 올바르지 않습니다."
                    ) from error
            decrypted.seek(0)
            workbook = openpyxl.load_workbook(
                decrypted,
                read_only=True,
                data_only=data_only,
            )
        except (WorkbookPasswordRequiredError, WorkbookPasswordError):
            raise
        except RosterMergeError:
            raise
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
        ) as error:
            raise RosterMergeError(
                f"암호 해제한 합칠 명단 파일을 읽을 수 없습니다: {path}"
            ) from error

        try:
            yield workbook
        finally:
            workbook.close()
    finally:
        decrypted.close()


def merge_roster(
    request: RosterMergeRequest,
    *,
    now: datetime | None = None,
) -> RosterMergeResult:
    """사전 검증 후 백업을 만들고 최신 명단을 원본 DB에 병합한다."""
    target_path = Path(request.target_path).resolve()
    source_path = Path(request.source_path).resolve()
    if target_path == source_path:
        raise RosterMergeError("db 파일과 합칠 명단 파일은 서로 달라야 합니다.")
    if not target_path.is_file():
        raise RosterMergeError(f"db 파일이 없습니다: {target_path}")
    if not source_path.is_file():
        raise RosterMergeError(f"합칠 명단 파일이 없습니다: {source_path}")

    source_columns = _validate_source_columns(
        request.source_layout,
        request.source_columns,
    )
    source_people = _read_source_people(
        source_path,
        request.source_layout,
        source_columns,
        password=request.source_password,
    )
    if not source_people:
        raise RosterMergeError("합칠 명단 파일에 유효한 대상자가 없습니다.")

    keep_vba = target_path.suffix.casefold() == ".xlsm"
    try:
        workbook = openpyxl.load_workbook(
            target_path,
            data_only=False,
            keep_vba=keep_vba,
        )
    except Exception as error:
        raise RosterMergeError(
            "db 파일을 편집 가능한 형식으로 열 수 없습니다. "
            f"암호화 또는 파일 손상을 확인해 주세요: {error}"
        ) from error

    publication_started = False
    try:
        worksheet, table = _find_db_table(workbook)
        min_column, header_row, max_column, max_row = range_boundaries(table.ref)
        if _table_has_totals(table):
            raise RosterMergeError(
                "합계 행이 있는 db 표는 명단 합치기를 지원하지 않습니다."
            )

        target_columns = _resolve_target_columns(
            worksheet,
            header_row,
            min_column,
            max_column,
            request.target_header_mapping,
        )
        status_column, max_column = _ensure_status_column(
            worksheet,
            table,
            header_row,
            min_column,
            max_column,
            max_row,
        )

        existing_rows_by_key = _target_rows_by_key(
            worksheet,
            header_row + 1,
            max_row,
            target_columns,
        )
        initial_keys = set(existing_rows_by_key)
        for row_number in range(header_row + 1, max_row + 1):
            worksheet.cell(row=row_number, column=status_column).value = None

        log_entries: list[MergeLogEntry] = []
        updated_people = 0
        existing_people = 0
        new_people = 0
        changed_cells = 0
        added_rows = 0
        current_max_row = max_row

        for key, source_person in source_people.items():
            target_rows = list(existing_rows_by_key.get(key, ()))
            if not target_rows:
                target_rows, current_max_row, new_logs = _append_new_person_rows(
                    worksheet,
                    source_person,
                    target_columns,
                    status_column,
                    min_column,
                    max_column,
                    current_max_row,
                )
                existing_rows_by_key[key] = target_rows
                log_entries.extend(new_logs)
                new_people += 1
                added_rows += len(target_rows)
                continue

            person_changed = False
            for role, column in target_columns.items():
                if role not in source_columns or role in CONTROL_HEADERS:
                    continue
                latest_value = source_person.latest_value(role)
                for row_number in target_rows:
                    cell = worksheet.cell(row=row_number, column=column)
                    if _values_equal(role, cell.value, latest_value):
                        continue
                    log_entries.append(
                        MergeLogEntry(
                            action="갱신",
                            name=source_person.name,
                            birth_prefix=source_person.birth_prefix,
                            target_row=row_number,
                            source_rows=tuple(source_person.source_rows),
                            field=role,
                            old_value=cell.value,
                            new_value=latest_value,
                        )
                    )
                    cell.value = latest_value
                    changed_cells += 1
                    person_changed = True

            for role in ("배송담당", "연번"):
                if role not in source_columns or role not in target_columns:
                    continue
                latest_value = source_person.latest_value(role)
                for row_number in target_rows:
                    cell = worksheet.cell(row=row_number, column=target_columns[role])
                    if _values_equal(role, cell.value, latest_value):
                        continue
                    log_entries.append(
                        MergeLogEntry(
                            action="갱신",
                            name=source_person.name,
                            birth_prefix=source_person.birth_prefix,
                            target_row=row_number,
                            source_rows=tuple(source_person.source_rows),
                            field=role,
                            old_value=cell.value,
                            new_value=latest_value,
                        )
                    )
                    cell.value = latest_value
                    changed_cells += 1
                    person_changed = True

            existing_meals = {
                _canonical_meal(
                    worksheet.cell(
                        row=row_number,
                        column=target_columns["식종"],
                    ).value
                )
                for row_number in target_rows
            }
            missing_meals = [
                meal for meal in source_person.meals if meal not in existing_meals
            ]
            if missing_meals:
                added, current_max_row, meal_logs = _append_meal_rows(
                    worksheet,
                    source_person,
                    missing_meals,
                    target_columns,
                    status_column,
                    min_column,
                    max_column,
                    current_max_row,
                    action="갱신",
                )
                target_rows.extend(added)
                existing_rows_by_key[key] = target_rows
                log_entries.extend(meal_logs)
                added_rows += len(added)
                person_changed = True

            status = "갱신" if person_changed else "기존"
            for row_number in target_rows:
                worksheet.cell(row=row_number, column=status_column).value = status
            if person_changed:
                updated_people += 1
            else:
                existing_people += 1

        table.ref = _table_ref(
            min_column,
            header_row,
            max_column,
            current_max_row,
        )
        if worksheet.auto_filter.ref:
            worksheet.auto_filter.ref = table.ref

        timestamp = now or datetime.now().astimezone()
        backup_path = _unique_output_path(
            target_path, "백업", timestamp, target_path.suffix
        )
        log_path = _unique_output_path(
            target_path, "명단합치기_로그", timestamp, ".xlsx"
        )
        shutil.copy2(target_path, backup_path)

        untouched_people = len(initial_keys - set(source_people))
        _write_merge_log(
            log_path,
            target_path=target_path,
            source_path=source_path,
            backup_path=backup_path,
            timestamp=timestamp,
            updated_people=updated_people,
            existing_people=existing_people,
            new_people=new_people,
            untouched_people=untouched_people,
            changed_cells=changed_cells,
            added_rows=added_rows,
            entries=log_entries,
        )

        publication_started = True
        publish_workbook(workbook, target_path)
        return RosterMergeResult(
            backup_path=backup_path,
            log_path=log_path,
            updated_people=updated_people,
            existing_people=existing_people,
            new_people=new_people,
            untouched_people=untouched_people,
            changed_cells=changed_cells,
            added_rows=added_rows,
        )
    finally:
        if not publication_started:
            workbook.close()


def _validate_source_columns(
    layout: SourceLayout,
    columns: Mapping[str, int],
) -> dict[str, int]:
    unknown = sorted(set(columns) - set(MERGE_SOURCE_ROLES))
    if unknown:
        raise RosterMergeError("지원하지 않는 합칠 명단 역할: " + ", ".join(unknown))
    normalized: dict[str, int] = {}
    for role, raw_column in columns.items():
        if isinstance(raw_column, bool) or not isinstance(raw_column, int):
            raise RosterMergeError(f"{role} 열 번호가 올바르지 않습니다.")
        if raw_column < 1 or raw_column > len(layout.headers):
            raise RosterMergeError(f"{role} 열이 합칠 명단 범위를 벗어났습니다.")
        normalized[role] = raw_column
    missing = [role for role in REQUIRED_MERGE_SOURCE_ROLES if role not in normalized]
    if missing:
        raise RosterMergeError("선택하지 않은 필수 열: " + ", ".join(missing))
    if len(set(normalized.values())) != len(normalized):
        raise RosterMergeError("각 합칠 명단 역할은 서로 다른 Excel 열이어야 합니다.")
    return normalized


def _read_source_people(
    path: Path,
    layout: SourceLayout,
    columns: Mapping[str, int],
    *,
    password: str,
) -> dict[tuple[str, str], _SourcePerson]:
    with _open_source_workbook(
        path,
        password=password,
        data_only=True,
    ) as workbook:
        if layout.sheet_name not in workbook.sheetnames:
            raise RosterMergeError(
                f"합칠 명단 시트를 찾을 수 없습니다: {layout.sheet_name}"
            )
        worksheet = workbook[layout.sheet_name]
        people: dict[tuple[str, str], _SourcePerson] = {}
        invalid_rows: list[int] = []
        invalid_meal_rows: list[int] = []
        for row_number in range(layout.header_row + 1, worksheet.max_row + 1):
            values = {
                role: worksheet.cell(row=row_number, column=column).value
                for role, column in columns.items()
            }
            if all(_is_blank(value) for value in values.values()):
                continue
            name = values.get("성명")
            birth = values.get("생년월일")
            key = _merge_identity(name, birth)
            if key is None:
                invalid_rows.append(row_number)
                continue
            meals = _parse_combined_meals(values.get(MERGE_MEAL_ROLE))
            if not meals:
                invalid_meal_rows.append(row_number)
                continue
            person = people.get(key)
            if person is None:
                person = _SourcePerson(
                    key=key,
                    name="" if name is None else str(name),
                    birth_prefix=key[1],
                    source_rows=[],
                    values_by_role={role: [] for role in columns},
                    meals=[],
                )
                people[key] = person
            person.source_rows.append(row_number)
            for role, value in values.items():
                person.values_by_role.setdefault(role, []).append(value)
            for meal in meals:
                if meal not in person.meals:
                    person.meals.append(meal)
        if invalid_rows:
            rows = ", ".join(str(row) for row in invalid_rows[:10])
            raise RosterMergeError(
                f"성명 또는 생년월일 앞 6자리가 없는 명단 행이 있습니다: {rows}행"
            )
        if invalid_meal_rows:
            rows = ", ".join(str(row) for row in invalid_meal_rows[:10])
            raise RosterMergeError(
                "합쳐진 식종에서 조식·중식·석식을 찾지 못한 명단 행이 있습니다: "
                f"{rows}행"
            )
        return people


def _find_db_table(workbook) -> tuple[Any, Table]:
    matches: list[tuple[Any, Table]] = []
    for worksheet in workbook.worksheets:
        for name in worksheet.tables:
            table = worksheet.tables[name]
            if table.name.casefold() == TABLE_NAME.casefold():
                matches.append((worksheet, table))
    if not matches:
        raise RosterMergeError("db 파일에서 이름이 'db'인 Excel 표를 찾지 못했습니다.")
    if len(matches) > 1:
        raise RosterMergeError("db 파일에 이름이 'db'인 Excel 표가 여러 개입니다.")
    return matches[0]


def _resolve_target_columns(
    worksheet,
    header_row: int,
    min_column: int,
    max_column: int,
    mapping: Mapping[str, str],
) -> dict[str, int]:
    columns_by_header = {
        _normalize_header(worksheet.cell(row=header_row, column=column).value): column
        for column in range(min_column, max_column + 1)
    }
    resolved: dict[str, int] = {}
    for role in (*PROFILE_HEADER_ROLES, *CONTROL_HEADERS):
        physical_header = mapping.get(role, role)
        column = columns_by_header.get(_normalize_header(physical_header))
        if column is None:
            if role == "연번" and not physical_header:
                continue
            raise RosterMergeError(f"db 파일에서 매핑된 {role} 열을 찾지 못했습니다.")
        resolved[role] = column
    return resolved


def _ensure_status_column(
    worksheet,
    table: Table,
    header_row: int,
    min_column: int,
    max_column: int,
    max_row: int,
) -> tuple[int, int]:
    for column in range(min_column, max_column + 1):
        if _normalize_header(worksheet.cell(header_row, column).value) == STATUS_HEADER:
            return column, max_column

    status_column = max_column + 1
    header_cell = worksheet.cell(
        row=header_row, column=status_column, value=STATUS_HEADER
    )
    copy_style(worksheet.cell(row=header_row, column=max_column), header_cell)
    for row_number in range(header_row + 1, max_row + 1):
        copy_style(
            worksheet.cell(row=row_number, column=max_column),
            worksheet.cell(row=row_number, column=status_column),
        )
    existing_ids = [column.id for column in table.tableColumns]
    next_id = max(existing_ids, default=0) + 1
    table.tableColumns.append(TableColumn(id=next_id, name=STATUS_HEADER))
    table.ref = _table_ref(min_column, header_row, status_column, max_row)
    return status_column, status_column


def _target_rows_by_key(
    worksheet,
    first_row: int,
    last_row: int,
    target_columns: Mapping[str, int],
) -> dict[tuple[str, str], list[int]]:
    rows: dict[tuple[str, str], list[int]] = {}
    for row_number in range(first_row, last_row + 1):
        key = _merge_identity(
            worksheet.cell(row_number, target_columns["성명"]).value,
            worksheet.cell(row_number, target_columns["생년월일"]).value,
        )
        if key is not None:
            rows.setdefault(key, []).append(row_number)
    return rows


def _append_new_person_rows(
    worksheet,
    person: _SourcePerson,
    target_columns: Mapping[str, int],
    status_column: int,
    min_column: int,
    max_column: int,
    current_max_row: int,
) -> tuple[list[int], int, list[MergeLogEntry]]:
    return _append_meal_rows(
        worksheet,
        person,
        person.meals,
        target_columns,
        status_column,
        min_column,
        max_column,
        current_max_row,
        action="신규",
    )


def _append_meal_rows(
    worksheet,
    person: _SourcePerson,
    meals: Sequence[str],
    target_columns: Mapping[str, int],
    status_column: int,
    min_column: int,
    max_column: int,
    current_max_row: int,
    *,
    action: str,
) -> tuple[list[int], int, list[MergeLogEntry]]:
    added_rows: list[int] = []
    entries: list[MergeLogEntry] = []
    template_row = current_max_row
    for meal in meals:
        row_number = current_max_row + 1
        if any(
            not _is_blank(worksheet.cell(row=row_number, column=column).value)
            for column in range(min_column, max_column + 1)
        ):
            worksheet.insert_rows(row_number)
        for column in range(min_column, max_column + 1):
            source_cell = worksheet.cell(row=template_row, column=column)
            target_cell = worksheet.cell(row=row_number, column=column)
            copy_style(source_cell, target_cell)
        if template_row in worksheet.row_dimensions:
            worksheet.row_dimensions[row_number].height = copy(
                worksheet.row_dimensions[template_row].height
            )
        for role, column in target_columns.items():
            if role == "식종":
                value = meal
            elif role in person.values_by_role:
                value = person.latest_value(role)
            else:
                value = None
            worksheet.cell(row=row_number, column=column).value = value
        worksheet.cell(row=row_number, column=status_column).value = action
        added_rows.append(row_number)
        entries.append(
            MergeLogEntry(
                action=action,
                name=person.name,
                birth_prefix=person.birth_prefix,
                target_row=row_number,
                source_rows=tuple(person.source_rows),
                field="행 추가",
                old_value=None,
                new_value=f"식종={meal}",
            )
        )
        current_max_row = row_number
    return added_rows, current_max_row, entries


def _write_merge_log(
    path: Path,
    *,
    target_path: Path,
    source_path: Path,
    backup_path: Path,
    timestamp: datetime,
    updated_people: int,
    existing_people: int,
    new_people: int,
    untouched_people: int,
    changed_cells: int,
    added_rows: int,
    entries: Sequence[MergeLogEntry],
) -> None:
    workbook = openpyxl.Workbook()
    summary = workbook.active
    if summary is None:  # pragma: no cover - openpyxl 기본 계약 방어
        raise RuntimeError("명단 합치기 로그 시트를 만들 수 없습니다.")
    summary.title = "요약"
    summary_rows = (
        ("병합 일시", timestamp.isoformat(timespec="seconds")),
        ("db 파일", str(target_path)),
        ("합칠 명단 파일", str(source_path)),
        ("백업 파일", str(backup_path)),
        ("갱신 대상", updated_people),
        ("기존 대상", existing_people),
        ("신규 대상", new_people),
        ("합칠 명단에 없는 기존 대상", untouched_people),
        ("변경 셀", changed_cells),
        ("추가 행", added_rows),
    )
    for label, value in summary_rows:
        summary.append((label, value))
    for cell in summary["A"]:
        cell.font = Font(bold=True)
    summary.column_dimensions["A"].width = 28
    summary.column_dimensions["B"].width = 90

    history = workbook.create_sheet("변경이력")
    headers = (
        "구분",
        "성명",
        "생년월일 앞6자리",
        "db 행",
        "합칠 명단 행",
        "필드",
        "이전값",
        "최신값",
    )
    history.append(headers)
    header_fill = PatternFill(fill_type="solid", fgColor="1F4E78")
    for cell in history[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = header_fill
        cell.alignment = CENTER_ALIGN
    for entry in entries:
        history.append(
            (
                entry.action,
                entry.name,
                entry.birth_prefix,
                entry.target_row,
                ", ".join(str(row) for row in entry.source_rows),
                entry.field,
                _display_value(entry.old_value),
                _display_value(entry.new_value),
            )
        )
    for row in history.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)
    history.freeze_panes = "A2"
    history.auto_filter.ref = f"A1:H{history.max_row}"
    for column, width in {
        "A": 10,
        "B": 14,
        "C": 18,
        "D": 10,
        "E": 16,
        "F": 18,
        "G": 30,
        "H": 30,
    }.items():
        history.column_dimensions[column].width = width
    publish_workbook(workbook, path)


def _unique_output_path(
    target_path: Path,
    label: str,
    timestamp: datetime,
    suffix: str,
) -> Path:
    stamp = timestamp.strftime("%Y%m%d_%H%M%S")
    base = target_path.with_name(f"{target_path.stem}_{label}_{stamp}{suffix}")
    if not base.exists():
        return base
    for index in range(2, 1000):
        candidate = target_path.with_name(
            f"{target_path.stem}_{label}_{stamp}_{index}{suffix}"
        )
        if not candidate.exists():
            return candidate
    raise RosterMergeError("백업·로그 파일 이름을 만들 수 없습니다.")


def _table_has_totals(table: Table) -> bool:
    return bool(table.totalsRowCount or table.totalsRowShown)


def _table_ref(
    min_column: int,
    min_row: int,
    max_column: int,
    max_row: int,
) -> str:
    return (
        f"{get_column_letter(min_column)}{min_row}:"
        f"{get_column_letter(max_column)}{max_row}"
    )


def _merge_identity(name: Any, birth: Any) -> tuple[str, str] | None:
    name_key = _text_key(name)
    birth_prefix = _birth_digits(birth)[:6]
    if not name_key or len(birth_prefix) != 6:
        return None
    return name_key, birth_prefix


def _parse_combined_meals(value: Any) -> tuple[str, ...]:
    text = "" if value is None else str(value)
    return tuple(meal for meal in ALLOWED_MEALS if meal in text)


def _canonical_meal(value: Any) -> str | None:
    meals = _parse_combined_meals(value)
    return meals[0] if len(meals) == 1 else None


def _values_equal(role: str, first: Any, second: Any) -> bool:
    if _is_blank(first) and _is_blank(second):
        return True
    if role == "생년월일":
        return _birth_digits(first) == _birth_digits(second)
    if role in {"핸드폰", "전화번호"}:
        return _digits(first) == _digits(second)
    numeric_first = _numeric_key(first)
    numeric_second = _numeric_key(second)
    if numeric_first is not None and numeric_second is not None:
        return numeric_first == numeric_second
    return _text_key(first) == _text_key(second)


def _numeric_key(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return number.normalize() if number.is_finite() else None


def _birth_digits(value: Any) -> str:
    if isinstance(value, date):
        return value.strftime("%Y%m%d")
    return _digits(value)


def _digits(value: Any) -> str:
    if value is None:
        return ""
    return "".join(character for character in str(value) if character.isdigit())


def _text_key(value: Any) -> str:
    if _is_blank(value):
        return ""
    return " ".join(str(value).split()).casefold()


def _normalize_header(value: Any) -> str:
    return " ".join(_display_header(value).split())


def _source_header_keyword_count(headers: Sequence[str]) -> int:
    keywords = (
        "성명",
        "이름",
        "생년",
        "학교",
        "세대주",
        "전화",
        "핸드폰",
        "주소",
        "제공기관",
        "관할동",
        "연번",
        "급식",
        "식종",
        "식사",
        "조사결과",
        "전입",
        "변경",
    )
    return sum(
        1
        for header in headers
        if any(keyword in _normalize_header(header) for keyword in keywords)
    )


def _display_header(value: Any) -> str:
    if value is None:
        return ""
    if parse_annual_date_header(value) is not None:
        return str(value)
    return str(value).strip()


def _display_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat(sep=" ", timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())
