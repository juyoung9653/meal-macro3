"""Classify roster rows without meal delivery over a selected date window."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping
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
    status_on: Callable[[UUID, Meal, date], MealStatus | None] | None = None,
    has_visible_history: Callable[[UUID], bool] | None = None,
) -> frozenset[UUID]:
    """Return people without delivery during ``days`` inclusive calendar days.

    For one day, every meal must display as STOPPED or unavailable (None).
    ``status_on`` supplies the roster display's date-specific availability.
    When ``has_visible_history`` is authoritative, a person with no visible
    meal-change rows and no currently displayed delivery or pause is inactive
    regardless of the selected window length. Without that provider, only a
    person with no meal enrollments and no effective status history receives
    the same conservative treatment.
    Longer windows require an explicit effective STOPPED interval throughout
    each enrolled meal's part of the window. PAUSED and uncovered dates
    interrupt the run. A past enrollment
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
    if status_on is not None and not callable(status_on):
        raise TypeError("상태 조회자는 호출 가능해야 합니다.")
    if has_visible_history is not None and not callable(has_visible_history):
        raise TypeError("표시 이력 조회자는 호출 가능해야 합니다.")
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
        if days == 1:
            statuses = tuple(
                status_on(person.person_id, meal, as_of)
                if status_on is not None
                else _default_status_on(roster, histories, person.person_id, meal, as_of)
                for meal in Meal
            )
            if all(_is_non_delivery_status(status) for status in statuses):
                inactive.add(person.person_id)
            continue
        if has_visible_history is not None and not has_visible_history(person.person_id):
            statuses = tuple(
                status_on(person.person_id, meal, as_of)
                if status_on is not None
                else _default_status_on(roster, histories, person.person_id, meal, as_of)
                for meal in Meal
            )
            if all(_is_non_delivery_status(status) for status in statuses):
                inactive.add(person.person_id)
            continue
        if has_visible_history is None and not enrollments and not any(intervals_by_meal.values()):
            inactive.add(person.person_id)
            continue
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


def _default_status_on(
    roster: RosterSnapshot,
    histories: Mapping[tuple[UUID, Meal], StatusHistory],
    person_id: UUID,
    meal: Meal,
    day: date,
) -> MealStatus | None:
    if meal not in roster.meals_for(person_id, on=day):
        return None
    history = histories.get((person_id, meal))
    return MealStatus.DELIVERY if history is None else history.status_on(day)


def _is_non_delivery_status(status: MealStatus | None) -> bool:
    if status is not None and not isinstance(status, MealStatus):
        raise TypeError("상태 조회 결과는 MealStatus 또는 None이어야 합니다.")
    return status is None or status is MealStatus.STOPPED


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
