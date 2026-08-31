"""Raw status event and effective interval contract tests."""

from datetime import UTC, date, datetime
from uuid import UUID

import pytest

from src.domain import (
    InclusiveDateRange,
    Meal,
    MealStatus,
    StatusEvent,
    StatusHistory,
    StatusInterval,
    resolve_status_history,
)


PERSON_ID = UUID("10000000-0000-0000-0000-000000000001")
REVISION_ID = UUID("10000000-0000-0000-0000-000000000002")
STOP_EVENT_ID = UUID("10000000-0000-0000-0000-000000000003")
DELIVERY_EVENT_ID = UUID("10000000-0000-0000-0000-000000000004")


def _event(
    event_id: UUID,
    status: MealStatus,
    period: InclusiveDateRange,
    source_text: str,
) -> StatusEvent:
    return StatusEvent(
        event_id=event_id,
        revision_id=REVISION_ID,
        person_id=PERSON_ID,
        meal=Meal.DINNER,
        status=status,
        period=period,
        recorded_at=datetime(2026, 8, 24, 15, 0, tzinfo=UTC),
        source_text=source_text,
    )


def test_raw_events_remain_distinct_from_their_derived_effective_period():
    stopped = _event(
        STOP_EVENT_ID,
        MealStatus.STOPPED,
        InclusiveDateRange(date(2026, 8, 17), None),
        "17일 중지",
    )
    resumed = _event(
        DELIVERY_EVENT_ID,
        MealStatus.DELIVERY,
        InclusiveDateRange(date(2026, 8, 19), None),
        "19일 배송",
    )
    effective_pause = StatusInterval(
        MealStatus.PAUSED,
        InclusiveDateRange(date(2026, 8, 17), date(2026, 8, 18)),
        (STOP_EVENT_ID, DELIVERY_EVENT_ID),
    )

    history = StatusHistory(
        PERSON_ID,
        Meal.DINNER,
        (stopped, resumed),
        (effective_pause,),
    )

    assert tuple(event.source_text for event in history.events) == (
        "17일 중지",
        "19일 배송",
    )
    assert history.intervals == (effective_pause,)


def test_pause_event_requires_a_closed_period():
    with pytest.raises(ValueError, match="시작일과 종료일"):
        _event(
            STOP_EVENT_ID,
            MealStatus.PAUSED,
            InclusiveDateRange(date(2026, 8, 17), None),
            "17일부터 일시중지",
        )


def test_event_recorded_at_requires_timezone():
    with pytest.raises(ValueError, match="시간대"):
        StatusEvent(
            event_id=STOP_EVENT_ID,
            revision_id=REVISION_ID,
            person_id=PERSON_ID,
            meal=Meal.DINNER,
            status=MealStatus.STOPPED,
            period=InclusiveDateRange(date(2026, 8, 17), None),
            recorded_at=datetime(2026, 8, 24, 15, 0),
        )


def test_history_rejects_adjacent_equal_intervals_that_should_be_coalesced():
    event = _event(
        STOP_EVENT_ID,
        MealStatus.STOPPED,
        InclusiveDateRange(date(2026, 8, 17), None),
        "17일 중지",
    )

    with pytest.raises(ValueError, match="하나로 묶어야"):
        StatusHistory(
            PERSON_ID,
            Meal.DINNER,
            (event,),
            (
                StatusInterval(
                    MealStatus.STOPPED,
                    InclusiveDateRange(date(2026, 8, 17), date(2026, 8, 18)),
                    (STOP_EVENT_ID,),
                ),
                StatusInterval(
                    MealStatus.STOPPED,
                    InclusiveDateRange(date(2026, 8, 19), None),
                    (STOP_EVENT_ID,),
                ),
            ),
        )


def test_resolver_infers_temporary_pause_but_preserves_resume_event():
    stopped = _event(
        STOP_EVENT_ID,
        MealStatus.STOPPED,
        InclusiveDateRange(date(2026, 8, 17), None),
        "17일 중지",
    )
    resumed = _event(
        DELIVERY_EVENT_ID,
        MealStatus.DELIVERY,
        InclusiveDateRange(date(2026, 8, 19), None),
        "19일 배송",
    )

    history = resolve_status_history(
        PERSON_ID,
        Meal.DINNER,
        (stopped, resumed),
    )

    assert history.intervals == (
        StatusInterval(
            MealStatus.PAUSED,
            InclusiveDateRange(date(2026, 8, 17), date(2026, 8, 18)),
            (STOP_EVENT_ID, DELIVERY_EVENT_ID),
        ),
        StatusInterval(
            MealStatus.DELIVERY,
            InclusiveDateRange(date(2026, 8, 19), None),
            (DELIVERY_EVENT_ID,),
        ),
    )
    assert tuple(event.source_text for event in history.events) == (
        "17일 중지",
        "19일 배송",
    )


def test_finite_delivery_override_does_not_turn_an_ongoing_stop_into_pause():
    stopped = _event(
        STOP_EVENT_ID,
        MealStatus.STOPPED,
        InclusiveDateRange(date(2026, 8, 17), None),
        "17일 중지",
    )
    delivered_temporarily = _event(
        DELIVERY_EVENT_ID,
        MealStatus.DELIVERY,
        InclusiveDateRange(date(2026, 8, 19), date(2026, 8, 20)),
        "19~20일 배송",
    )

    history = resolve_status_history(
        PERSON_ID,
        Meal.DINNER,
        (stopped, delivered_temporarily),
    )

    assert tuple((item.status, item.period) for item in history.intervals) == (
        (
            MealStatus.STOPPED,
            InclusiveDateRange(date(2026, 8, 17), date(2026, 8, 18)),
        ),
        (
            MealStatus.DELIVERY,
            InclusiveDateRange(date(2026, 8, 19), date(2026, 8, 20)),
        ),
        (
            MealStatus.STOPPED,
            InclusiveDateRange(date(2026, 8, 21), None),
        ),
    )


def test_adjacent_authored_pauses_are_coalesced_with_both_sources():
    first = _event(
        STOP_EVENT_ID,
        MealStatus.PAUSED,
        InclusiveDateRange(date(2026, 8, 17), date(2026, 8, 18)),
        "17~18일 일시중지",
    )
    second = _event(
        DELIVERY_EVENT_ID,
        MealStatus.PAUSED,
        InclusiveDateRange(date(2026, 8, 19), date(2026, 8, 20)),
        "19~20일 일시중지",
    )

    history = resolve_status_history(PERSON_ID, Meal.DINNER, (first, second))

    assert history.intervals == (
        StatusInterval(
            MealStatus.PAUSED,
            InclusiveDateRange(date(2026, 8, 17), date(2026, 8, 20)),
            (STOP_EVENT_ID, DELIVERY_EVENT_ID),
        ),
    )


def test_status_defaults_to_delivery_outside_explicit_intervals():
    history = resolve_status_history(PERSON_ID, Meal.DINNER, ())

    assert history.intervals == ()
    assert history.status_on(date(2026, 8, 17)) is MealStatus.DELIVERY
