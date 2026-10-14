"""Entitlement and physical breakfast delivery rule tests."""

from datetime import UTC, date, datetime
from uuid import UUID

from src.domain.entitlements import (
    BreakfastDeliveryRule,
    ClosedFridayMode,
    EntitlementExclusion,
    LunchDayRule,
    MealEntitlement,
    PhysicalDeliveryCalendar,
    SchoolLunchMark,
    UnassignedReason,
    decide_entitlement,
    plan_breakfast_shipments,
)
from src.domain.roster import (
    Household,
    HouseholdMembership,
    InclusiveDateRange,
    Meal,
    MealEnrollment,
    Person,
    PersonProfile,
    RosterSnapshot,
)
from src.domain.status import (
    MealStatus,
    StatusEvent,
    resolve_status_history,
)
from src.domain.service_adjustments import (
    ServiceProvision,
    ServiceTreatment,
)


REVISION_ID = UUID("60000000-0000-0000-0000-000000000001")
PERSON_ID = UUID("60000000-0000-0000-0000-000000000002")
HOUSEHOLD_ID = UUID("60000000-0000-0000-0000-000000000003")
EVENT_ID = UUID("60000000-0000-0000-0000-000000000004")


def _roster(*, grade: str = "6학년") -> RosterSnapshot:
    return RosterSnapshot(
        REVISION_ID,
        persons=(Person(PERSON_ID, PersonProfile("홍길동", grade=grade)),),
        households=(Household(HOUSEHOLD_ID),),
        memberships=(HouseholdMembership(PERSON_ID, HOUSEHOLD_ID),),
        enrollments=(
            MealEnrollment(PERSON_ID, Meal.BREAKFAST),
            MealEnrollment(PERSON_ID, Meal.LUNCH),
        ),
    )


def test_entitlement_counts_service_dates_and_explains_every_exclusion():
    paused = StatusEvent(
        EVENT_ID,
        REVISION_ID,
        PERSON_ID,
        Meal.BREAKFAST,
        MealStatus.PAUSED,
        InclusiveDateRange(date(2026, 8, 17), date(2026, 8, 18)),
        datetime(2026, 8, 24, 16, 0, tzinfo=UTC),
        "17~18일 일시중지",
    )
    history = resolve_status_history(PERSON_ID, Meal.BREAKFAST, (paused,))

    paused_decision = decide_entitlement(
        _roster(),
        PERSON_ID,
        Meal.BREAKFAST,
        date(2026, 8, 17),
        status_history=history,
    )
    unavailable = decide_entitlement(
        _roster(),
        PERSON_ID,
        Meal.DINNER,
        date(2026, 8, 17),
        service_available=False,
    )

    assert paused_decision.entitlement is None
    assert paused_decision.exclusions == (EntitlementExclusion.PAUSED,)
    assert unavailable.exclusions == (
        EntitlementExclusion.NOT_ENROLLED,
        EntitlementExclusion.SERVICE_UNAVAILABLE,
    )


def test_replacement_provision_stays_billable_but_not_provided_is_excluded():
    service_on = date(2026, 8, 20)
    replacement = decide_entitlement(
        _roster(),
        PERSON_ID,
        Meal.BREAKFAST,
        service_on,
        service_provision=ServiceProvision(
            Meal.BREAKFAST,
            service_on,
            ServiceTreatment.REPLACEMENT,
            date(2026, 8, 19),
        ),
    )
    omitted = decide_entitlement(
        _roster(),
        PERSON_ID,
        Meal.BREAKFAST,
        service_on,
        service_provision=ServiceProvision(
            Meal.BREAKFAST,
            service_on,
            ServiceTreatment.NOT_PROVIDED,
            None,
        ),
    )

    assert replacement.entitled
    assert replacement.entitlement is not None
    assert replacement.entitlement.service_on == service_on
    assert not omitted.entitled
    assert omitted.exclusions == (EntitlementExclusion.SERVICE_UNAVAILABLE,)


def test_school_lunch_o_x_and_grade_override_have_exact_meaning():
    profile = _roster().person(PERSON_ID).profile
    monday = date(2026, 8, 17)
    saturday = date(2026, 8, 22)
    school_provides = LunchDayRule(SchoolLunchMark.SCHOOL_PROVIDES)
    sixth_grade_override = LunchDayRule(
        SchoolLunchMark.SCHOOL_PROVIDES,
        frozenset({"6학년"}),
    )
    vendor_required = LunchDayRule(SchoolLunchMark.VENDOR_REQUIRED)

    assert not school_provides.vendor_service_available(profile, monday)
    assert sixth_grade_override.vendor_service_available(profile, monday)
    assert vendor_required.vendor_service_available(profile, monday)
    assert not vendor_required.vendor_service_available(profile, saturday)


def test_normal_friday_shipment_bundles_three_entitlements_not_one_count():
    friday = date(2026, 8, 21)
    entitlements = tuple(
        MealEntitlement(PERSON_ID, Meal.BREAKFAST, friday.replace(day=day))
        for day in (21, 22, 23)
    )
    rule = BreakfastDeliveryRule(
        "breakfast-v1",
        ClosedFridayMode.THURSDAY_WEEKEND_ONLY,
    )

    plan = plan_breakfast_shipments(
        entitlements,
        PhysicalDeliveryCalendar(),
        rule,
    )

    assert len(plan.shipments) == 1
    assert plan.shipments[0].delivery_on == friday
    assert plan.shipments[0].service_dates == tuple(
        friday.replace(day=day) for day in (21, 22, 23)
    )
    assert plan.shipments[0].entitlement_count == 3
    assert plan.entitlement_count == 3
    assert plan.unassigned == ()


def test_closed_friday_rule_can_move_only_weekend_entitlements_to_thursday():
    thursday = date(2026, 8, 20)
    friday = date(2026, 8, 21)
    entitlements = tuple(
        MealEntitlement(PERSON_ID, Meal.BREAKFAST, thursday.replace(day=day))
        for day in (20, 21, 22, 23)
    )
    calendar = PhysicalDeliveryCalendar(closed_days=frozenset({friday}))
    rule = BreakfastDeliveryRule(
        "breakfast-v1",
        ClosedFridayMode.THURSDAY_WEEKEND_ONLY,
    )

    plan = plan_breakfast_shipments(entitlements, calendar, rule)

    assert len(plan.shipments) == 1
    assert plan.shipments[0].delivery_on == thursday
    assert plan.shipments[0].service_dates == (
        thursday,
        date(2026, 8, 22),
        date(2026, 8, 23),
    )
    assert tuple(item.entitlement.service_on for item in plan.unassigned) == (friday,)
    assert plan.unassigned[0].reason is UnassignedReason.CLOSED_FRIDAY_POLICY
    assert plan.entitlement_count == 4
