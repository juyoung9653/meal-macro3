"""Classify people with a proven, continuous period without meal delivery."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from uuid import UUID

from src.domain.roster import Meal, MealEnrollment, RosterSnapshot
from src.domain.status import MealStatus, StatusHistory, StatusInterval


def inactive_person_ids(
    roster: RosterSnapshot,
    histories: Mapping[tuple[UUID, Meal], StatusHistory],
    *,
    days: int = 365,
    as_of: date | None = None,
) -> frozenset[UUID]:
    """Return people without delivery during ``days`` inclusive calendar days.

    An enrolled meal must
    have an explicit effective STOPPED interval throughout its part of that
    window. PAUSED and uncovered dates interrupt the run. A past enrollment
    ending before the window is sufficient evidence of earlier participation.
    For an enrollment with no recorded start, a dated STOPPED interval already
    in effect at the window start also establishes that earlier history.
    """

    if not isinstance(roster, RosterSnapshot):
        raise TypeError("roster는 RosterSnapshot이어야 합니다.")
    if not isinstance(histories, Mapping):
        raise TypeError("histories는 상태 이력 mapping이어야 합니다.")
    if isinstance(days, bool) or not isinstance(days, int):
        raise TypeError("일수는 정수여야 합니다.")
    if not 1 <= days <= 36500:
        raise ValueError("일수는 1~36500이어야 합니다.")
    if as_of is None:
        as_of = date.today()
    if isinstance(as_of, datetime) or not isinstance(as_of, date):
        raise TypeError("기준일은 date여야 합니다.")
    try:
        window_start = as_of - timedelta(days=days - 1)
    except OverflowError as exc:
        raise ValueError("일수가 기준일보다 큽니다.") from exc

    enrollments_by_person = defaultdict(list)
    for enrollment in roster.enrollments:
        enrollments_by_person[enrollment.person_id].append(enrollment)

    inactive: set[UUID] = set()
    for person in roster.persons:
        enrollments = enrollments_by_person[person.person_id]
        intervals_by_meal = {}
        for meal in Meal:
            history = histories.get((person.person_id, meal))
            if history is not None and (
                not isinstance(history, StatusHistory)
                or history.person_id != person.person_id
                or history.meal is not meal
            ):
                raise ValueError("상태 이력의 사람·식종이 조회 키와 다릅니다.")
            intervals_by_meal[meal] = () if history is None else history.intervals
        if not _has_prior_enrollment(enrollments, intervals_by_meal, window_start):
            continue
        if all(
            _enrollment_is_stopped(
                enrollment.period.starts_on,
                enrollment.period.ends_on,
                window_start,
                as_of,
                intervals_by_meal[enrollment.meal],
            )
            for enrollment in enrollments
        ):
            inactive.add(person.person_id)
    return frozenset(inactive)


def _has_prior_enrollment(
    enrollments: list[MealEnrollment],
    intervals_by_meal: dict[Meal, tuple[StatusInterval, ...]],
    window_start: date,
) -> bool:
    return any(
        (item.period.starts_on is not None and item.period.starts_on <= window_start)
        or (item.period.ends_on is not None and item.period.ends_on < window_start)
        or (
            item.period.starts_on is None
            and item.period.contains(window_start)
            and any(
                interval.status is MealStatus.STOPPED
                and interval.period.starts_on is not None
                and interval.period.starts_on <= window_start
                and interval.period.contains(window_start)
                for interval in intervals_by_meal[item.meal]
            )
        )
        for item in enrollments
    )


def _enrollment_is_stopped(
    starts_on: date | None,
    ends_on: date | None,
    window_start: date,
    as_of: date,
    intervals: tuple[StatusInterval, ...],
) -> bool:
    first = max(starts_on or date.min, window_start)
    last = min(ends_on or date.max, as_of)
    if first > last:
        return True

    next_required = first.toordinal()
    last_ordinal = last.toordinal()
    for interval in intervals:
        period = interval.period
        interval_first = max(period.starts_on or date.min, first).toordinal()
        interval_last = min(period.ends_on or date.max, last).toordinal()
        if interval_last < next_required:
            continue
        if interval_first > next_required or interval.status is not MealStatus.STOPPED:
            return False
        next_required = interval_last + 1
        if next_required > last_ordinal:
            return True
    return False
