"""School-only imports fill existing lunch dates without replacing user edits."""
from datetime import date, datetime, UTC
from uuid import uuid4

import pytest

from src.domain.entitlements import LunchDayRule, SchoolLunchMark
from src.domain.lunch_periods import fill_missing_lunch_periods
from src.domain.projections import roster_audit_changes, roster_from_historical_snapshot
from src.domain.revisions import EntityRef, HistoricalSnapshot, RevisionActor
from src.domain.roster import InclusiveDateRange, Meal, MealEnrollment, Person, PersonProfile, RosterSnapshot
from src.domain.school_calendar import (
    SchoolLunchCalendar, SchoolLunchDay, SchoolLunchSchoolSchedule,
    school_lunch_school_schedule_state, SCHOOL_LUNCH_SCHEDULE_KIND,
)
from src.importing.school_calendar import (
    SchoolCalendarColumnMapping, SchoolCalendarImportPreview, SchoolVacationRow,
    build_school_calendar_import_revision, build_school_lunch_schedules,
)


def _calendar():
    return SchoolLunchCalendar((SchoolLunchSchoolSchedule(
        "예시초", (InclusiveDateRange(date(2026, 8, 17), date(2026, 8, 21)),),
        tuple(SchoolLunchDay(date(2026, 8, day), LunchDayRule(
            SchoolLunchMark.SCHOOL_PROVIDES if day in (17, 21) else SchoolLunchMark.VENDOR_REQUIRED,
            frozenset({"6학년"}) if day == 17 else frozenset(),
        )) for day in range(17, 22)),
    ),))


@pytest.mark.parametrize("grade,start,end,expected", [
    ("5", None, None, (18, 20)),
    ("6", None, None, (17, 20)),
    ("6학년", 19, None, (19, 20)),
    ("5", None, 18, (18, 18)),
    ("5", 21, None, None),
])
def test_fill_uses_school_grade_and_existing_enrollment(grade, start, end, expected):
    person = Person(uuid4(), PersonProfile("합성", school_name="예시 초", grade=grade))
    enrollment = MealEnrollment(person.person_id, Meal.LUNCH, InclusiveDateRange(
        None if start is None else date(2026, 8, start),
        None if end is None else date(2026, 8, end),
    ))
    roster = RosterSnapshot(uuid4(), (person,), enrollments=(enrollment,))
    filled = fill_missing_lunch_periods(roster, _calendar())
    assert filled.enrollments == roster.enrollments
    assert filled.persons[0].lunch_period == (
        None if expected is None else InclusiveDateRange(
            date(2026, 8, expected[0]), date(2026, 8, expected[1]))
    )


def test_fill_preserves_manual_dates_clears_other_schools_and_unregistered_people():
    manual = InclusiveDateRange(date(2026, 8, 19), date(2026, 8, 31))
    open_end = InclusiveDateRange(date(2026, 8, 18))
    people = tuple(Person(uuid4(), PersonProfile(str(index), school_name=school), lunch_period=period)
                   for index, (school, period) in enumerate((
                       ("예시초", manual), ("예시초", InclusiveDateRange()),
                       ("예시초", open_end), ("다른초", None), (None, None), ("예시초", None))))
    roster = RosterSnapshot(uuid4(), people, enrollments=tuple(
        MealEnrollment(person.person_id, Meal.LUNCH) for person in people[:-1]))
    assert fill_missing_lunch_periods(roster, _calendar()) == roster


def test_reimport_unchanged_calendar_can_fill_legacy_dates_then_is_noop():
    person = Person(uuid4(), PersonProfile("합성", school_name="예시초", grade="6"))
    roster = RosterSnapshot(uuid4(), (person,), enrollments=(MealEnrollment(person.person_id, Meal.LUNCH),))
    mapping = SchoolCalendarColumnMapping("일정", 2, 3, 2026, 1, 2, 3, 4, 5)
    preview = SchoolCalendarImportPreview(
        "synthetic.xlsx", "a" * 64, 100, "일정", "b" * 64, mapping,
        InclusiveDateRange(date(2026, 8, 1), date(2026, 8, 31)),
        (SchoolVacationRow(3, "예시초", date(2026, 8, 17), SchoolLunchMark.SCHOOL_PROVIDES,
                           date(2026, 8, 21), SchoolLunchMark.SCHOOL_PROVIDES),), (), (),
    )
    schedule = build_school_lunch_schedules(preview, {})[0]
    entities = {EntityRef(c.entity_kind, c.entity_id): c.after for c in roster_audit_changes(None, roster)}
    entities[EntityRef(SCHOOL_LUNCH_SCHEDULE_KIND, schedule.entity_id)] = school_lunch_school_schedule_state(schedule)
    snapshot = HistoricalSnapshot(roster.revision_id, entities)
    kwargs = dict(decisions={}, actor=RevisionActor("test", "test"), recorded_at=datetime(2026, 8, 1, tzinfo=UTC))
    prepared = build_school_calendar_import_revision(snapshot, preview, **kwargs)
    assert prepared.changed_school_names == ()
    assert len(prepared.record.changes) == 1
    assert prepared.record.metadata.decisions["school_calendar_import"]["filled_lunch_periods"] == (str(person.person_id),)
    for change in prepared.record.changes:
        entities[EntityRef(change.entity_kind, change.entity_id)] = change.after
    updated = HistoricalSnapshot(prepared.record.metadata.revision_id, entities)
    filled = roster_from_historical_snapshot(updated)
    assert filled.person(person.person_id).lunch_period == InclusiveDateRange(date(2026, 8, 18), date(2026, 8, 20))
    with pytest.raises(ValueError, match="달라지는 내용이 없습니다"):
        build_school_calendar_import_revision(updated, preview, **kwargs)
