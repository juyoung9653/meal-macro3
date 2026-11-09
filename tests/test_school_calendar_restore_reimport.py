from datetime import UTC, date, datetime
from uuid import uuid4

import pytest

from src.domain.entitlements import SchoolLunchMark
from src.domain.projections import roster_audit_changes
from src.domain.revisions import RevisionActor, RevisionMetadata, RevisionRecord, RevisionSource
from src.domain.roster import RosterSnapshot, Person, PersonProfile, MealEnrollment, Meal, InclusiveDateRange
from src.domain.school_calendar import school_lunch_calendar_from_snapshot
from src.importing.school_calendar import SchoolCalendarColumnMapping, SchoolCalendarImportPreview, SchoolVacationRow, build_school_calendar_import_revision
from src.services.revision_restore import restore_revision
from src.storage.database_session import DatabaseSession
from src.storage.encrypted_database import RevisionConflictError


def test_restoring_before_school_import_allows_same_school_import_again(tmp_path):
    actor = RevisionActor("test", "test")
    stamp = datetime(2027, 7, 1, tzinfo=UTC)
    person = Person(uuid4(), PersonProfile("합성", school_name="예시초"))
    roster = RosterSnapshot(uuid4(), (person,), enrollments=(MealEnrollment(person.person_id, Meal.LUNCH),))
    preview = SchoolCalendarImportPreview(
        "synthetic.xlsx", "a" * 64, 100, "일정", "b" * 64,
        SchoolCalendarColumnMapping("일정", 2, 3, 2027, 1, 2, 3, 4, 5),
        InclusiveDateRange(date(2027, 7, 1), date(2027, 8, 31)),
        (SchoolVacationRow(3, "예시초", date(2027, 7, 22), SchoolLunchMark.VENDOR_REQUIRED,
                           date(2027, 8, 18), SchoolLunchMark.SCHOOL_PROVIDES),), (), ())
    path = tmp_path / "test.sqlite3"
    first_import = None
    with DatabaseSession.create(path, "password", "recovery") as session:
        session.append_revision(RevisionRecord(RevisionMetadata(roster.revision_id, None, stamp,
            None, actor, "합성", RevisionSource.UI), roster_audit_changes(None, roster)))
        for iteration in range(3):
            record = build_school_calendar_import_revision(
                session.database.snapshot(session.database.latest_revision_id()), preview,
                decisions={}, actor=actor, recorded_at=stamp).record
            session.append_revision(record)
            if first_import is None:
                first_import = session.database.revision(record.metadata.revision_id)
            current = session.database.current_roster()
            assert current.person(person.person_id).lunch_period == InclusiveDateRange()
            session.database.check_integrity()
            # A second INSERT while the school still exists remains a conflict.
            from dataclasses import replace
            duplicate = replace(record, metadata=replace(record.metadata, revision_id=uuid4(),
                parent_revision_id=session.database.latest_revision_id()), import_batch=None)
            with pytest.raises(RevisionConflictError):
                session.database.append_revision(duplicate)
            if iteration < 2:
                restore_revision(session, roster.revision_id, session.database.latest_revision_id(), actor)
                from src.services.school_calendar_history import school_calendar_import_history
                assert school_calendar_import_history(session.database, session.database.latest_revision_id()) == ()
        assert session.database.revision(first_import.metadata.revision_id) == first_import
    with DatabaseSession.open(path, "password") as session:
        session.database.check_integrity()
        calendar = school_lunch_calendar_from_snapshot(session.database.snapshot(session.database.latest_revision_id()))
        assert calendar.vendor_service_available(person.profile, date(2027, 7, 22))
        assert not calendar.vendor_service_available(person.profile, date(2027, 8, 18))
