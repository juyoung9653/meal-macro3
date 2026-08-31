"""Raw status events and derived status-history contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum
from uuid import UUID

from .roster import InclusiveDateRange, Meal


class MealStatus(StrEnum):
    DELIVERY = "배송"
    PAUSED = "일시중지"
    STOPPED = "중지"


@dataclass(frozen=True, slots=True)
class StatusEvent:
    """One operator-authored event, retaining its original wording."""

    event_id: UUID
    revision_id: UUID
    person_id: UUID
    meal: Meal
    status: MealStatus
    period: InclusiveDateRange
    recorded_at: datetime
    source_text: str | None = None

    def __post_init__(self) -> None:
        for value, label in (
            (self.event_id, "사건 ID"),
            (self.revision_id, "revision ID"),
            (self.person_id, "사람 ID"),
        ):
            if not isinstance(value, UUID):
                raise TypeError(f"{label}는 UUID여야 합니다.")
        if not isinstance(self.meal, Meal):
            raise TypeError("meal은 Meal이어야 합니다.")
        if not isinstance(self.status, MealStatus):
            raise TypeError("status는 MealStatus여야 합니다.")
        if not isinstance(self.period, InclusiveDateRange):
            raise TypeError("상태 기간은 InclusiveDateRange여야 합니다.")
        if not isinstance(self.recorded_at, datetime):
            raise TypeError("기록 시각은 datetime이어야 합니다.")
        if self.recorded_at.tzinfo is None or self.recorded_at.utcoffset() is None:
            raise ValueError("기록 시각에는 시간대가 있어야 합니다.")
        if self.source_text is not None:
            if not isinstance(self.source_text, str):
                raise TypeError("원문 사건은 문자열 또는 None이어야 합니다.")
            if not self.source_text.strip():
                raise ValueError("원문 사건은 빈 문자열일 수 없습니다.")

        if self.status is MealStatus.PAUSED:
            if self.period.starts_on is None or self.period.ends_on is None:
                raise ValueError("일시중지는 시작일과 종료일이 모두 필요합니다.")
        elif self.period.starts_on is None and self.period.ends_on is None:
            raise ValueError("배송과 중지는 시작일 또는 종료일이 필요합니다.")


@dataclass(frozen=True, slots=True)
class StatusInterval:
    """One calculated effective interval derived from raw events."""

    status: MealStatus
    period: InclusiveDateRange
    source_event_ids: tuple[UUID, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.status, MealStatus):
            raise TypeError("status는 MealStatus여야 합니다.")
        if not isinstance(self.period, InclusiveDateRange):
            raise TypeError("상태 기간은 InclusiveDateRange여야 합니다.")
        if self.period.starts_on is None and self.period.ends_on is None:
            raise ValueError("계산된 상태 기간은 한쪽 날짜가 필요합니다.")
        source_event_ids = tuple(self.source_event_ids)
        if not source_event_ids or any(
            not isinstance(event_id, UUID) for event_id in source_event_ids
        ):
            raise ValueError("계산된 상태 기간에는 UUID 원본 사건이 필요합니다.")
        if len(set(source_event_ids)) != len(source_event_ids):
            raise ValueError("원본 사건 ID는 중복될 수 없습니다.")
        object.__setattr__(self, "source_event_ids", source_event_ids)


@dataclass(frozen=True, slots=True)
class StatusHistory:
    """Validated raw and effective history for one person and meal."""

    person_id: UUID
    meal: Meal
    events: tuple[StatusEvent, ...]
    intervals: tuple[StatusInterval, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.person_id, UUID):
            raise TypeError("사람 ID는 UUID여야 합니다.")
        if not isinstance(self.meal, Meal):
            raise TypeError("meal은 Meal이어야 합니다.")
        events = tuple(self.events)
        intervals = tuple(self.intervals)
        event_ids: set[UUID] = set()
        for event in events:
            if not isinstance(event, StatusEvent):
                raise TypeError("events에는 StatusEvent만 허용됩니다.")
            if event.person_id != self.person_id or event.meal is not self.meal:
                raise ValueError("상태 사건의 사람·식종이 이력과 다릅니다.")
            if event.event_id in event_ids:
                raise ValueError("상태 사건 ID가 중복되었습니다.")
            event_ids.add(event.event_id)

        previous: StatusInterval | None = None
        for interval in intervals:
            if not isinstance(interval, StatusInterval):
                raise TypeError("intervals에는 StatusInterval만 허용됩니다.")
            unknown = set(interval.source_event_ids) - event_ids
            if unknown:
                raise ValueError("계산된 상태 기간이 없는 원본 사건을 참조합니다.")
            if previous is not None:
                if previous.period.overlaps(interval.period):
                    raise ValueError("계산된 상태 기간은 서로 겹칠 수 없습니다.")
                if _sort_start(previous.period) > _sort_start(interval.period):
                    raise ValueError("계산된 상태 기간은 날짜순이어야 합니다.")
                if _is_contiguous_same_status(previous, interval):
                    raise ValueError("연속된 같은 상태 기간은 하나로 묶어야 합니다.")
            previous = interval

        object.__setattr__(self, "events", events)
        object.__setattr__(self, "intervals", intervals)

    def status_on(self, day: date) -> MealStatus:
        """Return the effective status, defaulting an enrolled meal to delivery."""

        if isinstance(day, datetime) or not isinstance(day, date):
            raise TypeError("조회일은 date여야 합니다.")
        for interval in self.intervals:
            if interval.period.contains(day):
                return interval.status
        return MealStatus.DELIVERY


@dataclass(frozen=True, slots=True)
class _PaintedInterval:
    status: MealStatus
    starts_ordinal: int | None
    ends_exclusive_ordinal: int | None
    source_event_ids: tuple[UUID, ...]
    winning_event: StatusEvent


def resolve_status_history(
    person_id: UUID,
    meal: Meal,
    events: tuple[StatusEvent, ...],
) -> StatusHistory:
    """Overlay raw events and calculate non-overlapping effective intervals.

    Later-recorded events win only over the dates they cover. Tuple order breaks
    ties when two events have the same recorded timestamp, matching ledger order.
    An open-ended raw ``중지`` that is permanently superseded by an open-ended
    ``배송`` is displayed as a finite ``일시중지`` while both raw events remain.
    """

    if not isinstance(person_id, UUID):
        raise TypeError("사람 ID는 UUID여야 합니다.")
    if not isinstance(meal, Meal):
        raise TypeError("meal은 Meal이어야 합니다.")
    events = tuple(events)
    for event in events:
        if not isinstance(event, StatusEvent):
            raise TypeError("events에는 StatusEvent만 허용됩니다.")
        if event.person_id != person_id or event.meal is not meal:
            raise ValueError("상태 사건의 사람·식종이 이력과 다릅니다.")

    boundaries: set[int] = set()
    event_bounds: list[tuple[int | None, int | None]] = []
    for event in events:
        starts = (
            None
            if event.period.starts_on is None
            else event.period.starts_on.toordinal()
        )
        ends_exclusive = _ends_exclusive(event.period.ends_on)
        event_bounds.append((starts, ends_exclusive))
        if starts is not None:
            boundaries.add(starts)
        if ends_exclusive is not None:
            boundaries.add(ends_exclusive)

    ordered_boundaries = sorted(boundaries)
    segment_bounds: list[tuple[int | None, int | None]] = []
    if events:
        if not ordered_boundaries:
            segment_bounds.append((None, None))
        else:
            segment_bounds.append((None, ordered_boundaries[0]))
            segment_bounds.extend(zip(ordered_boundaries, ordered_boundaries[1:]))
            if ordered_boundaries[-1] <= date.max.toordinal():
                segment_bounds.append((ordered_boundaries[-1], None))

    painted: list[_PaintedInterval] = []
    for starts, ends_exclusive in segment_bounds:
        representative = _representative_ordinal(starts, ends_exclusive)
        candidates = [
            (event.recorded_at, index, event)
            for index, (event, bounds) in enumerate(zip(events, event_bounds))
            if _bounds_contain(bounds, representative)
        ]
        if not candidates:
            continue
        winner = max(candidates, key=lambda candidate: (candidate[0], candidate[1]))[2]
        painted.append(
            _PaintedInterval(
                winner.status,
                starts,
                ends_exclusive,
                (winner.event_id,),
                winner,
            )
        )

    interpreted = _interpret_temporary_stops(painted)
    intervals = _coalesce_painted_intervals(interpreted)
    return StatusHistory(person_id, meal, events, intervals)


def _interpret_temporary_stops(
    painted: list[_PaintedInterval],
) -> list[_PaintedInterval]:
    interpreted: list[_PaintedInterval] = []
    for index, interval in enumerate(painted):
        next_interval = painted[index + 1] if index + 1 < len(painted) else None
        is_temporary_stop = (
            interval.status is MealStatus.STOPPED
            and interval.winning_event.period.ends_on is None
            and interval.ends_exclusive_ordinal is not None
            and next_interval is not None
            and next_interval.status is MealStatus.DELIVERY
            and next_interval.ends_exclusive_ordinal is None
            and next_interval.winning_event.period.ends_on is None
        )
        if not is_temporary_stop:
            interpreted.append(interval)
            continue
        interpreted.append(
            _PaintedInterval(
                MealStatus.PAUSED,
                interval.starts_ordinal,
                interval.ends_exclusive_ordinal,
                _ordered_union(
                    interval.source_event_ids,
                    next_interval.source_event_ids,
                ),
                interval.winning_event,
            )
        )
    return interpreted


def _coalesce_painted_intervals(
    painted: list[_PaintedInterval],
) -> tuple[StatusInterval, ...]:
    merged: list[_PaintedInterval] = []
    for interval in painted:
        if (
            merged
            and merged[-1].status is interval.status
            and merged[-1].ends_exclusive_ordinal == interval.starts_ordinal
        ):
            previous = merged[-1]
            merged[-1] = _PaintedInterval(
                previous.status,
                previous.starts_ordinal,
                interval.ends_exclusive_ordinal,
                _ordered_union(
                    previous.source_event_ids,
                    interval.source_event_ids,
                ),
                interval.winning_event,
            )
        else:
            merged.append(interval)
    return tuple(
        StatusInterval(
            interval.status,
            InclusiveDateRange(
                _date_from_ordinal(interval.starts_ordinal),
                _inclusive_end(interval.ends_exclusive_ordinal),
            ),
            interval.source_event_ids,
        )
        for interval in merged
    )


def _ends_exclusive(value: date | None) -> int | None:
    return None if value is None else value.toordinal() + 1


def _date_from_ordinal(value: int | None) -> date | None:
    return None if value is None else date.fromordinal(value)


def _inclusive_end(ends_exclusive: int | None) -> date | None:
    if ends_exclusive is None:
        return None
    return date.fromordinal(ends_exclusive - 1)


def _representative_ordinal(
    starts: int | None,
    ends_exclusive: int | None,
) -> int:
    if starts is not None:
        return starts
    if ends_exclusive is not None:
        return ends_exclusive - 1
    return date.min.toordinal()


def _bounds_contain(
    bounds: tuple[int | None, int | None],
    ordinal: int,
) -> bool:
    starts, ends_exclusive = bounds
    return (starts is None or starts <= ordinal) and (
        ends_exclusive is None or ordinal < ends_exclusive
    )


def _ordered_union(first: tuple[UUID, ...], second: tuple[UUID, ...]) -> tuple[UUID, ...]:
    return tuple(dict.fromkeys((*first, *second)))


def _sort_start(period: InclusiveDateRange) -> date:
    return period.starts_on or date.min


def _is_contiguous_same_status(
    previous: StatusInterval,
    current: StatusInterval,
) -> bool:
    if previous.status is not current.status:
        return False
    if previous.period.ends_on is None or current.period.starts_on is None:
        return False
    return previous.period.ends_on + timedelta(days=1) == current.period.starts_on
