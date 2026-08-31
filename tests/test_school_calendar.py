"""Reviewed school calendar domain and Excel adapter contracts."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from uuid import UUID

import openpyxl
import pytest

from src.domain.entitlements import LunchDayRule, SchoolLunchMark
from src.domain.revisions import (
    AuditChange,
    ChangeOperation,
    EntityRef,
    HistoricalSnapshot,
    RevisionActor,
    RevisionMetadata,
    RevisionRecord,
    RevisionSource,
)
from src.domain.roster import InclusiveDateRange, PersonProfile
from src.domain.school_calendar import (
    SCHOOL_LUNCH_SCHEDULE_KIND,
    SchoolLunchCalendar,
    SchoolLunchDay,
    SchoolLunchSchoolSchedule,
    normalize_grade,
    school_lunch_calendar_from_snapshot,
    school_lunch_school_schedule_from_state,
    school_lunch_school_schedule_state,
)
from src.importing.school_calendar import (
    SchoolCalendarColumnMapping,
    SchoolCalendarNoteAction,
    SchoolCalendarNoteDecision,
    SchoolVacationRow,
    build_school_calendar_import_revision,
    build_school_lunch_schedules,
    inspect_school_calendar_workbook,
    parse_school_calendar_workbook,
)


BASE_REVISION_ID = UUID("91000000-0000-0000-0000-000000000001")
BASE_ENTITY_ID = UUID("91000000-0000-0000-0000-000000000002")
IMPORT_REVISION_ID = UUID("91000000-0000-0000-0000-000000000003")
BATCH_ID = UUID("91000000-0000-0000-0000-000000000004")


def _write_schedule(path: Path, *, merge_school: bool = True) -> None:
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.title = "학교일정"
    worksheet.append(("2026년 여름방학 학교 급식 일정",))
    worksheet.append(
        ("학교명", "시작일", "시작일 O/X", "종료일", "종료일 O/X", "비고")
    )
    worksheet.append(
        (
            "분당초",
            date(2026, 7, 24),
            "O",
            date(2026, 8, 19),
            "O",
            "8/19 6학년 미급식",
        )
    )
    worksheet.append(
        (
            None if merge_school else "분당초",
            date(2026, 12, 24),
            "X",
            date(2027, 1, 8),
            "O",
            None,
        )
    )
    if merge_school:
        worksheet.merge_cells("A3:A4")
    workbook.save(path)
    workbook.close()


def _base_snapshot() -> HistoricalSnapshot:
    return HistoricalSnapshot(
        BASE_REVISION_ID,
        {
            # Generic entity is enough to establish an existing parent revision.
        },
    )


def test_school_schedule_state_is_complete_and_grade_matching_is_normalized():
    period = InclusiveDateRange(date(2026, 8, 17), date(2026, 8, 19))
    schedule = SchoolLunchSchoolSchedule(
        " 분당 초 ",
        (period,),
        (
            SchoolLunchDay(date(2026, 8, 17), LunchDayRule(SchoolLunchMark.SCHOOL_PROVIDES)),
            SchoolLunchDay(
                date(2026, 8, 18),
                LunchDayRule(SchoolLunchMark.SCHOOL_PROVIDES, frozenset({"6"})),
            ),
            SchoolLunchDay(date(2026, 8, 19), LunchDayRule(SchoolLunchMark.VENDOR_REQUIRED)),
        ),
    )
    restored = school_lunch_school_schedule_from_state(
        school_lunch_school_schedule_state(schedule)
    )
    calendar = SchoolLunchCalendar((restored,))

    assert restored == schedule
    assert normalize_grade(" 6 ") == "6학년"
    assert not calendar.vendor_service_available(
        PersonProfile("아동", school_name="분당초", grade="5학년"),
        date(2026, 8, 18),
    )
    assert calendar.vendor_service_available(
        PersonProfile("아동", school_name="분당초", grade="6"),
        date(2026, 8, 18),
    )
    assert calendar.vendor_service_available(
        PersonProfile("아동", school_name="분당초", grade="5학년"),
        date(2026, 8, 19),
    )


def test_school_schedule_rejects_a_missing_reviewed_weekday():
    with pytest.raises(ValueError, match="누락"):
        SchoolLunchSchoolSchedule(
            "분당초",
            (InclusiveDateRange(date(2026, 8, 17), date(2026, 8, 19)),),
            (
                SchoolLunchDay(
                    date(2026, 8, 17),
                    LunchDayRule(SchoolLunchMark.SCHOOL_PROVIDES),
                ),
                SchoolLunchDay(
                    date(2026, 8, 19),
                    LunchDayRule(SchoolLunchMark.VENDOR_REQUIRED),
                ),
            ),
        )


def test_adapter_auto_detects_columns_and_only_forward_fills_actual_merge(tmp_path: Path):
    path = tmp_path / "school-calendar.xlsx"
    _write_schedule(path)

    inspection = inspect_school_calendar_workbook(
        path,
        header_row=2,
        data_start_row=3,
        calendar_year=2026,
    )
    assert inspection.proposed_mapping == SchoolCalendarColumnMapping(
        "학교일정", 2, 3, 2026, 1, 2, 3, 4, 5, 6
    )

    preview = parse_school_calendar_workbook(path, inspection.proposed_mapping)

    assert preview.can_continue
    assert tuple(row.school_name for row in preview.rows) == ("분당초", "분당초")
    assert preview.rows[1].ends_on == date(2027, 1, 8)
    assert preview.coverage == InclusiveDateRange(
        date(2026, 7, 1),
        date(2027, 1, 31),
    )
    assert len(preview.note_candidates) == 1
    candidate = preview.note_candidates[0]
    assert candidate.suggested_period == InclusiveDateRange(
        date(2026, 8, 19),
        date(2026, 8, 19),
    )
    assert candidate.suggested_grades == ("6학년",)


def test_adapter_does_not_fill_a_plain_blank_school_cell(tmp_path: Path):
    path = tmp_path / "unmerged-calendar.xlsx"
    _write_schedule(path, merge_school=False)
    workbook = openpyxl.load_workbook(path)
    workbook["학교일정"]["A4"] = None
    workbook.save(path)
    workbook.close()

    inspection = inspect_school_calendar_workbook(path, calendar_year=2026)
    assert inspection.proposed_mapping is not None
    preview = parse_school_calendar_workbook(path, inspection.proposed_mapping)

    assert not preview.can_continue
    assert any(issue.row_number == 4 and "학교명" in issue.message for issue in preview.issues)
    assert len(preview.rows) == 1


def test_reviewed_note_becomes_a_grade_override_and_unreviewed_note_blocks(tmp_path: Path):
    path = tmp_path / "review-calendar.xlsx"
    _write_schedule(path)
    inspection = inspect_school_calendar_workbook(path, calendar_year=2026)
    assert inspection.proposed_mapping is not None
    preview = parse_school_calendar_workbook(path, inspection.proposed_mapping)
    candidate = preview.note_candidates[0]

    with pytest.raises(ValueError, match="미검토 1"):
        build_school_lunch_schedules(preview, {})

    decision = SchoolCalendarNoteDecision(
        candidate.candidate_id,
        SchoolCalendarNoteAction.VENDOR_REQUIRED,
        candidate.suggested_period,
        candidate.suggested_grades,
    )
    schedules = build_school_lunch_schedules(
        preview,
        {candidate.candidate_id: decision},
    )
    calendar = SchoolLunchCalendar(schedules)

    assert calendar.vendor_service_available(
        PersonProfile("6학년 아동", school_name="분당초", grade="6"),
        date(2026, 8, 19),
    )
    assert not calendar.vendor_service_available(
        PersonProfile("5학년 아동", school_name="분당초", grade="5"),
        date(2026, 8, 19),
    )
    assert calendar.vendor_service_available(
        PersonProfile("아동", school_name="분당초", grade="5"),
        date(2026, 8, 3),
    )


def test_gui_style_row_edits_are_the_values_committed_to_revision(tmp_path: Path):
    path = tmp_path / "edited-calendar.xlsx"
    _write_schedule(path)
    inspection = inspect_school_calendar_workbook(path, calendar_year=2026)
    assert inspection.proposed_mapping is not None
    preview = parse_school_calendar_workbook(path, inspection.proposed_mapping)
    original = preview.rows[0]
    edited = SchoolVacationRow(
        original.row_number,
        "분당새초",
        date(2026, 7, 27),
        SchoolLunchMark.VENDOR_REQUIRED,
        original.ends_on,
        original.ends_on_mark,
        None,
    )
    edited_preview = preview.with_review_edits(
        (edited,),
        InclusiveDateRange(date(2026, 7, 1), date(2026, 8, 31)),
    )

    prepared = build_school_calendar_import_revision(
        _base_snapshot(),
        edited_preview,
        decisions={},
        actor=RevisionActor("tester", "S-1-5-21-test"),
        recorded_at=datetime(2026, 8, 31, 1, 0, tzinfo=UTC),
        revision_id=IMPORT_REVISION_ID,
        batch_id=BATCH_ID,
    )

    assert prepared.changed_school_names == ("분당새초",)
    assert prepared.record.metadata.parent_revision_id == BASE_REVISION_ID
    assert prepared.record.import_batch is not None
    assert prepared.record.import_batch.mapping_manifest["columns"]["starts_on"] == 2
    assert prepared.record.metadata.decisions["school_calendar_import"]["school_rows"][0] == {
        "source_row": 3,
        "school_name": "분당새초",
        "starts_on": "2026-07-27",
        "starts_on_mark": "X",
        "ends_on": "2026-08-19",
        "ends_on_mark": "O",
        "note": None,
    }
    snapshot = HistoricalSnapshot(
        IMPORT_REVISION_ID,
        {
            # Rehydrate the exact canonical entity created by the revision.
            EntityRef(
                SCHOOL_LUNCH_SCHEDULE_KIND,
                prepared.record.changes[0].entity_id,
            ): prepared.record.changes[0].after,
        },
    )
    restored = school_lunch_calendar_from_snapshot(snapshot)
    assert restored.schedules[0].school_name == "분당새초"
    assert restored.vendor_service_available(
        PersonProfile("아동", school_name="분당새초", grade="5"),
        date(2026, 7, 27),
    )
