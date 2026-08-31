"""Multi-meal status revision and delivery auto-enrollment tests."""

from datetime import UTC, date, datetime
from uuid import UUID

import pytest

from src.domain import (
    ENROLLMENT_KIND,
    STATUS_EVENT_KIND,
    InclusiveDateRange,
    Meal,
    MealEnrollment,
    MealStatus,
    Person,
    PersonProfile,
    RevisionActor,
    RevisionSource,
    RosterSnapshot,
    build_meal_status_revision,
)


CURRENT_REVISION_ID = UUID("88000000-0000-0000-0000-000000000001")
NEW_REVISION_ID = UUID("88000000-0000-0000-0000-000000000002")
PERSON_ID = UUID("88000000-0000-0000-0000-000000000003")
BREAKFAST_EVENT_ID = UUID("88000000-0000-0000-0000-000000000004")
LUNCH_EVENT_ID = UUID("88000000-0000-0000-0000-000000000005")
DINNER_EVENT_ID = UUID("88000000-0000-0000-0000-000000000006")
RECORDED_AT = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
STARTS_ON = date(2026, 9, 1)


def _roster(*enrollments: MealEnrollment) -> RosterSnapshot:
    return RosterSnapshot(
        CURRENT_REVISION_ID,
        persons=(Person(PERSON_ID, PersonProfile("홍길동")),),
        enrollments=enrollments,
    )


def _actor() -> RevisionActor:
    return RevisionActor("tester", "S-1-5-21-test")


def test_one_revision_applies_same_pause_period_to_multiple_enrolled_meals():
    roster = _roster(
        MealEnrollment(PERSON_ID, Meal.BREAKFAST),
        MealEnrollment(PERSON_ID, Meal.LUNCH),
    )
    period = InclusiveDateRange(STARTS_ON, date(2026, 9, 5))

    prepared = build_meal_status_revision(
        roster,
        PERSON_ID,
        (Meal.LUNCH, Meal.BREAKFAST),
        MealStatus.PAUSED,
        period,
        actor=_actor(),
        recorded_at=RECORDED_AT,
        revision_id=NEW_REVISION_ID,
        event_ids={
            Meal.BREAKFAST: BREAKFAST_EVENT_ID,
            Meal.LUNCH: LUNCH_EVENT_ID,
        },
    )

    assert tuple(event.meal for event in prepared.events) == (
        Meal.BREAKFAST,
        Meal.LUNCH,
    )
    assert all(event.status is MealStatus.PAUSED for event in prepared.events)
    assert all(event.period == period for event in prepared.events)
    assert prepared.auto_enrolled_meals == ()
    assert prepared.snapshot.enrollments == roster.enrollments
    assert {change.entity_kind for change in prepared.record.changes} == {
        STATUS_EVENT_KIND
    }
    assert prepared.record.metadata.source is RevisionSource.UI
    assert prepared.record.metadata.decisions["meal_status_change"] == {
        "person_id": str(PERSON_ID),
        "meals": (Meal.BREAKFAST.value, Meal.LUNCH.value),
        "status": MealStatus.PAUSED.value,
        "starts_on": STARTS_ON.isoformat(),
        "ends_on": "2026-09-05",
        "auto_enrolled_meals": (),
    }


def test_delivery_auto_enrolls_unenrolled_meal_for_requested_period():
    roster = _roster(MealEnrollment(PERSON_ID, Meal.BREAKFAST))
    period = InclusiveDateRange(STARTS_ON, date(2026, 9, 5))

    prepared = build_meal_status_revision(
        roster,
        PERSON_ID,
        (Meal.BREAKFAST, Meal.DINNER),
        MealStatus.DELIVERY,
        period,
        actor=_actor(),
        recorded_at=RECORDED_AT,
        revision_id=NEW_REVISION_ID,
        event_ids={
            Meal.BREAKFAST: BREAKFAST_EVENT_ID,
            Meal.DINNER: DINNER_EVENT_ID,
        },
    )

    assert prepared.auto_enrolled_meals == (Meal.DINNER,)
    assert prepared.snapshot.meals_for(PERSON_ID, on=STARTS_ON) == (
        Meal.BREAKFAST,
        Meal.DINNER,
    )
    assert prepared.snapshot.meals_for(
        PERSON_ID,
        on=date(2026, 9, 6),
    ) == (Meal.BREAKFAST,)
    assert [
        change.entity_kind for change in prepared.record.changes
    ].count(ENROLLMENT_KIND) == 1
    assert [
        change.entity_kind for change in prepared.record.changes
    ].count(STATUS_EVENT_KIND) == 2
    assert prepared.record.metadata.decisions["meal_status_change"][
        "auto_enrolled_meals"
    ] == (Meal.DINNER.value,)


def test_delivery_period_merges_with_adjacent_future_enrollment():
    roster = _roster(
        MealEnrollment(
            PERSON_ID,
            Meal.DINNER,
            InclusiveDateRange(date(2026, 9, 10), None),
        )
    )

    prepared = build_meal_status_revision(
        roster,
        PERSON_ID,
        (Meal.DINNER,),
        MealStatus.DELIVERY,
        InclusiveDateRange(STARTS_ON, date(2026, 9, 9)),
        actor=_actor(),
        recorded_at=RECORDED_AT,
        revision_id=NEW_REVISION_ID,
        event_ids={Meal.DINNER: DINNER_EVENT_ID},
    )

    assert prepared.snapshot.enrollments == (
        MealEnrollment(
            PERSON_ID,
            Meal.DINNER,
            InclusiveDateRange(STARTS_ON, None),
        ),
    )
    assert prepared.auto_enrolled_meals == (Meal.DINNER,)


def test_pause_or_stop_rejects_meal_not_enrolled_on_start_date():
    roster = _roster(MealEnrollment(PERSON_ID, Meal.BREAKFAST))

    for status, period in (
        (
            MealStatus.PAUSED,
            InclusiveDateRange(STARTS_ON, date(2026, 9, 5)),
        ),
        (MealStatus.STOPPED, InclusiveDateRange(STARTS_ON, None)),
    ):
        with pytest.raises(ValueError, match="미가입"):
            build_meal_status_revision(
                roster,
                PERSON_ID,
                (Meal.DINNER,),
                status,
                period,
                actor=_actor(),
                recorded_at=RECORDED_AT,
                revision_id=NEW_REVISION_ID,
                event_ids={Meal.DINNER: DINNER_EVENT_ID},
            )
