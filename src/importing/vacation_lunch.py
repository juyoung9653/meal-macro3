"""Vacation-scoped lunch registrations for incremental roster imports."""

from dataclasses import replace
from datetime import date, timedelta
from enum import StrEnum

from src.domain.roster import InclusiveDateRange, Meal, MealEnrollment


class LunchDuplicatePolicy(StrEnum):
    KEEP = "keep"
    UPDATE = "update"


def apply_vacation_periods(current, candidate, person_ids, entry, calendar, policy):
    """Replace only this vacation; retain other finite vacation registrations."""
    updates = {}
    skipped = set()
    enrollments = list(current.enrollments)
    for pid in sorted(person_ids, key=str):
        person = candidate.person(pid)
        rows = entry.school_rows(person.profile.school_name) if person.profile.school_name else ()
        if not rows:
            raise ValueError(
                f"{person.profile.name}: 선택한 방학 기록에 학교 일정이 없습니다. "
                "학교명과 선택한 방학 파일을 확인해주세요.")
        scope = InclusiveDateRange(min(row.starts_on for row in rows), max(row.ends_on for row in rows))
        matching = [item for item in current.enrollments
                    if item.person_id == pid and item.meal is Meal.LUNCH]
        # Unbounded legacy registrations are not a confirmed vacation.
        if policy is LunchDuplicatePolicy.KEEP and any(
            item.period.starts_on is not None and item.period.ends_on is not None
            and item.period.starts_on <= scope.ends_on and item.period.ends_on >= scope.starts_on
            for item in matching
        ):
            skipped.add(pid)
            continue
        schedule = calendar.schedule_for(person.profile.school_name)
        days = [item.service_on for item in schedule.days
                if any(row.starts_on <= item.service_on <= row.ends_on for row in rows)
                and calendar.vendor_service_available(person.profile, item.service_on)]
        if not days:
            raise ValueError(
                f"{person.profile.name}: 선택한 방학에 받을 수 있는 중식 날짜가 없습니다. "
                "학교 일정의 중식 기간과 학년 예외를 확인해주세요.")
        period = InclusiveDateRange(min(days), max(days))
        updates[pid] = period
        for item in matching:
            old = item.period
            if (old.ends_on or date.max) < scope.starts_on or (old.starts_on or date.min) > scope.ends_on:
                continue
            enrollments.remove(item)
            if (old.starts_on or date.min) < scope.starts_on:
                enrollments.append(replace(item, period=InclusiveDateRange(
                    old.starts_on, scope.starts_on - timedelta(days=1))))
            if old.ends_on is not None and old.ends_on > scope.ends_on:
                enrollments.append(replace(item, period=InclusiveDateRange(
                    scope.ends_on + timedelta(days=1), old.ends_on)))
        enrollments.append(MealEnrollment(pid, Meal.LUNCH, period))
    return replace(candidate, enrollments=tuple(enrollments),
                   persons=tuple(replace(p, lunch_period=updates[p.person_id])
                                 if p.person_id in updates else p for p in candidate.persons)), updates, skipped
