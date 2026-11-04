"""Regrouped source sequences must agree with the route editor's lunch eligibility."""
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from src.desktop_v2.roster_window import RosterWindow
from src.domain.entitlements import LunchDayRule, SchoolLunchMark
from src.domain.household_grouping import build_grouping_revision
from src.domain.projections import roster_audit_changes
from src.domain.revisions import AuditChange, ChangeOperation, RevisionMetadata, RevisionRecord, RevisionSource
from src.domain.roster import DeliveryAssignment, InclusiveDateRange, Meal, MealEnrollment
from src.domain.school_calendar import (
    SCHOOL_LUNCH_SCHEDULE_KIND, SchoolLunchDay, SchoolLunchSchoolSchedule,
    school_lunch_calendar_from_snapshot, school_lunch_school_schedule_state,
)
from src.domain.status import MealStatus
from src.storage.database_session import DatabaseSession
from tests.test_household_sequence_sync import seed
from tests.test_driver_households import uid
from tests.test_mobile_household_merge import ACTOR, DAY, NOW


def add_lunch(session, mode, existing):
    before = seed(session, merged=True)
    persons = tuple(replace(p, profile=replace(p.profile,
        school_name=None if mode == "no_school" else "Synthetic school", grade="1"))
        for p in before.persons)
    assignments = before.assignments
    if existing:
        assignments += tuple(DeliveryAssignment(p.person_id,
            None if existing == "common" else Meal.LUNCH, "lunch-driver", "077")
            for p in persons)
    enrollments = before.enrollments
    if mode != "unenrolled":
        enrollments += tuple(MealEnrollment(p.person_id, Meal.LUNCH) for p in persons)
    after = replace(before, revision_id=uuid4(), persons=persons,
                    assignments=assignments, enrollments=enrollments)
    changes = list(roster_audit_changes(before, after))
    if mode not in ("no_school", "missing", "unenrolled"):
        day = DAY + timedelta(days=1) if mode == "uncovered" else DAY
        rule = LunchDayRule(SchoolLunchMark.VENDOR_REQUIRED if mode in ("vendor", "uncovered")
                            else SchoolLunchMark.SCHOOL_PROVIDES,
                            frozenset({"1"}) if mode == "grade_exception" else frozenset())
        schedule = SchoolLunchSchoolSchedule("Synthetic school", (InclusiveDateRange(day, day),),
                                            (SchoolLunchDay(day, rule),))
        changes.append(AuditChange(SCHOOL_LUNCH_SCHEDULE_KIND, schedule.entity_id,
            ChangeOperation.INSERT, None, school_lunch_school_schedule_state(schedule)))
    session.append_revision(RevisionRecord(
        RevisionMetadata(after.revision_id, before.revision_id, NOW, DAY, ACTOR,
                         "lunch fixture", RevisionSource.UI), tuple(changes)))
    return session.database.current_roster()


@pytest.mark.parametrize("mode", ["no_school", "missing", "uncovered", "school", "unenrolled", "vendor", "grade_exception"])
@pytest.mark.parametrize("existing", [None, "explicit", "common"])
def test_reapply_uses_route_editor_eligibility_and_clears_phantom_lunch(tmp_path, mode, existing):
    app = QApplication.instance() or QApplication([])
    expected = mode in ("vendor", "grade_exception")
    path = tmp_path / "lunch.db"
    with DatabaseSession.create(path, "password") as session:
        before = add_lunch(session, mode, existing)
        record = build_grouping_revision(session.database.snapshot(before.revision_id), before,
            ("mobile",), DAY, actor=ACTOR, recorded_at=NOW, revision_id=uuid4())
        session.append_revision(record)
        after = session.database.current_roster()
        calendar = school_lunch_calendar_from_snapshot(session.database.snapshot(after.revision_id))
        settings = QSettings(str(tmp_path / "view.ini"), QSettings.IniFormat)
        window = RosterWindow(after, school_calendar=calendar,
                              column_settings=settings, inactive_settings=settings)
        try:
            window.table_model.set_context(DAY, Meal.LUNCH)
            for p in after.persons:
                visible = window.table_model.effective_status_on(p.person_id, Meal.LUNCH, DAY)
                assert (visible is MealStatus.DELIVERY) == expected
                sequence = after.resolve_assignment(p.person_id, Meal.LUNCH).sequence
                assert bool(sequence) == expected
                context = window.table_model._contexts[p.person_id]
                if not expected:
                    assert not any(label == Meal.LUNCH.value and value for label, value in context.sequence_pairs)
            lunch_rows = [part for row in window.driver_household_rows(service_day=DAY)
                          for part in row.meal_rows if Meal.LUNCH in part.meals]
            assert bool(lunch_rows) == expected
            for row in lunch_rows:
                assert len(row.sequences) == 1
                assert all(after.resolve_assignment(pid, Meal.LUNCH).sequence == row.sequences[0]
                           for pid in row.member_ids)
            assert after.enrollments == before.enrollments
            for p in before.persons:
                for meal in Meal:
                    assert after.resolve_assignment(p.person_id, meal).driver == before.resolve_assignment(p.person_id, meal).driver
            session.database.check_integrity()
        finally:
            window.close()
    with DatabaseSession.open(path, "password") as session:
        assert all(bool(session.database.current_roster().resolve_assignment(p.person_id, Meal.LUNCH).sequence) == expected
                   for p in before.persons)


@pytest.mark.parametrize("mode", ["missing", "school", "vendor"])
def test_lunch_status_change_does_not_restore_unavailable_sequences(tmp_path, mode):
    from tests.test_meal_status_sequence import change
    with DatabaseSession.create(tmp_path / "status.db", "password") as session:
        before = add_lunch(session, mode, "common")
        after = change(session, ((uid(1), Meal.LUNCH),), MealStatus.DELIVERY, day=DAY)
        assert all(bool(after.resolve_assignment(p.person_id, Meal.LUNCH).sequence) == (mode == "vendor")
                   for p in after.persons)
        for p in before.persons:
            for meal in (Meal.DINNER, Meal.BREAKFAST):
                assert after.resolve_assignment(p.person_id, meal) == before.resolve_assignment(p.person_id, meal)
        session.database.check_integrity()


def test_lunch_cleanup_preserves_history_and_backup_replays_exactly(tmp_path):
    folder = tmp_path / "backups"
    folder.mkdir()
    with DatabaseSession.create(tmp_path / "source.db", "password", backup_directory=folder) as session:
        before = add_lunch(session, "missing", "common")
        assert all(before.resolve_assignment(p.person_id, Meal.LUNCH).sequence == "077" for p in before.persons)
        historical_before = session.database.snapshot(before.revision_id)
        record = build_grouping_revision(session.database.snapshot(before.revision_id), before,
            ("mobile",), DAY, actor=ACTOR, recorded_at=NOW, revision_id=uuid4())
        backup = session.append_revision(record)
        assert backup.succeeded, backup.error
        after = session.database.current_roster()
        assert all(after.resolve_assignment(p.person_id, Meal.LUNCH).sequence is None for p in after.persons)
        assert session.database.snapshot(before.revision_id) == historical_before
    with DatabaseSession.restore_and_open_with_user_password(
        backup.record.path, tmp_path / "restored.db", "password",
    ) as restored:
        assert restored.database.current_roster() == after
        assert restored.database.snapshot(before.revision_id) == historical_before
        restored.database.check_integrity()
