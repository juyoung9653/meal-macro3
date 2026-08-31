from __future__ import annotations

import os
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import UUID

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QDate
from PySide6.QtWidgets import QApplication, QDialog

from src.desktop_v2.school_calendar_edit import (
    SchoolCalendarEditController,
    SchoolCalendarEditDialog,
    SchoolCalendarEditStatus,
)
from src.domain import (
    AuditChange,
    ChangeOperation,
    InclusiveDateRange,
    LunchDayRule,
    Meal,
    MealEnrollment,
    Person,
    PersonProfile,
    RevisionActor,
    RevisionMetadata,
    RevisionRecord,
    RevisionSource,
    RosterSnapshot,
    SCHOOL_LUNCH_SCHEDULE_KIND,
    SchoolLunchCalendar,
    SchoolLunchDay,
    SchoolLunchMark,
    SchoolLunchSchoolSchedule,
    apply_school_lunch_rule_period,
    roster_audit_changes,
    school_lunch_calendar_from_snapshot,
    school_lunch_school_schedule_state,
)
from src.storage import DatabaseSession


BASE_REVISION_ID = UUID("93000000-0000-0000-0000-000000000001")
PERSON_ID = UUID("93000000-0000-0000-0000-000000000002")
CALENDAR_REVISION_ID = UUID("93000000-0000-0000-0000-000000000003")
EDIT_REVISION_ID = UUID("93000000-0000-0000-0000-000000000004")


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _schedule() -> SchoolLunchSchoolSchedule:
    coverage = InclusiveDateRange(date(2026, 8, 17), date(2026, 8, 28))
    days = []
    current = coverage.starts_on
    while current <= coverage.ends_on:
        if current.weekday() < 5:
            mark = (
                SchoolLunchMark.VENDOR_REQUIRED
                if date(2026, 8, 19) <= current <= date(2026, 8, 25)
                else SchoolLunchMark.SCHOOL_PROVIDES
            )
            days.append(SchoolLunchDay(current, LunchDayRule(mark)))
        current = date.fromordinal(current.toordinal() + 1)
    return SchoolLunchSchoolSchedule("분당초", (coverage,), tuple(days))


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
            date(2026, 8, 1),
            RevisionActor("tester", "S-1-5-21-test"),
            "학교 일정 편집 기준 명단",
            RevisionSource.UI,
        ),
        roster_audit_changes(None, roster),
    )


def _calendar_revision(schedule: SchoolLunchSchoolSchedule) -> RevisionRecord:
    return RevisionRecord(
        RevisionMetadata(
            CALENDAR_REVISION_ID,
            BASE_REVISION_ID,
            datetime(2026, 8, 31, 1, 0, tzinfo=UTC),
            date(2026, 8, 17),
            RevisionActor("tester", "S-1-5-21-test"),
            "학교 일정 최초 저장",
            RevisionSource.UI,
        ),
        (
            AuditChange(
                SCHOOL_LUNCH_SCHEDULE_KIND,
                schedule.entity_id,
                ChangeOperation.INSERT,
                before=None,
                after=school_lunch_school_schedule_state(schedule),
            ),
        ),
    )


def test_dialog_reopens_stored_segments_and_marks_only_actual_changes(
    qapp: QApplication,
):
    dialog = SchoolCalendarEditDialog(SchoolLunchCalendar((_schedule(),)))
    try:
        qapp.processEvents()
        assert dialog._ledger.rowCount() == 3
        assert not dialog._save.isEnabled()
        assert "분당초" in dialog._school_title.text()

        dialog._ledger.selectRow(1)
        qapp.processEvents()
        dialog._starts_on.setDate(QDate(2026, 8, 20))
        dialog._ends_on.setDate(QDate(2026, 8, 26))
        dialog._apply.click()
        qapp.processEvents()

        assert dialog.changed_school_count() == 1
        assert dialog.changed_weekday_count() == 2
        assert dialog._save.isEnabled()
        assert any(
            dialog._ledger.item(row, 6).text() == "수정됨"
            for row in range(dialog._ledger.rowCount())
        )
        dialog._save.click()
        assert dialog.result() == QDialog.DialogCode.Accepted
        updated = dialog.edited_schedules()[0]
        assert updated.rule_on(date(2026, 8, 19)).mark is SchoolLunchMark.SCHOOL_PROVIDES
        assert updated.rule_on(date(2026, 8, 26)).mark is SchoolLunchMark.VENDOR_REQUIRED
    finally:
        dialog.close()


def test_controller_commits_one_ui_revision_and_backup_restores_it(
    qapp: QApplication,
    tmp_path: Path,
):
    del qapp
    original = _schedule()
    edited = apply_school_lunch_rule_period(
        original,
        InclusiveDateRange(date(2026, 8, 17), date(2026, 8, 18)),
        LunchDayRule(SchoolLunchMark.VENDOR_REQUIRED),
    )

    class AcceptedDialog:
        import_requested = False

        def __init__(self, calendar, parent=None):
            del parent
            assert calendar.schedule_for("분당초") == original

        @staticmethod
        def exec():
            return QDialog.DialogCode.Accepted

        @staticmethod
        def edited_schedules():
            return (edited,)

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
        session.append_revision(_calendar_revision(original))
        outcome = SchoolCalendarEditController(
            session,
            dialog_type=AcceptedDialog,
            actor_provider=lambda: RevisionActor("tester", "S-1-5-21-test"),
            recorded_at_provider=lambda: datetime(2026, 8, 31, 2, 0, tzinfo=UTC),
            revision_id_factory=lambda: EDIT_REVISION_ID,
            message_box=Messages,
        ).run()

        assert outcome.completed
        assert outcome.roster is not None
        assert outcome.roster.revision_id == EDIT_REVISION_ID
        assert outcome.backup_result is not None
        assert outcome.backup_result.record is not None
        backup_path = outcome.backup_result.record.path
        latest = session.database.latest_revision()
        assert latest is not None
        assert latest.metadata.source is RevisionSource.UI
        assert latest.import_batch is None

    restored_path = tmp_path / "restored.sqlite3"
    with DatabaseSession.restore_and_open_with_user_password(
        backup_path,
        restored_path,
        "user-password",
    ) as restored:
        stored = school_lunch_calendar_from_snapshot(
            restored.database.snapshot(EDIT_REVISION_ID)
        )
        assert stored.schedule_for("분당초") == edited
        restored.database.check_integrity()


def test_controller_routes_to_excel_import_when_no_calendar_is_stored(
    qapp: QApplication,
    tmp_path: Path,
):
    del qapp
    with DatabaseSession.create(
        tmp_path / "empty-calendar.sqlite3",
        "user-password",
        "recovery-password",
    ) as session:
        session.append_revision(_base_revision())
        outcome = SchoolCalendarEditController(session).run()

    assert outcome.status is SchoolCalendarEditStatus.IMPORT_REQUESTED
