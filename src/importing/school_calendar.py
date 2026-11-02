"""School calendar Excel adapter and reviewed revision builder.

The adapter recognizes columns and explicit O/X facts.  Free-form notes remain
review candidates until the operator explicitly applies or ignores each one.
"""

from __future__ import annotations

import calendar as month_calendar
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from types import MappingProxyType
from uuid import UUID, uuid4, uuid5

from openpyxl.cell.cell import Cell
from openpyxl.utils import get_column_letter
from openpyxl.utils.datetime import from_excel

from src.domain.entitlements import LunchDayRule, SchoolLunchMark
from src.domain.revisions import (
    AuditChange,
    ChangeOperation,
    HistoricalSnapshot,
    ImportBatch,
    JsonValue,
    RevisionActor,
    RevisionMetadata,
    RevisionRecord,
    RevisionSource,
)
from src.domain.roster import InclusiveDateRange
from src.domain.school_calendar import (
    SCHOOL_LUNCH_CALENDAR_POLICY_VERSION,
    SCHOOL_LUNCH_SCHEDULE_KIND,
    SchoolLunchCalendar,
    SchoolLunchDay,
    SchoolLunchSchoolSchedule,
    merge_school_lunch_schedule,
    normalize_grade,
    normalize_school_name,
    school_lunch_calendar_from_snapshot,
    school_lunch_schedule_entity_id,
    school_lunch_school_schedule_state,
)

from .workbook import (
    ImportIssue,
    SourceColumn,
    WorkbookPreviewRow,
    _file_identity,
    _open_workbook,
    _source_path,
)


SCHOOL_CALENDAR_IMPORT_POLICY_VERSION = 1
_NOTE_NAMESPACE = UUID("3616b7ab-b68d-4fc3-96ac-43efb8bde014")
_MAX_DATA_ROWS = 10_000
_BLANK_ROW_STOP = 100
_GRADE_PATTERN = re.compile(r"(?<!\d)(\d{1,2})\s*학년")
_FULL_DATE_PATTERN = re.compile(
    r"(?P<year>20\d{2})\s*(?:년|[./-])\s*(?P<month>\d{1,2})"
    r"\s*(?:월|[./-])\s*(?P<day>\d{1,2})\s*일?"
)
_PARTIAL_DATE_PATTERN = re.compile(
    r"(?<!\d)(?P<month>\d{1,2})\s*(?:월|[./-])\s*(?P<day>\d{1,2})\s*일?"
)


@dataclass(frozen=True, slots=True)
class SchoolCalendarColumnMapping:
    sheet_name: str
    header_row: int
    data_start_row: int
    calendar_year: int
    school_column: int
    starts_on_column: int
    starts_on_mark_column: int
    ends_on_column: int
    ends_on_mark_column: int
    note_column: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.sheet_name, str) or not self.sheet_name.strip():
            raise ValueError("학교 일정 시트명이 필요합니다.")
        for value, label in (
            (self.header_row, "헤더 행"),
            (self.data_start_row, "데이터 시작 행"),
            (self.school_column, "학교명 열"),
            (self.starts_on_column, "시작일 열"),
            (self.starts_on_mark_column, "시작일 O/X 열"),
            (self.ends_on_column, "종료일 열"),
            (self.ends_on_mark_column, "종료일 O/X 열"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{label}은 1 이상의 정수여야 합니다.")
        if self.data_start_row <= self.header_row:
            raise ValueError("데이터 시작 행은 헤더 행보다 뒤여야 합니다.")
        if self.calendar_year < 2000 or self.calendar_year > 2200:
            raise ValueError("일정 연도는 2000~2200 사이여야 합니다.")
        columns = (
            self.school_column,
            self.starts_on_column,
            self.starts_on_mark_column,
            self.ends_on_column,
            self.ends_on_mark_column,
        )
        if len(set(columns)) != len(columns):
            raise ValueError("학교 일정 필수 열은 서로 달라야 합니다.")
        if self.note_column is not None:
            if self.note_column < 1:
                raise ValueError("비고 열은 1 이상이어야 합니다.")
            if self.note_column in columns:
                raise ValueError("비고 열은 필수 열과 달라야 합니다.")
        object.__setattr__(self, "sheet_name", self.sheet_name.strip())

    def manifest(self) -> Mapping[str, JsonValue]:
        return MappingProxyType(
            {
                "source_kind": "school-calendar-xlsx",
                "policy_version": SCHOOL_CALENDAR_IMPORT_POLICY_VERSION,
                "sheet_name": self.sheet_name,
                "header_row": self.header_row,
                "data_start_row": self.data_start_row,
                "calendar_year": self.calendar_year,
                "columns": {
                    "school_name": self.school_column,
                    "starts_on": self.starts_on_column,
                    "starts_on_mark": self.starts_on_mark_column,
                    "ends_on": self.ends_on_column,
                    "ends_on_mark": self.ends_on_mark_column,
                    "note": self.note_column,
                },
            }
        )


@dataclass(frozen=True, slots=True)
class SchoolCalendarWorkbookInspection:
    source_filename: str
    sheet_names: tuple[str, ...]
    selected_sheet: str
    header_row: int
    columns: tuple[SourceColumn, ...]
    preview_rows: tuple[WorkbookPreviewRow, ...]
    header_fingerprint: str
    proposed_mapping: SchoolCalendarColumnMapping | None


@dataclass(frozen=True, slots=True)
class SchoolVacationRow:
    row_number: int
    school_name: str
    starts_on: date
    starts_on_mark: SchoolLunchMark
    ends_on: date
    ends_on_mark: SchoolLunchMark
    note: str | None = None

    def __post_init__(self) -> None:
        if self.row_number < 1:
            raise ValueError("원본 행 번호는 1 이상이어야 합니다.")
        school = _required_text(self.school_name, "학교명")
        _validate_date(self.starts_on, "시작일")
        _validate_date(self.ends_on, "종료일")
        if self.starts_on > self.ends_on:
            raise ValueError("학교 일정 시작일은 종료일보다 늦을 수 없습니다.")
        if not isinstance(self.starts_on_mark, SchoolLunchMark):
            raise TypeError("시작일 표시는 SchoolLunchMark여야 합니다.")
        if not isinstance(self.ends_on_mark, SchoolLunchMark):
            raise TypeError("종료일 표시는 SchoolLunchMark여야 합니다.")
        note = None if self.note is None or not str(self.note).strip() else str(self.note).strip()
        object.__setattr__(self, "school_name", school)
        object.__setattr__(self, "note", note)


@dataclass(frozen=True, slots=True)
class SchoolCalendarNoteCandidate:
    candidate_id: UUID
    row_number: int
    school_name: str
    note: str
    suggested_period: InclusiveDateRange | None = None
    suggested_grades: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.candidate_id, UUID):
            raise TypeError("비고 후보 ID는 UUID여야 합니다.")
        if self.row_number < 1:
            raise ValueError("원본 행 번호는 1 이상이어야 합니다.")
        object.__setattr__(self, "school_name", _required_text(self.school_name, "학교명"))
        object.__setattr__(self, "note", _required_text(self.note, "비고"))
        if self.suggested_period is not None:
            if not isinstance(self.suggested_period, InclusiveDateRange):
                raise TypeError("비고 후보 기간은 InclusiveDateRange 또는 None이어야 합니다.")
            if self.suggested_period.starts_on is None or self.suggested_period.ends_on is None:
                raise ValueError("비고 후보 기간에는 시작일과 종료일이 필요합니다.")
        grades = tuple(dict.fromkeys(normalize_grade(item) for item in self.suggested_grades))
        object.__setattr__(self, "suggested_grades", grades)


class SchoolCalendarNoteAction(StrEnum):
    IGNORE = "ignore"
    VENDOR_REQUIRED = "vendor-required"
    SCHOOL_PROVIDES = "school-provides"


@dataclass(frozen=True, slots=True)
class SchoolCalendarNoteDecision:
    candidate_id: UUID
    action: SchoolCalendarNoteAction
    period: InclusiveDateRange | None = None
    grades: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.candidate_id, UUID):
            raise TypeError("비고 후보 ID는 UUID여야 합니다.")
        if not isinstance(self.action, SchoolCalendarNoteAction):
            raise TypeError("비고 결정 action이 올바르지 않습니다.")
        grades = tuple(dict.fromkeys(normalize_grade(item) for item in self.grades))
        if self.action is SchoolCalendarNoteAction.IGNORE:
            if self.period is not None or grades:
                raise ValueError("반영 안 함 결정에는 기간이나 학년을 지정할 수 없습니다.")
        else:
            if not isinstance(self.period, InclusiveDateRange):
                raise ValueError("일정 반영 결정에는 기간이 필요합니다.")
            if self.period.starts_on is None or self.period.ends_on is None:
                raise ValueError("일정 반영 기간에는 시작일과 종료일이 필요합니다.")
            if self.action is SchoolCalendarNoteAction.SCHOOL_PROVIDES and grades:
                raise ValueError("학교 급식(O)은 특정 학년만 지정할 수 없습니다.")
        object.__setattr__(self, "grades", grades)


@dataclass(frozen=True, slots=True)
class SchoolCalendarImportPreview:
    source_filename: str
    sha256: str
    byte_size: int
    sheet_name: str
    header_fingerprint: str
    mapping: SchoolCalendarColumnMapping
    coverage: InclusiveDateRange
    rows: tuple[SchoolVacationRow, ...]
    note_candidates: tuple[SchoolCalendarNoteCandidate, ...]
    issues: tuple[ImportIssue, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.mapping, SchoolCalendarColumnMapping):
            raise TypeError("mapping은 SchoolCalendarColumnMapping이어야 합니다.")
        if not isinstance(self.coverage, InclusiveDateRange):
            raise TypeError("coverage는 InclusiveDateRange여야 합니다.")
        if self.coverage.starts_on is None or self.coverage.ends_on is None:
            raise ValueError("학교 일정 검토 범위에는 시작일과 종료일이 필요합니다.")
        rows = tuple(self.rows)
        candidates = tuple(self.note_candidates)
        issues = tuple(self.issues)
        if any(not isinstance(item, SchoolVacationRow) for item in rows):
            raise TypeError("rows에는 SchoolVacationRow만 허용됩니다.")
        if any(not isinstance(item, SchoolCalendarNoteCandidate) for item in candidates):
            raise TypeError("note_candidates 형식이 올바르지 않습니다.")
        if any(not isinstance(item, ImportIssue) for item in issues):
            raise TypeError("issues 형식이 올바르지 않습니다.")
        if any(
            row.starts_on < self.coverage.starts_on or row.ends_on > self.coverage.ends_on
            for row in rows
        ):
            raise ValueError("학교별 시작일·종료일은 검토 범위 안에 있어야 합니다.")
        object.__setattr__(self, "rows", rows)
        object.__setattr__(self, "note_candidates", candidates)
        object.__setattr__(self, "issues", issues)

    @property
    def can_continue(self) -> bool:
        return bool(self.rows) and not self.issues

    @property
    def school_count(self) -> int:
        return len({normalize_school_name(row.school_name) for row in self.rows})

    def with_review_edits(
        self,
        rows: Sequence[SchoolVacationRow],
        coverage: InclusiveDateRange,
    ) -> SchoolCalendarImportPreview:
        edited_rows = tuple(rows)
        candidates = _note_candidates(
            self.sha256,
            self.sheet_name,
            edited_rows,
            self.mapping.calendar_year,
        )
        return replace(
            self,
            coverage=coverage,
            rows=edited_rows,
            note_candidates=candidates,
            issues=(),
        )


@dataclass(frozen=True, slots=True)
class PreparedSchoolCalendarRevision:
    calendar: SchoolLunchCalendar
    record: RevisionRecord
    changed_school_names: tuple[str, ...]


def inspect_school_calendar_workbook(
    path: str | Path,
    *,
    password: str | None = None,
    sheet_name: str | None = None,
    header_row: int = 2,
    data_start_row: int = 3,
    calendar_year: int | None = None,
    preview_limit: int = 10,
) -> SchoolCalendarWorkbookInspection:
    source_path = _source_path(path)
    year = date.today().year if calendar_year is None else calendar_year
    if header_row < 1 or data_start_row <= header_row:
        raise ValueError("헤더 행과 데이터 시작 행을 확인해주세요.")
    with _open_workbook(source_path, password or "", read_only=False) as workbook:
        worksheet = _select_sheet(workbook, sheet_name)
        columns = _columns(worksheet, header_row)
        preview_rows = _preview_rows(worksheet, data_start_row, len(columns), preview_limit)
        fingerprint = _header_fingerprint(header_row, columns)
        proposal = _propose_mapping(
            worksheet.title,
            header_row,
            data_start_row,
            year,
            columns,
            preview_rows,
        )
        return SchoolCalendarWorkbookInspection(
            source_path.name,
            tuple(workbook.sheetnames),
            worksheet.title,
            header_row,
            columns,
            preview_rows,
            fingerprint,
            proposal,
        )


def parse_school_calendar_workbook(
    path: str | Path,
    mapping: SchoolCalendarColumnMapping,
    *,
    password: str | None = None,
) -> SchoolCalendarImportPreview:
    if not isinstance(mapping, SchoolCalendarColumnMapping):
        raise TypeError("mapping은 SchoolCalendarColumnMapping이어야 합니다.")
    source_path = _source_path(path)
    digest, byte_size = _file_identity(source_path)
    issues: list[ImportIssue] = []
    rows: list[SchoolVacationRow] = []
    with _open_workbook(source_path, password or "", read_only=False) as workbook:
        worksheet = _select_sheet(workbook, mapping.sheet_name)
        columns = _columns(worksheet, mapping.header_row)
        fingerprint = _header_fingerprint(mapping.header_row, columns)
        _validate_mapping_columns(mapping, len(columns))
        blank_run = 0
        final_row = min(worksheet.max_row, mapping.data_start_row + _MAX_DATA_ROWS - 1)
        for row_number in range(mapping.data_start_row, final_row + 1):
            values = _mapped_values(worksheet, row_number, mapping)
            if all(_blank(value) for value in values.values()):
                blank_run += 1
                if blank_run >= _BLANK_ROW_STOP:
                    break
                continue
            blank_run = 0
            school_value = values["school_name"]
            if _blank(school_value):
                school_value = _merged_school_value(
                    worksheet,
                    row_number,
                    mapping.school_column,
                )
            try:
                school_name = _cell_text(school_value, "학교명")
                starts_on = _date_value(
                    values["starts_on"],
                    mapping.calendar_year,
                    workbook.epoch,
                )
                ends_on = _date_value(
                    values["ends_on"],
                    mapping.calendar_year,
                    workbook.epoch,
                )
                if ends_on < starts_on and _date_has_no_year(values["ends_on"]):
                    ends_on = _date_value(
                        values["ends_on"],
                        mapping.calendar_year + 1,
                        workbook.epoch,
                    )
                row = SchoolVacationRow(
                    row_number,
                    school_name,
                    starts_on,
                    _mark_value(values["starts_on_mark"]),
                    ends_on,
                    _mark_value(values["ends_on_mark"]),
                    _optional_cell_text(values.get("note")),
                )
            except (TypeError, ValueError) as error:
                issues.append(ImportIssue(row_number, None, str(error)))
                continue
            rows.append(row)
    if not rows and not issues:
        issues.append(ImportIssue(None, None, "가져올 학교 일정 행이 없습니다."))
    coverage = _derived_coverage(rows, mapping.calendar_year)
    candidates = _note_candidates(digest, mapping.sheet_name, tuple(rows), mapping.calendar_year)
    return SchoolCalendarImportPreview(
        source_path.name,
        digest,
        byte_size,
        mapping.sheet_name,
        fingerprint,
        mapping,
        coverage,
        tuple(rows),
        candidates,
        tuple(issues),
    )


def build_school_lunch_schedules(
    preview: SchoolCalendarImportPreview,
    decisions: Mapping[UUID, SchoolCalendarNoteDecision],
) -> tuple[SchoolLunchSchoolSchedule, ...]:
    if not isinstance(preview, SchoolCalendarImportPreview):
        raise TypeError("preview는 SchoolCalendarImportPreview이어야 합니다.")
    if not preview.can_continue:
        raise ValueError("오류가 남은 학교 일정은 저장할 수 없습니다.")
    expected = {item.candidate_id for item in preview.note_candidates}
    if set(decisions) != expected:
        missing = len(expected - set(decisions))
        extra = len(set(decisions) - expected)
        raise ValueError(f"비고 검토 결정이 맞지 않습니다(미검토 {missing}, 알 수 없음 {extra}).")
    for candidate_id, decision in decisions.items():
        if not isinstance(decision, SchoolCalendarNoteDecision):
            raise TypeError("비고 검토 결정 형식이 올바르지 않습니다.")
        if decision.candidate_id != candidate_id:
            raise ValueError("비고 검토 결정 ID가 key와 다릅니다.")

    grouped: dict[str, list[SchoolVacationRow]] = {}
    display_names: dict[str, str] = {}
    for row in preview.rows:
        key = normalize_school_name(row.school_name)
        grouped.setdefault(key, []).append(row)
        display_names.setdefault(key, row.school_name)

    candidate_by_id = {item.candidate_id: item for item in preview.note_candidates}
    schedules = []
    for school_key, rows in grouped.items():
        day_rules = {
            day: LunchDayRule(SchoolLunchMark.SCHOOL_PROVIDES)
            for day in _weekdays(preview.coverage.starts_on, preview.coverage.ends_on)
        }
        assigned: dict[date, tuple[LunchDayRule, int, str]] = {}
        for row in sorted(rows, key=lambda item: (item.starts_on, item.ends_on, item.row_number)):
            for day in _days(row.starts_on + timedelta(days=1), row.ends_on - timedelta(days=1)):
                if day.weekday() < 5:
                    _assign_explicit_rule(
                        day_rules,
                        assigned,
                        day,
                        LunchDayRule(SchoolLunchMark.VENDOR_REQUIRED),
                        row.row_number,
                        "시작일과 종료일 사이",
                    )
            for day, mark, label in (
                (row.starts_on, row.starts_on_mark, "시작일 O/X"),
                (row.ends_on, row.ends_on_mark, "종료일 O/X"),
            ):
                if day.weekday() < 5:
                    _assign_explicit_rule(
                        day_rules,
                        assigned,
                        day,
                        LunchDayRule(mark),
                        row.row_number,
                        label,
                    )

        relevant_decisions = sorted(
            (
                (candidate_by_id[candidate_id], decision)
                for candidate_id, decision in decisions.items()
                if normalize_school_name(candidate_by_id[candidate_id].school_name) == school_key
            ),
            key=lambda item: item[0].row_number,
        )
        for candidate, decision in relevant_decisions:
            if decision.action is SchoolCalendarNoteAction.IGNORE:
                continue
            assert decision.period is not None
            if (
                decision.period.starts_on < preview.coverage.starts_on
                or decision.period.ends_on > preview.coverage.ends_on
            ):
                raise ValueError(
                    f"{candidate.row_number}행 비고 반영 기간은 전체 검토 범위 안이어야 합니다."
                )
            for day in _weekdays(decision.period.starts_on, decision.period.ends_on):
                current = day_rules[day]
                if decision.action is SchoolCalendarNoteAction.SCHOOL_PROVIDES:
                    day_rules[day] = LunchDayRule(SchoolLunchMark.SCHOOL_PROVIDES)
                elif decision.grades:
                    day_rules[day] = LunchDayRule(
                        current.mark,
                        frozenset((*current.grades_without_school_meal, *decision.grades)),
                    )
                else:
                    day_rules[day] = LunchDayRule(SchoolLunchMark.VENDOR_REQUIRED)

        schedules.append(
            SchoolLunchSchoolSchedule(
                display_names[school_key],
                (preview.coverage,),
                tuple(SchoolLunchDay(day, rule) for day, rule in day_rules.items()),
            )
        )
    return tuple(schedules)


def build_school_calendar_import_revision(
    current_snapshot: HistoricalSnapshot,
    preview: SchoolCalendarImportPreview,
    *,
    decisions: Mapping[UUID, SchoolCalendarNoteDecision],
    actor: RevisionActor,
    recorded_at: datetime,
    revision_id: UUID | None = None,
    batch_id: UUID | None = None,
    reason: str = "학교 중식 일정 Excel 가져오기",
) -> PreparedSchoolCalendarRevision:
    if not isinstance(current_snapshot, HistoricalSnapshot):
        raise TypeError("current_snapshot은 HistoricalSnapshot이어야 합니다.")
    if not isinstance(actor, RevisionActor):
        raise TypeError("actor는 RevisionActor이어야 합니다.")
    if not isinstance(recorded_at, datetime) or recorded_at.tzinfo is None:
        raise ValueError("recorded_at에는 시간대가 있어야 합니다.")
    revision_id = uuid4() if revision_id is None else revision_id
    batch_id = uuid4() if batch_id is None else batch_id
    if not isinstance(revision_id, UUID) or not isinstance(batch_id, UUID):
        raise TypeError("revision ID와 batch ID는 UUID여야 합니다.")

    replacements = build_school_lunch_schedules(preview, decisions)
    existing_calendar = school_lunch_calendar_from_snapshot(current_snapshot)
    existing_by_key = {
        item.normalized_school_name: item for item in existing_calendar.schedules
    }
    merged_by_key = dict(existing_by_key)
    changes = []
    changed_names = []
    for replacement_schedule in replacements:
        key = replacement_schedule.normalized_school_name
        existing = existing_by_key.get(key)
        merged = merge_school_lunch_schedule(existing, replacement_schedule)
        if existing == merged:
            continue
        entity_id = school_lunch_schedule_entity_id(merged.school_name)
        before = None if existing is None else school_lunch_school_schedule_state(existing)
        after = school_lunch_school_schedule_state(merged)
        changes.append(
            AuditChange(
                SCHOOL_LUNCH_SCHEDULE_KIND,
                entity_id,
                ChangeOperation.INSERT if existing is None else ChangeOperation.UPDATE,
                before=before,
                after=after,
            )
        )
        merged_by_key[key] = merged
        changed_names.append(merged.school_name)
    if not changes:
        raise ValueError("이번 학교 일정 가져오기로 달라지는 내용이 없습니다.")

    decision_manifest = {
        "coverage": {
            "starts_on": preview.coverage.starts_on.isoformat(),
            "ends_on": preview.coverage.ends_on.isoformat(),
        },
        "school_rows": [
            {
                "source_row": row.row_number,
                "school_name": row.school_name,
                "starts_on": row.starts_on.isoformat(),
                "starts_on_mark": row.starts_on_mark.value,
                "ends_on": row.ends_on.isoformat(),
                "ends_on_mark": row.ends_on_mark.value,
                "note": row.note,
            }
            for row in preview.rows
        ],
        "note_reviews": [
            _note_decision_manifest(candidate, decisions[candidate.candidate_id])
            for candidate in preview.note_candidates
        ],
    }
    metadata = RevisionMetadata(
        revision_id,
        current_snapshot.revision_id,
        recorded_at,
        preview.coverage.starts_on,
        actor,
        reason,
        RevisionSource.IMPORT,
        decisions={"school_calendar_import": decision_manifest},
        rule_versions={
            "school_calendar_policy_version": SCHOOL_LUNCH_CALENDAR_POLICY_VERSION,
            "school_calendar_import_policy_version": SCHOOL_CALENDAR_IMPORT_POLICY_VERSION,
        },
    )
    mapping_manifest = dict(preview.mapping.manifest())
    mapping_manifest["coverage"] = decision_manifest["coverage"]
    batch = ImportBatch(
        batch_id,
        revision_id,
        preview.source_filename,
        preview.sha256,
        preview.byte_size,
        preview.sheet_name,
        preview.header_fingerprint,
        mapping_manifest,
        preview.coverage.starts_on,
        decision_manifest,
    )
    calendar = SchoolLunchCalendar(tuple(merged_by_key.values()))
    return PreparedSchoolCalendarRevision(
        calendar,
        RevisionRecord(metadata, tuple(changes), batch),
        tuple(changed_names),
    )


def _note_decision_manifest(
    candidate: SchoolCalendarNoteCandidate,
    decision: SchoolCalendarNoteDecision,
) -> Mapping[str, JsonValue]:
    return {
        "candidate_id": str(candidate.candidate_id),
        "source_row": candidate.row_number,
        "school_name": candidate.school_name,
        "source_note": candidate.note,
        "action": decision.action.value,
        "period": None
        if decision.period is None
        else {
            "starts_on": decision.period.starts_on.isoformat(),
            "ends_on": decision.period.ends_on.isoformat(),
        },
        "grades": list(decision.grades),
    }


def _assign_explicit_rule(
    day_rules: dict[date, LunchDayRule],
    assigned: dict[date, tuple[LunchDayRule, int, str]],
    day: date,
    rule: LunchDayRule,
    row_number: int,
    label: str,
) -> None:
    previous = assigned.get(day)
    if previous is not None and previous[0] != rule:
        raise ValueError(
            f"{day:%Y.%m.%d} 일정이 {previous[1]}행 {previous[2]}와 "
            f"{row_number}행 {label}에서 서로 다릅니다."
        )
    assigned[day] = (rule, row_number, label)
    day_rules[day] = rule


def _derived_coverage(
    rows: Sequence[SchoolVacationRow],
    calendar_year: int,
) -> InclusiveDateRange:
    if not rows:
        return InclusiveDateRange(date(calendar_year, 1, 1), date(calendar_year, 12, 31))
    first = min(row.starts_on for row in rows)
    last = max(row.ends_on for row in rows)
    starts_on = date(first.year, first.month, 1)
    ends_on = date(last.year, last.month, month_calendar.monthrange(last.year, last.month)[1])
    return InclusiveDateRange(starts_on, ends_on)


def _note_candidates(
    file_sha256: str,
    sheet_name: str,
    rows: Sequence[SchoolVacationRow],
    calendar_year: int,
) -> tuple[SchoolCalendarNoteCandidate, ...]:
    candidates = []
    for row in rows:
        if row.note is None:
            continue
        candidate_id = uuid5(
            _NOTE_NAMESPACE,
            f"{file_sha256}|{sheet_name}|{row.row_number}|{row.note}",
        )
        grades = tuple(
            dict.fromkeys(normalize_grade(value) for value in _GRADE_PATTERN.findall(row.note))
        )
        dates = _dates_from_note(row.note, calendar_year, row.starts_on, row.ends_on)
        period = None
        if dates:
            period = InclusiveDateRange(min(dates), max(dates))
        candidates.append(
            SchoolCalendarNoteCandidate(
                candidate_id,
                row.row_number,
                row.school_name,
                row.note,
                period,
                grades,
            )
        )
    return tuple(candidates)


def _dates_from_note(
    note: str,
    calendar_year: int,
    row_start: date,
    row_end: date,
) -> tuple[date, ...]:
    found: list[date] = []
    occupied: list[tuple[int, int]] = []
    for match in _FULL_DATE_PATTERN.finditer(note):
        found.append(date(int(match.group("year")), int(match.group("month")), int(match.group("day"))))
        occupied.append(match.span())
    for match in _PARTIAL_DATE_PATTERN.finditer(note):
        if any(start <= match.start() < end for start, end in occupied):
            continue
        month = int(match.group("month"))
        day = int(match.group("day"))
        year = calendar_year
        candidate = date(year, month, day)
        if row_start.year < row_end.year and candidate < row_start:
            candidate = date(year + 1, month, day)
        found.append(candidate)
    return tuple(found)


def _propose_mapping(
    sheet_name: str,
    header_row: int,
    data_start_row: int,
    calendar_year: int,
    columns: tuple[SourceColumn, ...],
    preview_rows: tuple[WorkbookPreviewRow, ...],
) -> SchoolCalendarColumnMapping | None:
    normalized = {column.index: _header_key(column.header) for column in columns}
    school = _unique_header(normalized, ("학교명", "학교"))
    start = _unique_contains(normalized, ("방학식", "시작일", "방학시작", "종업식", "졸업식"), exclude=("급식", "ox"))
    end = _unique_contains(normalized, ("개학일", "종료일", "방학종료"), exclude=("급식", "ox"))
    note = _unique_contains(normalized, ("비고", "특이사항", "참고"), required=False)
    ox_columns = _ox_columns(columns, preview_rows)
    start_mark = _associated_mark(normalized, ox_columns, start, ("방학식", "시작", "종업", "졸업"))
    end_mark = _associated_mark(normalized, ox_columns, end, ("개학", "종료"), excluded={start_mark} if start_mark else set())
    required = (school, start, start_mark, end, end_mark)
    if any(value is None for value in required) or len(set(required)) != 5:
        return None
    return SchoolCalendarColumnMapping(
        sheet_name,
        header_row,
        data_start_row,
        calendar_year,
        school,
        start,
        start_mark,
        end,
        end_mark,
        note,
    )


def _associated_mark(
    headers: Mapping[int, str],
    candidates: tuple[int, ...],
    date_column: int | None,
    role_tokens: tuple[str, ...],
    *,
    excluded: set[int] | None = None,
) -> int | None:
    if date_column is None:
        return None
    excluded = set() if excluded is None else excluded
    role_matches = [
        index
        for index in candidates
        if index not in excluded and any(token in headers.get(index, "") for token in role_tokens)
    ]
    if len(role_matches) == 1:
        return role_matches[0]
    available = [index for index in candidates if index not in excluded]
    if not available:
        return None
    distances = sorted((abs(index - date_column), index) for index in available)
    if len(distances) > 1 and distances[0][0] == distances[1][0]:
        return None
    return distances[0][1]


def _ox_columns(
    columns: tuple[SourceColumn, ...],
    preview_rows: tuple[WorkbookPreviewRow, ...],
) -> tuple[int, ...]:
    candidates = []
    for column in columns:
        header = _header_key(column.header)
        header_hint = "급식" in header or "ox" in header or "유무" in header
        values = [
            row.values[column.index - 1]
            for row in preview_rows
            if column.index <= len(row.values) and row.values[column.index - 1].strip()
        ]
        value_hint = bool(values) and all(_normalized_mark_text(value) in {"O", "X"} for value in values)
        if header_hint or value_hint:
            candidates.append(column.index)
    return tuple(candidates)


def _unique_header(headers: Mapping[int, str], aliases: tuple[str, ...]) -> int | None:
    matches = [index for index, value in headers.items() if value in aliases]
    return matches[0] if len(matches) == 1 else None


def _unique_contains(
    headers: Mapping[int, str],
    tokens: tuple[str, ...],
    *,
    exclude: tuple[str, ...] = (),
    required: bool = True,
) -> int | None:
    matches = [
        index
        for index, value in headers.items()
        if any(token in value for token in tokens)
        and not any(token in value for token in exclude)
    ]
    if len(matches) == 1:
        return matches[0]
    return None


def _mapped_values(worksheet, row_number: int, mapping: SchoolCalendarColumnMapping):
    values = {
        "school_name": worksheet.cell(row_number, mapping.school_column).value,
        "starts_on": worksheet.cell(row_number, mapping.starts_on_column).value,
        "starts_on_mark": worksheet.cell(row_number, mapping.starts_on_mark_column).value,
        "ends_on": worksheet.cell(row_number, mapping.ends_on_column).value,
        "ends_on_mark": worksheet.cell(row_number, mapping.ends_on_mark_column).value,
    }
    if mapping.note_column is not None:
        values["note"] = worksheet.cell(row_number, mapping.note_column).value
    return values


def _merged_school_value(worksheet, row_number: int, column: int):
    for merged in worksheet.merged_cells.ranges:
        if (
            merged.min_col == column
            and merged.max_col == column
            and merged.min_row <= row_number <= merged.max_row
        ):
            return worksheet.cell(merged.min_row, column).value
    return None


def _date_value(value: object, default_year: int, epoch) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, bool) or value is None:
        raise ValueError("시작일·종료일이 비어 있거나 날짜가 아닙니다.")
    if isinstance(value, (int, float)):
        parsed = from_excel(value, epoch)
        return parsed.date() if isinstance(parsed, datetime) else parsed
    text = unicodedata.normalize("NFKC", str(value)).strip()
    full = _FULL_DATE_PATTERN.fullmatch(text)
    if full is not None:
        return date(int(full.group("year")), int(full.group("month")), int(full.group("day")))
    partial = _PARTIAL_DATE_PATTERN.fullmatch(text)
    if partial is not None:
        return date(default_year, int(partial.group("month")), int(partial.group("day")))
    compact = re.fullmatch(r"(20\d{2})(\d{2})(\d{2})", text)
    if compact is not None:
        return date(int(compact.group(1)), int(compact.group(2)), int(compact.group(3)))
    raise ValueError(f"날짜 형식을 읽을 수 없습니다: {text!r}")


def _date_has_no_year(value: object) -> bool:
    if not isinstance(value, str):
        return False
    text = unicodedata.normalize("NFKC", value).strip()
    return _PARTIAL_DATE_PATTERN.fullmatch(text) is not None


def _mark_value(value: object) -> SchoolLunchMark:
    text = _normalized_mark_text(value)
    if text == "O":
        return SchoolLunchMark.SCHOOL_PROVIDES
    if text == "X":
        return SchoolLunchMark.VENDOR_REQUIRED
    raise ValueError(f"O/X 열에는 O 또는 X만 입력할 수 있습니다: {value!r}")


def _normalized_mark_text(value: object) -> str:
    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value)).strip().upper()
    return {"○": "O", "◯": "O", "×": "X", "✕": "X"}.get(text, text)


def _cell_text(value: object, label: str) -> str:
    if isinstance(value, Cell):
        value = value.value
    if value is None or not str(value).strip():
        raise ValueError(f"{label}이 비어 있습니다.")
    if isinstance(value, str) and value.startswith("="):
        raise ValueError(f"{label}에는 수식 대신 값을 입력해주세요.")
    return str(value).strip()


def _optional_cell_text(value: object) -> str | None:
    if value is None or not str(value).strip():
        return None
    return str(value).strip()


def _columns(worksheet, header_row: int) -> tuple[SourceColumn, ...]:
    last = 0
    for index in range(1, min(worksheet.max_column, 512) + 1):
        if not _blank(worksheet.cell(header_row, index).value):
            last = index
    if last == 0:
        raise ValueError(f"{header_row}행에 학교 일정 헤더가 없습니다.")
    return tuple(
        SourceColumn(
            index,
            get_column_letter(index),
            _optional_cell_text(worksheet.cell(header_row, index).value) or "",
        )
        for index in range(1, last + 1)
    )


def _preview_rows(worksheet, start_row: int, width: int, limit: int):
    rows = []
    for row_number in range(start_row, min(worksheet.max_row, start_row + 100) + 1):
        values = tuple(
            "" if worksheet.cell(row_number, column).value is None else str(worksheet.cell(row_number, column).value)
            for column in range(1, width + 1)
        )
        if any(value.strip() for value in values):
            rows.append(WorkbookPreviewRow(row_number, values))
            if len(rows) >= limit:
                break
    return tuple(rows)


def _header_fingerprint(header_row: int, columns: tuple[SourceColumn, ...]) -> str:
    text = "|".join(f"{item.index}:{item.header}" for item in columns)
    return sha256(f"{header_row}|{text}".encode("utf-8")).hexdigest()


def _validate_mapping_columns(mapping: SchoolCalendarColumnMapping, width: int) -> None:
    indexes = (
        mapping.school_column,
        mapping.starts_on_column,
        mapping.starts_on_mark_column,
        mapping.ends_on_column,
        mapping.ends_on_mark_column,
        mapping.note_column,
    )
    if any(index is not None and index > width for index in indexes):
        raise ValueError("선택한 열이 현재 Excel 헤더 범위를 벗어났습니다.")


def _select_sheet(workbook, sheet_name: str | None):
    if not workbook.sheetnames:
        raise ValueError("Excel에 시트가 없습니다.")
    selected = workbook.sheetnames[0] if sheet_name is None else sheet_name
    if selected not in workbook.sheetnames:
        raise ValueError(f"Excel에 없는 시트입니다: {selected}")
    return workbook[selected]


def _header_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"[^0-9a-z가-힣]+", "", normalized)


def _blank(value: object) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label}은 비어 있을 수 없습니다.")
    return value.strip()


def _validate_date(value: object, label: str) -> None:
    if isinstance(value, datetime) or not isinstance(value, date):
        raise TypeError(f"{label}은 date여야 합니다.")


def _days(starts_on: date | None, ends_on: date | None):
    if starts_on is None or ends_on is None or starts_on > ends_on:
        return
    current = starts_on
    while current <= ends_on:
        yield current
        current += timedelta(days=1)


def _weekdays(starts_on: date | None, ends_on: date | None):
    return (day for day in _days(starts_on, ends_on) if day.weekday() < 5)
