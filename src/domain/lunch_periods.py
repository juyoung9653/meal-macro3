"""Current editable lunch dates backed by historical meal enrollments."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from uuid import UUID

from .roster import InclusiveDateRange, Meal, MealEnrollment, Person, PersonProfile, RosterSnapshot
from .revisions import RevisionActor, RevisionMetadata, RevisionRecord, RevisionSource
from .school_calendar import SchoolLunchCalendar, SchoolLunchScheduleMissingError

LUNCH_DATE_COLUMNS = {"lunch_starts_on": "중식 시작일", "lunch_ends_on": "중식 종료일"}


def lunch_date(person: Person, key: str) -> date | None:
    period = person.lunch_period
    return None if period is None else getattr(period, key.removeprefix("lunch_"))


def reset_lunch_periods(roster: RosterSnapshot, starts_on: date) -> RosterSnapshot:
    """Clear current lunch dates and close registrations from a new season.

    Earlier registrations remain available for historical meal calculations.
    No other meal, assignment, person, or status event is changed.
    """
    ids = {item.person_id for item in roster.enrollments if item.meal is Meal.LUNCH}
    ids.update(person.person_id for person in roster.persons if person.lunch_period is not None)
    return set_lunch_periods(
        roster, {pid: InclusiveDateRange() for pid in ids},
        replace_from={pid: starts_on for pid in ids},
    )


def suggested_person_lunch_period(
    profile: PersonProfile, calendar: SchoolLunchCalendar | None, starts_on: date,
    *, exact_start: bool = False, ends_on: date | None = None,
) -> InclusiveDateRange:
    """Use the relevant reviewed school season; never reuse an expired season."""
    calendar = calendar or SchoolLunchCalendar()
    try:
        schedule = calendar.schedule_for(profile.school_name)
    except (KeyError, SchoolLunchScheduleMissingError):
        return InclusiveDateRange(starts_on, ends_on)
    seasons = [period for period in schedule.covered_periods if period.ends_on >= starts_on]
    if not seasons:
        return InclusiveDateRange(starts_on, ends_on)
    season = min(seasons, key=lambda period: period.starts_on)
    days = [item.service_on for item in schedule.days
            if season.contains(item.service_on) and item.service_on >= starts_on
            and calendar.vendor_service_available(profile, item.service_on)]
    if not days:
        return InclusiveDateRange(starts_on, ends_on) if exact_start else InclusiveDateRange()
    start = starts_on if exact_start else min(days)
    return InclusiveDateRange(start, ends_on if ends_on is not None else max(days))


def set_lunch_periods(
    roster: RosterSnapshot, updates: Mapping[UUID, InclusiveDateRange],
    *, replace_from: Mapping[UUID, date],
) -> RosterSnapshot:
    """Replace registrations from each boundary, preserving all earlier meals.

    An empty range clears the current fields and closes future registration.
    This never changes delivery/stop/pause events or another meal's enrollment.
    """
    enrollments = []
    for enrollment in roster.enrollments:
        pid = enrollment.person_id
        if enrollment.meal is not Meal.LUNCH or pid not in updates:
            enrollments.append(enrollment)
            continue
        boundary = replace_from[pid]
        if enrollment.period.ends_on is not None and enrollment.period.ends_on < boundary:
            enrollments.append(enrollment)
        elif (enrollment.period.starts_on or date.min) < boundary:
            enrollments.append(replace(enrollment, period=InclusiveDateRange(
                enrollment.period.starts_on, boundary - timedelta(days=1))))
    for pid, period in updates.items():
        roster.person(pid)
        if period.starts_on is not None:
            if period.starts_on < replace_from[pid]:
                raise ValueError("중식 기간의 적용 기준이 시작일보다 늦습니다.")
            enrollments.append(MealEnrollment(pid, Meal.LUNCH, period))
    return replace(roster,
        persons=tuple(replace(person, lunch_period=updates[person.person_id])
                      if person.person_id in updates else person for person in roster.persons),
        enrollments=tuple(enrollments))


@dataclass(frozen=True)
class PreparedLunchPeriodRevision:
    snapshot: RosterSnapshot
    record: RevisionRecord


def build_lunch_period_revision(
    roster: RosterSnapshot, person_id: UUID, column_key: str, value: date | None,
    *, actor: RevisionActor, recorded_at: datetime, revision_id: UUID, effective_on: date,
) -> PreparedLunchPeriodRevision | None:
    from .projections import roster_audit_changes
    if revision_id == roster.revision_id:
        raise ValueError("새 변경번호는 현재 변경번호와 달라야 합니다.")
    if column_key not in LUNCH_DATE_COLUMNS:
        raise ValueError("중식 시작일 또는 종료일을 선택해주세요.")
    person = roster.person(person_id)
    old = person.lunch_period or InclusiveDateRange()
    if value == getattr(old, column_key.removeprefix("lunch_")):
        return None
    if column_key == "lunch_starts_on":
        new = InclusiveDateRange(value, old.ends_on if value is not None else None)
    else:
        if old.starts_on is None and value is not None:
            raise ValueError("중식 시작일을 먼저 입력해주세요.")
        new = InclusiveDateRange(old.starts_on, value)
    starts = [day for day in (old.starts_on, new.starts_on) if day is not None]
    boundary = min(starts, default=effective_on)
    if person.lunch_period is None:
        # Legacy rows have no displayed range. Correct their current registration,
        # rather than retaining an unbounded interval before the newly entered start.
        candidates = [item.period for item in roster.enrollments
                      if item.person_id == person_id and item.meal is Meal.LUNCH
                      and (item.period.ends_on is None or item.period.ends_on >= effective_on
                           or item.period.contains(new.starts_on or effective_on))]
        if candidates:
            boundary = min(boundary, min(item.starts_on or date.min for item in candidates))
    updated = set_lunch_periods(roster, {person_id: new}, replace_from={person_id: boundary})
    updated = replace(updated, revision_id=revision_id)
    metadata = RevisionMetadata(revision_id, roster.revision_id, recorded_at, min(starts, default=effective_on), actor,
        "중식 기간 수정", RevisionSource.UI,
        decisions={"lunch_period_edit": {"person_id": str(person_id), "column": column_key}},
        rule_versions={"lunch_period": 1})
    return PreparedLunchPeriodRevision(updated, RevisionRecord(metadata, roster_audit_changes(roster, updated)))
