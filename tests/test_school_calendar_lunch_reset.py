"""A school-calendar import resets recipients without erasing earlier history."""
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest

from src.domain.entitlements import decide_entitlement
from src.domain.lunch_periods import reset_lunch_periods
from src.domain.projections import roster_audit_changes, roster_entity_states, status_event_audit_change
from src.domain.revisions import HistoricalSnapshot, RevisionActor, RevisionMetadata, RevisionRecord, RevisionSource
from src.domain.roster import InclusiveDateRange, Meal, MealEnrollment
from src.importing.school_calendar import (
    SchoolCalendarColumnMapping, SchoolCalendarImportPreview, build_school_calendar_import_revision,
)
from src.importing.vacation_lunch import LunchDuplicatePolicy
from src.services.happy_delivery_presence import happy_recipient_presences
from src.services.revision_restore import restore_revision
from src.services.school_calendar_delete import build_import_deletion
from src.services.school_calendar_history import school_calendar_import_history
from src.storage.database_session import DatabaseSession
from tests.test_vacation_lunch_import import _roster, _preview, _stopped, KEEP, OMIT, RESTORE
from tests.test_vacation_lunch_period_import import _build, calendar_entry

START = date(2026, 8, 17)
ACTOR = RevisionActor("test", "synthetic")
STAMP = datetime(2026, 8, 1, tzinfo=UTC)


def calendar_preview():
    entry = calendar_entry()
    return SchoolCalendarImportPreview(
        entry.filename, "a" * 64, 100, "일정", "b" * 64,
        SchoolCalendarColumnMapping("일정", 1, 2, 2026, 1, 2, 3, 4, 5),
        InclusiveDateRange(date(2026, 8, 1), date(2026, 8, 31)),
        entry.rows, (), (),
    )


@pytest.mark.parametrize("period", [
    InclusiveDateRange(),
    InclusiveDateRange(date(2026, 7, 1)),
    InclusiveDateRange(date(2026, 7, 1), date(2026, 7, 31)),
    InclusiveDateRange(START, date(2026, 8, 26)),
    InclusiveDateRange(date(2026, 12, 21), date(2026, 12, 31)),
])
def test_reset_removes_all_future_lunch_but_preserves_past_and_other_meals(period):
    original = _roster()
    original = replace(original, persons=tuple(
        replace(p, lunch_period=InclusiveDateRange(date(2026, 7, 1), date(2026, 7, 31)))
        for p in original.persons),
        enrollments=tuple(MealEnrollment(p.person_id, Meal.LUNCH, period) for p in original.persons)
        + (MealEnrollment(OMIT, Meal.BREAKFAST),))
    updated = reset_lunch_periods(original, START)
    for person in updated.persons:
        assert person.lunch_period == InclusiveDateRange()
        assert person.profile == original.person(person.person_id).profile
        for day in (START, date(2026, 8, 26), date(2026, 12, 23)):
            assert Meal.LUNCH not in updated.meals_for(person.person_id, on=day)
            assert not decide_entitlement(updated, person.person_id, Meal.LUNCH, day).entitled
        for day in (date(2026, 7, 1), START - timedelta(days=1)):
            assert updated.meals_for(person.person_id, on=day) == original.meals_for(person.person_id, on=day)
    assert Meal.BREAKFAST in updated.meals_for(OMIT, on=date(2026, 12, 23))
    assert updated.assignments == original.assignments
    assert reset_lunch_periods(updated, START) == updated


def test_reset_includes_other_schools_and_keeps_people_without_lunch_unchanged():
    roster = _roster()
    people = tuple(replace(p, profile=replace(p.profile, school_name="다른 학교"))
                   if p.person_id == OMIT else p for p in roster.persons)
    roster = replace(roster, persons=people,
        enrollments=tuple(e for e in roster.enrollments if e.person_id != RESTORE))
    updated = reset_lunch_periods(roster, START)
    assert updated.person(RESTORE) == roster.person(RESTORE)
    assert Meal.LUNCH not in updated.meals_for(OMIT, on=START)


def test_save_reimport_new_roster_delete_restore_and_reopen_preserve_audit(tmp_path):
    roster = _roster()
    initial = RevisionRecord(RevisionMetadata(
        roster.revision_id, None, STAMP, None, ACTOR, "합성 원본", RevisionSource.UI),
        (*roster_audit_changes(None, roster), status_event_audit_change(_stopped(KEEP))))
    path = tmp_path / "operation.sqlite3"
    backup = tmp_path / "backups"
    backup.mkdir()
    with DatabaseSession.create(path, "password", "recovery", backup_directory=backup) as session:
        session.append_revision(initial)
        initial = session.database.revision(roster.revision_id)
        source = session.database.snapshot(roster.revision_id)
        preview = calendar_preview()
        first = build_school_calendar_import_revision(source, preview, decisions={},
            actor=ACTOR, recorded_at=STAMP)
        assert session.append_revision(first.record).succeeded
        saved_first = session.database.revision(first.record.metadata.revision_id)
        current = session.database.current_roster()
        assert all(p.lunch_period == InclusiveDateRange() for p in current.persons)
        assert all(Meal.LUNCH not in current.meals_for(p.person_id, on=START) for p in current.persons)
        assert all(Meal.LUNCH in current.meals_for(p.person_id, on=START - timedelta(days=1)) for p in current.persons)
        info = first.record.metadata.decisions["school_calendar_import"]["lunch_reset"]
        assert info["starts_on"] == START.isoformat()
        assert set(info["person_ids"]) == {str(p.person_id) for p in roster.persons}
        historical = session.database.snapshot(current.revision_id)
        assert not happy_recipient_presences(historical, current, START, START, (Meal.LUNCH,))
        assert session.database.snapshot(roster.revision_id).entities == source.entities
        assert {ref: state for ref, state in historical.entities.items() if ref.entity_kind == "status.event"} == {
            ref: state for ref, state in source.entities.items() if ref.entity_kind == "status.event"}
        assert session.database.revision(roster.revision_id) == initial
        with pytest.raises(ValueError, match="달라지는 내용"):
            build_school_calendar_import_revision(historical, preview, decisions={}, actor=ACTOR, recorded_at=STAMP)

        # A selected vacation roster registers only its actual members.
        registered = _build(current, _preview("유지"), policy=LunchDuplicatePolicy.KEEP)
        session.append_revision(registered.record)
        current = session.database.current_roster()
        assert Meal.LUNCH in current.meals_for(KEEP, on=START)
        assert Meal.LUNCH not in current.meals_for(OMIT, on=START)
        assert Meal.BREAKFAST in current.meals_for(OMIT, on=START)
        assert current.person(KEEP).lunch_period == calendar_entry().period

        # Even an unchanged calendar file resets the newly populated recipients.
        second = build_school_calendar_import_revision(
            session.database.snapshot(current.revision_id), preview, decisions={}, actor=ACTOR, recorded_at=STAMP)
        assert second.changed_school_names == ()
        session.append_revision(second.record)
        assert Meal.LUNCH not in session.database.current_roster().meals_for(KEEP, on=START)
        assert len(school_calendar_import_history(session.database, second.record.metadata.revision_id)) == 2
        # Removing a calendar import does not restore recipients as a side effect.
        deleted = build_import_deletion(
            session.database.snapshot(second.record.metadata.revision_id),
            session.database.branch_revision_chain(second.record.metadata.revision_id),
            second.record.metadata.revision_id, actor=ACTOR, recorded_at=STAMP, revision_id=uuid4())
        session.append_revision(deleted.record)
        assert session.database.current_roster().person(KEEP).lunch_period == InclusiveDateRange()
        assert Meal.LUNCH not in session.database.current_roster().meals_for(KEEP, on=START)
        session.database.check_integrity()
    with DatabaseSession.open(path, "password", backup_directory=backup) as session:
        current = session.database.current_roster()
        assert Meal.LUNCH not in current.meals_for(KEEP, on=START)
        assert session.database.revision(first.record.metadata.revision_id) == saved_first
        restore_revision(session, roster.revision_id, current.revision_id, ACTOR)
        assert session.database.snapshot(session.database.latest_revision_id()).entities == source.entities
        assert session.database.revision(first.record.metadata.revision_id) == saved_first
        session.database.check_integrity()


def test_invalid_calendar_does_not_produce_a_reset_revision():
    roster = _roster()
    snapshot = HistoricalSnapshot(roster.revision_id, roster_entity_states(roster))
    preview = replace(calendar_preview(), rows=())
    with pytest.raises(ValueError):
        build_school_calendar_import_revision(snapshot, preview, decisions={}, actor=ACTOR, recorded_at=STAMP)
    assert all(Meal.LUNCH in roster.meals_for(p.person_id, on=START) for p in roster.persons)
