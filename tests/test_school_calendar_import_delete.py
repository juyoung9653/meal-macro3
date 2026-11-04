from dataclasses import replace
from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from PySide6.QtWidgets import QApplication, QMessageBox

from src.desktop_v2.school_calendar_edit import SchoolCalendarEditDialog
from src.domain.entitlements import SchoolLunchMark as Mark
from src.domain.projections import roster_audit_changes
from src.domain.revisions import RevisionActor, RevisionMetadata, RevisionRecord, RevisionSource
from src.domain.roster import Person, PersonProfile, RosterSnapshot, MealEnrollment, Meal, InclusiveDateRange
from src.domain.school_calendar import school_lunch_calendar_from_snapshot
from src.importing.school_calendar import SchoolCalendarColumnMapping, SchoolCalendarImportPreview, SchoolVacationRow, build_school_calendar_import_revision
from src.services.school_calendar_delete import build_import_deletion
from src.services.school_calendar_history import school_calendar_import_history
from src.services.revision_restore import restore_revision
from src.storage.database_session import DatabaseSession

ACTOR = RevisionActor("test", "test")
STAMP = datetime(2027, 7, 1, tzinfo=UTC)


def preview(rows):
    return SchoolCalendarImportPreview("synthetic.xlsx", "a" * 64, 100, "일정", "b" * 64,
        SchoolCalendarColumnMapping("일정", 2, 3, 2027, 1, 2, 3, 4, 5),
        InclusiveDateRange(date(2027, 7, 1), date(2027, 7, 31)), tuple(rows), (), ())


def row(number, school, first, last, mark=Mark.VENDOR_REQUIRED):
    return SchoolVacationRow(number, school, date(2027, 7, first), mark, date(2027, 7, last), mark)


def add(session, rows):
    prepared = build_school_calendar_import_revision(session.database.snapshot(session.database.latest_revision_id()),
        preview(rows), decisions={}, actor=ACTOR, recorded_at=STAMP)
    session.append_revision(prepared.record)
    return prepared.record.metadata.revision_id


def remove(session, target):
    head = session.database.latest_revision_id()
    prepared = build_import_deletion(session.database.snapshot(head), session.database.branch_revision_chain(head),
        target, actor=ACTOR, recorded_at=STAMP, revision_id=uuid4())
    backup = session.append_revision(prepared.record)
    assert backup.succeeded
    return prepared


def available(session, school, day):
    calendar = school_lunch_calendar_from_snapshot(session.database.snapshot(session.database.latest_revision_id()))
    return calendar.vendor_service_available(PersonProfile("합성", school_name=school), date(2027, 7, day))


def test_overlap_same_month_bulk_delete_restore_and_reopen(tmp_path):
    path = tmp_path / "test.sqlite3"
    backup = tmp_path / "backup"; backup.mkdir()
    people = tuple(Person(uuid4(), PersonProfile("합성" + school, school_name=school)) for school in ("가초", "나초"))
    roster = RosterSnapshot(uuid4(), people, enrollments=tuple(MealEnrollment(p.person_id, Meal.LUNCH) for p in people))
    with DatabaseSession.create(path, "password", "recovery", backup_directory=backup) as session:
        session.append_revision(RevisionRecord(RevisionMetadata(roster.revision_id, None, STAMP, None, ACTOR, "합성", RevisionSource.UI), roster_audit_changes(None, roster)))
        first = add(session, (row(3, "가초", 1, 9), row(4, "나초", 1, 9)))
        second = add(session, (row(3, "가초", 7, 15, Mark.SCHOOL_PROVIDES),))
        third = add(session, (row(3, "가초", 20, 23),))
        assert available(session, "가초", 2) and available(session, "가초", 8) and available(session, "가초", 21)
        assert not available(session, "가초", 7)
        remove(session, second)
        assert available(session, "가초", 7)  # underlying first import is restored
        assert not available(session, "가초", 12)
        assert available(session, "가초", 21)
        assert available(session, "나초", 2)
        assert {entry.revision_id for entry in school_calendar_import_history(session.database, session.database.latest_revision_id())} == {first, third}
        # A manually changed person date must survive deleting the import.
        before = session.database.current_roster()
        manual = InclusiveDateRange(date(2027, 7, 20), date(2027, 7, 23))
        after = replace(before, revision_id=uuid4(), persons=tuple(
            replace(person, lunch_period=manual) if person.person_id == people[0].person_id else person
            for person in before.persons))
        session.append_revision(RevisionRecord(RevisionMetadata(after.revision_id, before.revision_id, STAMP, None, ACTOR, "날짜 수정", RevisionSource.UI), roster_audit_changes(before, after)))
        restore_target = session.database.latest_revision_id()
        saved_entities = session.database.snapshot(restore_target).entities
        remove(session, first)
        calendar = school_lunch_calendar_from_snapshot(session.database.snapshot(session.database.latest_revision_id()))
        assert {schedule.school_name for schedule in calendar.schedules} == {"가초"}
        assert not available(session, "가초", 2) and available(session, "가초", 21)
        assert session.database.current_roster().person(people[0].person_id).lunch_period == manual
        assert session.database.current_roster().person(people[1].person_id).lunch_period is None
        remove(session, third)
        assert not school_lunch_calendar_from_snapshot(session.database.snapshot(session.database.latest_revision_id())).schedules
        assert school_calendar_import_history(session.database, session.database.latest_revision_id()) == ()
        reimported = add(session, (row(3, "가초", 1, 9), row(4, "나초", 1, 9)))
        assert available(session, "나초", 2)
        assert [entry.revision_id for entry in school_calendar_import_history(session.database, reimported)] == [reimported]
        result = restore_revision(session, restore_target, session.database.latest_revision_id(), ACTOR)
        assert session.database.snapshot(result.revision_id).entities == saved_entities
        assert len(school_calendar_import_history(session.database, result.revision_id)) == 2
        session.database.check_integrity()
    with DatabaseSession.open(path, "password") as session:
        session.database.check_integrity()
        assert available(session, "가초", 7) and available(session, "가초", 21)
        assert not available(session, "가초", 12)


def test_overlapping_rows_in_one_file_use_later_row():
    from src.importing.school_calendar import build_school_lunch_schedules
    schedule = build_school_lunch_schedules(preview((row(3, "가초", 1, 9), row(4, "가초", 7, 15, Mark.SCHOOL_PROVIDES))), {})[0]
    rules = {day.service_on: day.rule.mark for day in schedule.days}
    assert rules[date(2027, 7, 7)] is Mark.SCHOOL_PROVIDES
    assert rules[date(2027, 7, 2)] is Mark.VENDOR_REQUIRED


def test_delete_controller_saves_one_revision_and_keeps_later_manual_school_edit(tmp_path):
    from PySide6.QtWidgets import QDialog
    from src.desktop_v2.school_calendar_edit import SchoolCalendarEditController
    from src.domain.school_calendar_edits import apply_school_lunch_rule_period, build_school_calendar_edit_revision
    from src.domain.entitlements import LunchDayRule
    app = QApplication.instance() or QApplication([])
    backup = tmp_path / "backup"; backup.mkdir()
    with DatabaseSession.create(tmp_path / "test.sqlite3", "password", "recovery", backup_directory=backup) as session:
        roster = RosterSnapshot(uuid4(), (Person(uuid4(), PersonProfile("합성")),))
        session.append_revision(RevisionRecord(RevisionMetadata(roster.revision_id, None, STAMP, None, ACTOR, "합성", RevisionSource.UI), roster_audit_changes(None, roster)))
        first = add(session, (row(3, "가초", 1, 9),))
        add(session, (row(3, "가초", 20, 23),))
        snapshot = session.database.snapshot(session.database.latest_revision_id())
        calendar = school_lunch_calendar_from_snapshot(snapshot)
        edited = apply_school_lunch_rule_period(calendar.schedules[0],
            InclusiveDateRange(date(2027, 7, 21), date(2027, 7, 21)), LunchDayRule(Mark.SCHOOL_PROVIDES))
        session.append_revision(build_school_calendar_edit_revision(snapshot, (edited,), actor=ACTOR,
            recorded_at=STAMP, revision_id=uuid4()).record)
        class Accepted:
            import_requested = False
            delete_import_requested = first
            def __init__(self, calendar, parent=None):
                pass
            def exec(self):
                return QDialog.DialogCode.Accepted
        class Messages:
            @staticmethod
            def critical(*args):
                raise AssertionError(args)
            warning = critical
        count = len(session.database.revision_summaries())
        result = SchoolCalendarEditController(session, dialog_type=Accepted,
            message_box=Messages, actor_provider=lambda: ACTOR).run()
        assert result.completed and result.backup_result.succeeded
        assert len(session.database.revision_summaries()) == count + 1
        assert not available(session, "가초", 2)
        assert not available(session, "가초", 21)  # manual school-meal rule survives
        assert available(session, "가초", 22)
        session.database.check_integrity()


def test_delete_button_targets_whole_import_even_when_search_hides_other_school():
    from src.importing.school_calendar import build_school_lunch_schedules
    from src.domain.school_calendar import SchoolLunchCalendar
    from src.services.school_calendar_history import SchoolCalendarImportEntry
    app = QApplication.instance() or QApplication([])
    prompts = []
    class Messages:
        StandardButton = QMessageBox.StandardButton
        @staticmethod
        def question(*args):
            prompts.append(args[2])
            return QMessageBox.StandardButton.Yes
    rows = (row(3, "가초", 1, 9), row(4, "나초", 1, 9))
    entry = SchoolCalendarImportEntry(uuid4(), "001", STAMP, "synthetic.xlsx", rows)
    dialog = SchoolCalendarEditDialog(SchoolLunchCalendar(build_school_lunch_schedules(preview(rows), {})), message_box=Messages)
    dialog.set_import_history((entry,))
    dialog._school_search.setText("가초")
    assert dialog._delete_import.isEnabled()
    dialog._delete_import.click()
    assert dialog.delete_import_requested == entry.revision_id
    assert "2개 학교" in prompts[0] and "선택한 학교만" in prompts[0]
    dialog.close()
