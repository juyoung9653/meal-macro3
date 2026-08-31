"""Offscreen school calendar auto-import, editable review, and backup checks."""

from __future__ import annotations

import os
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import UUID

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import openpyxl
import pytest
from PySide6.QtCore import QDate
from PySide6.QtWidgets import QApplication, QDialog

from src.desktop_v2.school_calendar_import import (
    SchoolCalendarImportController,
    SchoolCalendarImportDialog,
    SchoolCalendarReviewDialog,
)
from src.domain.entitlements import SchoolLunchMark
from src.domain.projections import roster_audit_changes
from src.domain.revisions import RevisionActor, RevisionMetadata, RevisionRecord, RevisionSource
from src.domain.roster import Meal, MealEnrollment, Person, PersonProfile, RosterSnapshot
from src.domain.school_calendar import school_lunch_calendar_from_snapshot
from src.importing.school_calendar import (
    SchoolCalendarNoteAction,
    SchoolCalendarNoteDecision,
    inspect_school_calendar_workbook,
    parse_school_calendar_workbook,
)
from src.storage import DatabaseSession


BASE_REVISION_ID = UUID("92000000-0000-0000-0000-000000000001")
PERSON_ID = UUID("92000000-0000-0000-0000-000000000002")
IMPORT_REVISION_ID = UUID("92000000-0000-0000-0000-000000000003")
BATCH_ID = UUID("92000000-0000-0000-0000-000000000004")


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _write_schedule(path: Path) -> None:
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.title = "일정"
    worksheet.append(("학교 급식 일정",))
    worksheet.append(("학교명", "시작일", "시작일 O/X", "종료일", "종료일 O/X", "비고"))
    worksheet.append(
        (
            "분당초",
            date(2026, 7, 24),
            "O",
            date(2026, 8, 19),
            "O",
            "6학년 미급식",
        )
    )
    workbook.save(path)
    workbook.close()


def _preview(path: Path):
    inspection = inspect_school_calendar_workbook(path, calendar_year=2026)
    assert inspection.proposed_mapping is not None
    return parse_school_calendar_workbook(path, inspection.proposed_mapping)


def _base_revision() -> RevisionRecord:
    roster = RosterSnapshot(
        BASE_REVISION_ID,
        (Person(PERSON_ID, PersonProfile("아동", school_name="분당초", grade="6")),),
        enrollments=(MealEnrollment(PERSON_ID, Meal.LUNCH),),
    )
    return RevisionRecord(
        RevisionMetadata(
            BASE_REVISION_ID,
            None,
            datetime(2026, 8, 31, 0, 0, tzinfo=UTC),
            date(2026, 7, 1),
            RevisionActor("tester", "S-1-5-21-test"),
            "학교 일정 화면 기준 명단",
            RevisionSource.UI,
        ),
        roster_audit_changes(None, roster),
    )


def test_import_dialog_auto_reads_expected_columns_and_preview(
    qapp: QApplication,
    tmp_path: Path,
):
    path = tmp_path / "school.xlsx"
    _write_schedule(path)
    dialog = SchoolCalendarImportDialog(
        initial_directory=tmp_path,
        calendar_year=2026,
    )
    try:
        dialog.load_file(path)
        qapp.processEvents()

        assert dialog._continue.isEnabled()
        assert dialog._preview_table.rowCount() == 1
        assert dialog._mapping_combos["school"].currentData() == 1
        assert dialog._mapping_combos["starts_on"].currentData() == 2
        assert dialog._mapping_combos["ends_on"].currentData() == 4
        assert "자동으로 찾았습니다" in dialog._mapping_status.text() or "1곳" in dialog._mapping_status.text()

        dialog._continue.click()
        assert dialog.result() == QDialog.DialogCode.Accepted
        assert dialog.preview().rows[0].school_name == "분당초"
    finally:
        dialog.close()


def test_review_dialog_allows_per_school_date_mark_edits_and_requires_note_choice(
    qapp: QApplication,
    tmp_path: Path,
):
    path = tmp_path / "review.xlsx"
    _write_schedule(path)
    preview = _preview(path)
    dialog = SchoolCalendarReviewDialog(
        preview,
        known_school_names=("분당초",),
    )
    try:
        qapp.processEvents()
        assert not dialog._save.isEnabled()
        assert "모든 비고" in dialog._summary.text()

        row = dialog._row_editors[0]
        row.school.setText("분당새초")
        row.starts_on.setDate(QDate(2026, 7, 27))
        row.starts_mark.setCurrentIndex(
            row.starts_mark.findData(SchoolLunchMark.VENDOR_REQUIRED.value)
        )
        note = next(iter(dialog._note_editors.values()))
        note.action.setCurrentIndex(
            note.action.findData(SchoolCalendarNoteAction.IGNORE.value)
        )
        qapp.processEvents()

        assert dialog._save.isEnabled()
        dialog._save.click()
        assert dialog.result() == QDialog.DialogCode.Accepted
        edited = dialog.reviewed_preview().rows[0]
        assert edited.school_name == "분당새초"
        assert edited.starts_on == date(2026, 7, 27)
        assert edited.starts_on_mark is SchoolLunchMark.VENDOR_REQUIRED
        assert next(iter(dialog.decisions().values())).action is SchoolCalendarNoteAction.IGNORE
    finally:
        dialog.close()


def test_controller_commits_one_revision_and_verified_automatic_backup(
    qapp: QApplication,
    tmp_path: Path,
):
    del qapp
    path = tmp_path / "controller.xlsx"
    _write_schedule(path)
    preview = _preview(path)
    candidate = preview.note_candidates[0]
    decisions = {
        candidate.candidate_id: SchoolCalendarNoteDecision(
            candidate.candidate_id,
            SchoolCalendarNoteAction.IGNORE,
        )
    }

    class AcceptedImport:
        def __init__(self, *, initial_directory, calendar_year, parent=None):
            del parent
            assert Path(initial_directory) == tmp_path
            assert calendar_year == 2026

        @staticmethod
        def exec():
            return QDialog.DialogCode.Accepted

        @staticmethod
        def preview():
            return preview

    class AcceptedReview:
        def __init__(self, value, *, known_school_names, parent=None):
            del parent
            assert value == preview
            assert known_school_names == ("분당초",)

        @staticmethod
        def exec():
            return QDialog.DialogCode.Accepted

        @staticmethod
        def reviewed_preview():
            return preview

        @staticmethod
        def decisions():
            return decisions

    class Messages:
        @staticmethod
        def critical(*args):
            raise AssertionError(args)

        @staticmethod
        def warning(*args):
            raise AssertionError(args)

    backup_directory = tmp_path / "backups"
    backup_directory.mkdir()
    database_path = tmp_path / "operations.sqlite3"
    with DatabaseSession.create(
        database_path,
        "user-password",
        "recovery-password",
        backup_directory=backup_directory,
    ) as session:
        session.append_revision(_base_revision())
        outcome = SchoolCalendarImportController(
            session,
            import_dialog_type=AcceptedImport,
            review_dialog_type=AcceptedReview,
            actor_provider=lambda: RevisionActor("tester", "S-1-5-21-test"),
            today_provider=lambda: date(2026, 8, 31),
            recorded_at_provider=lambda: datetime(2026, 8, 31, 1, 0, tzinfo=UTC),
            revision_id_factory=lambda: IMPORT_REVISION_ID,
            batch_id_factory=lambda: BATCH_ID,
            message_box=Messages,
        ).run()

        assert outcome.completed
        assert outcome.roster is not None
        assert outcome.roster.revision_id == IMPORT_REVISION_ID
        assert outcome.backup_result is not None
        assert outcome.backup_result.record is not None
        backup_path = outcome.backup_result.record.path
        stored = school_lunch_calendar_from_snapshot(
            session.database.snapshot(IMPORT_REVISION_ID)
        )
        assert stored.schedules[0].school_name == "분당초"

    restored_path = tmp_path / "restored.sqlite3"
    with DatabaseSession.restore_and_open_with_user_password(
        backup_path,
        restored_path,
        "user-password",
    ) as restored:
        calendar = school_lunch_calendar_from_snapshot(
            restored.database.snapshot(IMPORT_REVISION_ID)
        )
        assert calendar.vendor_service_available(
            PersonProfile("아동", school_name="분당초", grade="6"),
            date(2026, 8, 3),
        )
        restored.database.check_integrity()
