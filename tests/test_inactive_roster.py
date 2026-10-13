"""Long-term non-delivery roster classification contract."""

from datetime import UTC, date, datetime, timedelta
from uuid import UUID

import pytest

from src.domain.roster import (
    InclusiveDateRange,
    Meal,
    MealEnrollment,
    Person,
    PersonProfile,
    RosterSnapshot,
)
from src.domain.status import MealStatus, StatusEvent, resolve_status_history
from src.services.inactive_roster import inactive_person_ids


PERSON_ID = UUID("a0000000-0000-0000-0000-000000000001")
REVISION_ID = UUID("a0000000-0000-0000-0000-000000000002")
AS_OF = date(2026, 9, 30)


def _roster(*enrollments: tuple[Meal, date | None, date | None]) -> RosterSnapshot:
    return RosterSnapshot(
        REVISION_ID,
        persons=(Person(PERSON_ID, PersonProfile("홍길동")),),
        enrollments=tuple(
            MealEnrollment(PERSON_ID, meal, InclusiveDateRange(start, end))
            for meal, start, end in enrollments
        ),
    )


def _histories(*events: tuple[Meal, MealStatus, date, date | None]):
    grouped = {}
    for ordinal, (meal, status, start, end) in enumerate(events, start=1):
        grouped.setdefault(meal, []).append(
            StatusEvent(
                event_id=UUID(int=ordinal),
                revision_id=REVISION_ID,
                person_id=PERSON_ID,
                meal=meal,
                status=status,
                period=InclusiveDateRange(start, end),
                recorded_at=datetime(2026, 9, 30, tzinfo=UTC),
            )
        )
    return {
        (PERSON_ID, meal): resolve_status_history(PERSON_ID, meal, tuple(items))
        for meal, items in grouped.items()
    }


def test_explicit_stop_for_all_enrolled_meals_qualifies_on_calendar_boundary():
    roster = _roster(
        (Meal.BREAKFAST, date(2024, 1, 1), None),
        (Meal.DINNER, date(2024, 1, 1), None),
    )
    histories = _histories(
        (Meal.BREAKFAST, MealStatus.STOPPED, date(2025, 9, 30), None),
        (Meal.DINNER, MealStatus.STOPPED, date(2025, 9, 1), None),
    )

    assert inactive_person_ids(roster, histories, as_of=AS_OF) == frozenset({PERSON_ID})


def test_unbounded_enrollment_with_dated_stop_proves_prior_history():
    roster = _roster(
        (Meal.BREAKFAST, None, None),
        (Meal.LUNCH, None, None),
        (Meal.DINNER, None, None),
    )
    histories = _histories(
        (Meal.BREAKFAST, MealStatus.STOPPED, date(2025, 9, 30), None),
        (Meal.LUNCH, MealStatus.STOPPED, date(2025, 9, 30), None),
        (Meal.DINNER, MealStatus.STOPPED, date(2025, 9, 30), None),
    )

    assert inactive_person_ids(roster, histories, as_of=AS_OF) == frozenset({PERSON_ID})


def test_unbounded_enrollment_without_history_remains_active():
    roster = _roster((Meal.DINNER, None, None))

    assert inactive_person_ids(roster, {}, as_of=AS_OF) == frozenset()


def test_one_day_classifies_all_unenrolled_meals_without_prior_history():
    assert inactive_person_ids(_roster(), {}, days=1, as_of=AS_OF) == frozenset({PERSON_ID})
    assert inactive_person_ids(_roster(), {}, days=2, as_of=AS_OF) == frozenset()


def test_one_day_uses_display_availability_for_enrolled_meal():
    roster = _roster(
        (Meal.DINNER, date(2024, 1, 1), None),
        (Meal.LUNCH, date(2024, 1, 1), None),
    )
    histories = _histories((Meal.DINNER, MealStatus.STOPPED, AS_OF, None))

    def displayed_status(_person_id, meal, day):
        assert day == AS_OF
        if meal is Meal.DINNER:
            return MealStatus.STOPPED
        return None

    assert inactive_person_ids(
        roster, histories, days=1, as_of=AS_OF, status_on=displayed_status
    ) == frozenset({PERSON_ID})
    assert inactive_person_ids(roster, histories, days=1, as_of=AS_OF) == frozenset()
    assert inactive_person_ids(
        roster, histories, days=2, as_of=AS_OF, status_on=displayed_status
    ) == frozenset()


@pytest.mark.parametrize("status", [MealStatus.DELIVERY, MealStatus.PAUSED])
def test_one_day_displayed_delivery_or_pause_stays_visible(status):
    roster = _roster()
    assert inactive_person_ids(
        roster, {}, days=1, as_of=AS_OF,
        status_on=lambda _person_id, meal, _day: status if meal is Meal.LUNCH else None,
    ) == frozenset()


def test_one_day_rejects_invalid_status_callback_result():
    with pytest.raises(TypeError, match="MealStatus 또는 None"):
        inactive_person_ids(
            _roster(), {}, days=1, as_of=AS_OF,
            status_on=lambda _person_id, _meal, _day: "-",
        )


def test_unbounded_three_meals_require_every_meal_to_be_stopped():
    roster = _roster(
        (Meal.BREAKFAST, None, None),
        (Meal.LUNCH, None, None),
        (Meal.DINNER, None, None),
    )
    histories = _histories(
        (Meal.BREAKFAST, MealStatus.STOPPED, date(2025, 1, 1), None),
        (Meal.LUNCH, MealStatus.STOPPED, date(2025, 1, 1), None),
        (Meal.DINNER, MealStatus.PAUSED, date(2026, 9, 1), AS_OF),
    )

    assert inactive_person_ids(roster, histories, as_of=AS_OF) == frozenset()


@pytest.mark.parametrize("status", [MealStatus.DELIVERY, MealStatus.PAUSED])
def test_one_day_of_delivery_or_pause_breaks_the_run(status):
    roster = _roster((Meal.DINNER, date(2024, 1, 1), None))
    histories = _histories(
        (Meal.DINNER, MealStatus.STOPPED, date(2025, 1, 1), None),
        (Meal.DINNER, status, date(2026, 2, 1), date(2026, 2, 1)),
    )

    assert inactive_person_ids(roster, histories, as_of=AS_OF) == frozenset()


def test_pause_ending_before_window_allows_new_stopped_run():
    roster = _roster((Meal.DINNER, date(2024, 1, 1), None))
    histories = _histories(
        (Meal.DINNER, MealStatus.PAUSED, date(2025, 1, 1), date(2025, 9, 29)),
        (Meal.DINNER, MealStatus.STOPPED, date(2025, 9, 30), None),
    )

    assert inactive_person_ids(roster, histories, as_of=AS_OF) == frozenset({PERSON_ID})


def test_another_enrolled_meal_defaults_to_delivery_without_history():
    roster = _roster(
        (Meal.DINNER, date(2024, 1, 1), None),
        (Meal.LUNCH, date(2024, 1, 1), None),
    )
    histories = _histories(
        (Meal.DINNER, MealStatus.STOPPED, date(2025, 1, 1), None),
    )

    assert inactive_person_ids(roster, histories, as_of=AS_OF) == frozenset()


def test_prior_ended_enrollment_proves_non_delivery_without_status_event():
    roster = _roster((Meal.LUNCH, date(2024, 1, 1), date(2025, 9, 29)))

    assert inactive_person_ids(roster, {}, as_of=AS_OF) == frozenset({PERSON_ID})


@pytest.mark.parametrize("enrollments", [(), ((Meal.DINNER, date(2026, 1, 1), date(2026, 1, 31)),)])
def test_no_enrollment_or_only_recent_enrollment_lacks_prior_evidence(enrollments):
    roster = _roster(*enrollments)

    assert inactive_person_ids(roster, {}, as_of=AS_OF) == frozenset()


def test_recent_reenrollment_with_delivery_returns_to_main_roster():
    roster = _roster(
        (Meal.DINNER, date(2024, 1, 1), date(2025, 1, 1)),
        (Meal.DINNER, date(2026, 9, 1), None),
    )

    assert inactive_person_ids(roster, {}, as_of=AS_OF) == frozenset()


def test_leap_day_is_counted_as_a_calendar_day():
    roster = _roster((Meal.DINNER, date(2023, 1, 1), None))
    histories = _histories(
        (Meal.DINNER, MealStatus.STOPPED, date(2024, 2, 29), None),
    )

    assert inactive_person_ids(roster, histories, days=32, as_of=date(2024, 3, 31)) == frozenset({PERSON_ID})
    assert inactive_person_ids(roster, histories, days=33, as_of=date(2024, 3, 31)) == frozenset()


@pytest.mark.parametrize("days", [1, 365, 1080])
def test_exact_inclusive_day_boundary(days):
    roster = _roster((Meal.DINNER, date(2020, 1, 1), None))
    first = AS_OF - timedelta(days=days - 1)
    histories = _histories((Meal.DINNER, MealStatus.STOPPED, first, None))
    assert inactive_person_ids(roster, histories, days=days, as_of=AS_OF) == frozenset({PERSON_ID})
    assert inactive_person_ids(roster, histories, days=days + 1, as_of=AS_OF) == frozenset()


def test_stop_after_window_start_has_not_reached_threshold():
    roster = _roster((Meal.DINNER, date(2024, 1, 1), None))
    histories = _histories(
        (Meal.DINNER, MealStatus.STOPPED, date(2025, 10, 2), None),
    )

    assert inactive_person_ids(roster, histories, as_of=AS_OF) == frozenset()


@pytest.mark.parametrize("days", [0, 36501])
def test_invalid_day_count_is_rejected(days):
    with pytest.raises(ValueError, match="1~36500"):
        inactive_person_ids(_roster(), {}, days=days, as_of=AS_OF)
