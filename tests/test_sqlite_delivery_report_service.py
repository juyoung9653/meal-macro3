from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest

from src.domain.entitlements import LunchDayRule, SchoolLunchMark
from src.domain.delivery_policies import (
    BREAKFAST_DELIVERY_POLICY_ID,
    BREAKFAST_DELIVERY_POLICY_KIND,
    BreakfastDeliveryPolicyChange,
    BreakfastDeliveryPolicySchedule,
    breakfast_delivery_policy_state,
)
from src.domain.entitlements import ClosedFridayMode
from src.domain.projections import roster_entity_states, status_event_state
from src.domain.revisions import EntityRef, HistoricalSnapshot
from src.domain.roster import (
    DeliveryAssignment,
    Household,
    HouseholdMembership,
    InclusiveDateRange,
    Meal,
    MealEnrollment,
    Person,
    PersonProfile,
    RosterSnapshot,
)
from src.domain.school_calendar import (
    SCHOOL_LUNCH_SCHEDULE_KIND,
    SchoolLunchDay,
    SchoolLunchSchoolSchedule,
    school_lunch_school_schedule_state,
)
from src.domain.service_adjustments import (
    SERVICE_ADJUSTMENT_SCHEDULE_ID,
    SERVICE_ADJUSTMENT_SCHEDULE_KIND,
    ServiceAdjustmentChange,
    ServiceAdjustmentSchedule,
    ServiceTreatment,
    service_adjustment_schedule_state,
)
from src.domain.status import MealStatus, StatusEvent
from src.services.sqlite_delivery_report_service import (
    prepare_delivery_report_data,
)


def _weekdays(starts_on: date, ends_on: date):
    current = starts_on
    while current <= ends_on:
        if current.weekday() < 5:
            yield current
        current += timedelta(days=1)


def _snapshot(
    *,
    people: int = 1,
    meals: tuple[Meal, ...] = (Meal.BREAKFAST,),
    same_household: bool = True,
    include_driver: bool = True,
    status_events: tuple[StatusEvent, ...] = (),
    adjustment: ServiceAdjustmentChange | None = None,
    breakfast_policy: BreakfastDeliveryPolicySchedule | None = None,
) -> tuple[HistoricalSnapshot, tuple]:
    revision_id = uuid4()
    person_ids = tuple(uuid4() for _ in range(people))
    household_ids = (
        (uuid4(),) * people
        if same_household
        else tuple(uuid4() for _ in range(people))
    )
    persons = tuple(
        Person(
            person_id,
            PersonProfile(
                f"아동{index + 1}",
                school_name="푸른초",
                grade="6학년",
                jurisdiction="정자동" if index == 0 else "수내동",
            ),
        )
        for index, person_id in enumerate(person_ids)
    )
    roster = RosterSnapshot(
        revision_id,
        persons=persons,
        households=tuple(
            Household(household_id)
            for household_id in dict.fromkeys(household_ids)
        ),
        memberships=tuple(
            HouseholdMembership(
                person_id,
                household_id,
                InclusiveDateRange(date(2026, 1, 1), None),
            )
            for person_id, household_id in zip(
                person_ids, household_ids, strict=True
            )
        ),
        enrollments=tuple(
            MealEnrollment(
                person_id,
                meal,
                InclusiveDateRange(date(2026, 1, 1), None),
            )
            for person_id in person_ids
            for meal in meals
        ),
        assignments=(
            tuple(
                DeliveryAssignment(
                    person_id,
                    None,
                    "김기사" if include_driver else None,
                    str(index + 1),
                )
                for index, person_id in enumerate(person_ids)
            )
        ),
    )
    states = dict(roster_entity_states(roster))
    if Meal.LUNCH in meals:
        schedule = SchoolLunchSchoolSchedule(
            "푸른초",
            (InclusiveDateRange(date(2026, 8, 1), date(2026, 9, 30)),),
            tuple(
                SchoolLunchDay(
                    day,
                    LunchDayRule(SchoolLunchMark.VENDOR_REQUIRED),
                )
                for day in _weekdays(date(2026, 8, 1), date(2026, 9, 30))
            ),
        )
        states[
            EntityRef(SCHOOL_LUNCH_SCHEDULE_KIND, schedule.entity_id)
        ] = school_lunch_school_schedule_state(schedule)
    if adjustment is not None:
        states[
            EntityRef(
                SERVICE_ADJUSTMENT_SCHEDULE_KIND,
                SERVICE_ADJUSTMENT_SCHEDULE_ID,
            )
        ] = service_adjustment_schedule_state(
            ServiceAdjustmentSchedule((adjustment,))
        )
    if breakfast_policy is not None:
        states[
            EntityRef(
                BREAKFAST_DELIVERY_POLICY_KIND,
                BREAKFAST_DELIVERY_POLICY_ID,
            )
        ] = breakfast_delivery_policy_state(breakfast_policy)
    for event in status_events:
        states[EntityRef("status.event", event.event_id)] = status_event_state(event)
    return HistoricalSnapshot(revision_id, states), person_ids


def test_breakfast_report_uses_physical_friday_and_keeps_three_entitlements():
    snapshot, _person_ids = _snapshot()
    friday = date(2026, 8, 21)

    prepared = prepare_delivery_report_data(
        snapshot,
        friday,
        friday,
        (Meal.BREAKFAST,),
    )

    assert len(prepared.reports) == 1
    report = prepared.reports[0]
    assert report.day == friday
    assert report.meal == "조식"
    assert report.totals.household_counts == (1, 0, 0, 0, 0)
    assert report.totals.person_total == 1
    assert len(prepared.shipments) == 1
    assert prepared.shipments[0].service_dates == (
        date(2026, 8, 21),
        date(2026, 8, 22),
        date(2026, 8, 23),
    )
    assert prepared.calculation_manifest["target_entitlement_count"] == 3
    assert prepared.calculation_manifest["report_period"]["basis"] == (
        "physical_delivery_day"
    )


def test_groups_by_stable_household_and_compares_previous_physical_delivery():
    monday = date(2026, 8, 3)
    tuesday = date(2026, 8, 4)
    event_id = uuid4()
    snapshot, person_ids = _snapshot(people=2, meals=(Meal.LUNCH,))
    paused = StatusEvent(
        event_id,
        snapshot.revision_id,
        person_ids[1],
        Meal.LUNCH,
        MealStatus.PAUSED,
        InclusiveDateRange(monday, monday),
        datetime(2026, 8, 1, tzinfo=UTC),
        "월요일만 일시중지",
    )
    states = dict(snapshot.entities)
    states[EntityRef("status.event", paused.event_id)] = status_event_state(paused)
    snapshot = HistoricalSnapshot(snapshot.revision_id, states)

    prepared = prepare_delivery_report_data(
        snapshot,
        tuesday,
        tuesday,
        (Meal.LUNCH,),
    )

    report = prepared.reports[0]
    assert report.totals.household_counts == (0, 1, 0, 0, 0)
    assert report.totals.person_total == 2
    assert report.rows[0].household_size_cells == (
        "0(-1)",
        "1(+1)",
        "",
        "",
        "",
    )
    assert report.rows[0].reference_areas == "수내동, 정자동"
    assert prepared.calculation_manifest["household_grouping"]["key"] == (
        "stable_household_id"
    )


def test_replacement_lunch_is_reported_on_saved_actual_delivery_day():
    service_on = date(2026, 9, 10)
    delivery_on = date(2026, 9, 9)
    adjustment = ServiceAdjustmentChange(
        uuid4(),
        InclusiveDateRange(service_on, service_on),
        (Meal.LUNCH,),
        ServiceTreatment.REPLACEMENT,
        delivery_on,
        (),
        "업체 휴무 전날 대체식",
        datetime(2026, 9, 1, tzinfo=UTC),
    )
    snapshot, _person_ids = _snapshot(
        meals=(Meal.LUNCH,),
        adjustment=adjustment,
    )

    prepared = prepare_delivery_report_data(
        snapshot,
        delivery_on,
        delivery_on,
        (Meal.LUNCH,),
    )

    assert len(prepared.reports) == 1
    assert prepared.reports[0].day == delivery_on
    assert prepared.shipments[0].service_dates == (delivery_on, service_on)
    assert prepared.reports[0].totals.person_total == 1
    assert prepared.calculation_manifest["target_entitlement_count"] == 2
    assert prepared.calculation_manifest["service_adjustments"][
        "relevant_change_ids"
    ] == (str(adjustment.change_id),)


def test_delivery_report_requires_a_driver_for_every_relevant_target():
    snapshot, _person_ids = _snapshot(include_driver=False)

    with pytest.raises(ValueError, match="배송담당이 비어 있습니다"):
        prepare_delivery_report_data(
            snapshot,
            date(2026, 8, 21),
            date(2026, 8, 21),
            (Meal.BREAKFAST,),
        )


def test_retired_closed_friday_policy_does_not_override_default_delivery():
    thursday = date(2026, 8, 20)
    friday = date(2026, 8, 21)
    closure = ServiceAdjustmentChange(
        uuid4(),
        InclusiveDateRange(friday, friday),
        (Meal.BREAKFAST,),
        ServiceTreatment.NOT_PROVIDED,
        None,
        (),
        "금요일 업체 휴무",
        datetime(2026, 8, 1, tzinfo=UTC),
    )
    policy = BreakfastDeliveryPolicySchedule(
        (
            BreakfastDeliveryPolicyChange(
                uuid4(),
                date(2026, 1, 1),
                ClosedFridayMode.SKIP_FRIDAY_WEEKEND,
                datetime(2026, 8, 1, tzinfo=UTC),
            ),
        )
    )
    snapshot, _person_ids = _snapshot(
        adjustment=closure,
        breakfast_policy=policy,
    )

    prepared = prepare_delivery_report_data(
        snapshot,
        thursday,
        thursday,
        (Meal.BREAKFAST,),
    )

    assert prepared.shipments[0].service_dates == (
        thursday, friday + timedelta(days=1), friday + timedelta(days=2),
    )
    assert prepared.calculation_manifest["target_entitlement_count"] == 3
    breakfast_manifest = prepared.calculation_manifest[
        "breakfast_physical_delivery"
    ]
    assert breakfast_manifest["used_closed_friday_modes"] == (
        ClosedFridayMode.THURSDAY_WEEKEND_ONLY.value,
    )
    assert breakfast_manifest["unassigned"] == {}


def test_selected_lunch_period_remains_strict_about_missing_school_schedule():
    # The normal fixture's earlier comparison dates may be incomplete, but an
    # enrolled lunch date inside the selected period may never be guessed.
    snapshot, _person_ids = _snapshot(meals=(Meal.LUNCH,))
    states = {
        reference: state
        for reference, state in snapshot.entities.items()
        if reference.entity_kind != SCHOOL_LUNCH_SCHEDULE_KIND
    }
    snapshot = HistoricalSnapshot(snapshot.revision_id, states)

    with pytest.raises(ValueError, match="중식 일정이 없습니다"):
        prepare_delivery_report_data(
            snapshot,
            date(2026, 8, 3),
            date(2026, 8, 3),
            (Meal.LUNCH,),
        )
