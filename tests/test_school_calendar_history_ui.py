from datetime import UTC, date, datetime
from dataclasses import replace
from uuid import uuid4

import openpyxl
import pytest
from PySide6.QtWidgets import QApplication

from src.desktop_v2.school_calendar_edit import SchoolCalendarEditDialog
from src.desktop_v2.school_grade_checks import SchoolGradeChecks
from src.domain.projections import roster_audit_changes
from src.domain.revisions import RevisionActor, RevisionMetadata, RevisionRecord, RevisionSource
from src.domain.roster import Person, PersonProfile, RosterSnapshot, Meal, MealEnrollment, InclusiveDateRange
from src.domain.school_calendar import school_lunch_calendar_from_snapshot
from src.importing.school_calendar import inspect_school_calendar_workbook, parse_school_calendar_workbook, build_school_calendar_import_revision
from src.services.school_calendar_history import school_calendar_import_history
from src.storage.database_session import DatabaseSession


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.mark.parametrize("school,count", [("예시초", 6), ("예시초등학교", 6), ("샘플중학교", 3), ("샘플고", 3), ("이름만있는학교", 6)])
def test_school_grade_checkboxes(qapp, school, count):
    widget = SchoolGradeChecks()
    widget.set_school(school)
    assert tuple(widget._checks) == tuple(f"{i}학년" for i in range(1, count + 1))
    assert widget.selected_grades() == frozenset()
    widget._checks["3학년"].setChecked(True)
    assert widget.selected_grades() == frozenset({"3학년"})
    widget.close()


def test_cumulative_history_school_search_and_actual_vacation_view(qapp, tmp_path):
    actor = RevisionActor("test", "test")
    person = Person(uuid4(), PersonProfile("합성", school_name="예시초", grade="6"))
    roster = RosterSnapshot(uuid4(), (person,), enrollments=(MealEnrollment(person.person_id, Meal.LUNCH),))
    initial = RevisionRecord(RevisionMetadata(roster.revision_id, None,
        datetime(2027, 1, 1, tzinfo=UTC), date(2027, 1, 1), actor, "합성", RevisionSource.UI),
        roster_audit_changes(None, roster))
    with DatabaseSession.create(tmp_path / "test.sqlite3", "password", "recovery") as session:
        session.append_revision(initial)
        for month, first, last in [(7, date(2027, 7, 22), date(2027, 8, 18)),
                                    (12, date(2027, 12, 23), date(2028, 1, 10))]:
            path = tmp_path / f"{month}월.xlsx"
            wb = openpyxl.Workbook();ws = wb.active
            ws.append(("합성 일정",))
            ws.append(("학교명", "방학식", "방학식 급식 제공 여부", "개학식", "개학식 급식 제공 여부"))
            for school in ("예시초", "샘플중"):
                ws.append((school, first, "x", last, "O"))
            wb.save(path);wb.close()
            mapping = inspect_school_calendar_workbook(path, calendar_year=2027).proposed_mapping
            preview = parse_school_calendar_workbook(path, replace(mapping, starts_on_provided_text="O", ends_on_provided_text="O"))
            prepared = build_school_calendar_import_revision(session.database.snapshot(session.database.latest_revision_id()),
                preview, decisions={}, actor=actor, recorded_at=datetime(2027, month, 1, tzinfo=UTC))
            session.append_revision(prepared.record)
        latest = session.database.latest_revision_id()
        history = school_calendar_import_history(session.database, latest)
        assert len(history) == 2
        calendar = school_lunch_calendar_from_snapshot(session.database.snapshot(latest))
        assert calendar.vendor_service_available(person.profile, date(2027, 7, 22))
        assert calendar.vendor_service_available(person.profile, date(2027, 12, 23))
        assert session.database.current_roster().person(person.person_id).lunch_period == InclusiveDateRange()
        dialog = SchoolCalendarEditDialog(calendar)
        try:
            dialog.set_import_history(history)
            assert "12월.xlsx" in dialog._period_choice.currentText()
            dialog._period_choice.setCurrentIndex(1)
            dialog._school_search.setText("예 시 초")
            assert dialog._current_schedule().school_name == "예시초"
            assert all(date(2027, 7, 22) <= item.starts_on <= item.ends_on <= date(2027, 8, 18)
                       for item in dialog._visible_segments)
            assert dialog._ledger.rowCount() == 1
            assert dialog._ledger.item(0, 0).text() == "2027.07.22"
            assert dialog._ledger.item(0, 1).text() == "2027.08.17"
            assert dialog._ledger.columnCount() == 5
            assert len(dialog._grades._checks) == 6
            # Apply a grade exception to opening day with checkboxes.
            last_row = len(dialog._visible_segments) - 1
            dialog._ledger.selectRow(last_row)
            dialog._segment_selected(last_row, 0, -1, -1)
            from PySide6.QtCore import QDate
            dialog._grade_day.setDate(QDate(2027, 8, 18))
            dialog._grades._checks["6학년"].setChecked(True)
            before_rules = {item.service_on: item.rule for item in dialog._current_schedule().days}
            dialog._apply_grades.click()
            assert dialog._current_schedule().rule_on(date(2027, 8, 18)).grades_without_school_meal == frozenset({"6학년"})
            assert all(item.rule == before_rules[item.service_on] for item in dialog._current_schedule().days
                       if item.service_on != date(2027, 8, 18))
            dialog._period_choice.setCurrentIndex(dialog._period_choice.count() - 1)
            assert all(item.starts_on != date(2027, 7, 1) for item in dialog._visible_segments)
            dialog._school_search.setText("없는 학교")
            assert dialog._ledger.rowCount() == 0
            assert not dialog._apply.isEnabled()
            dialog._school_search.setText("샘플중")
            assert len(dialog._grades._checks) == 3
            assert dialog.changed_school_count() == 1  # filtering keeps unsaved changes
            dialog._period_choice.setCurrentIndex(0)
            dialog._show_import_rows()
            assert dialog._history_dialog.windowTitle() == "불러온 원본 기록"
            dialog._history_dialog.close()
        finally:
            dialog.close()
