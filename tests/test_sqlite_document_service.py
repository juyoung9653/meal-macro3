from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest

from src.domain.entitlements import LunchDayRule, SchoolLunchMark
from src.domain.projections import (
    roster_entity_states,
    status_event_state,
)
from src.domain.revisions import EntityRef, HistoricalSnapshot
from src.domain.roster import (
    Household,
    HouseholdDeliverySetting,
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
from src.domain.unit_pricing import (
    UNIT_PRICE_SCHEDULE_ID,
    UNIT_PRICE_SCHEDULE_KIND,
    UnitPriceChange,
    UnitPriceSchedule,
    unit_price_schedule_state,
)
from src.services.sqlite_document_service import prepare_monthly_document_data


def _month_days(year: int, month: int):
    current = date(year, month, 1)
    while current.month == month:
        yield current
        current += timedelta(days=1)


def _source_snapshot(*, include_school: bool = True, delivery_settings: tuple[HouseholdDeliverySetting, ...] = ()) -> HistoricalSnapshot:
    revision_id = uuid4()
    person_id = uuid4()
    household_id = uuid4()
    roster = RosterSnapshot(
        revision_id,
        persons=(
            Person(
                person_id,
                PersonProfile(
                    "홍길동",
                    birth_date=date(2015, 3, 2),
                    school_type="초등학생",
                    school_name="푸른초",
                    grade="6학년",
                    household_head_name="홍보호",
                    relationship_to_child="자녀",
                    jurisdiction="정자동",
                    road_address="성남대로 1",
                    detail_address="101호",
                    mobile_phone="010-1111-1111",
                    provider="센터",
                ),
            ),
        ),
        households=(Household(household_id, delivery_settings),),
        memberships=(
            HouseholdMembership(
                person_id,
                household_id,
                InclusiveDateRange(date(2026, 1, 1), None),
            ),
        ),
        enrollments=tuple(
            MealEnrollment(
                person_id,
                meal,
                InclusiveDateRange(date(2026, 1, 1), None),
            )
            for meal in Meal
        ),
    )
    states = dict(roster_entity_states(roster))
    if include_school:
        schedule = SchoolLunchSchoolSchedule(
            "푸른초",
            (InclusiveDateRange(date(2026, 8, 1), date(2026, 8, 31)),),
            tuple(
                SchoolLunchDay(
                    day,
                    LunchDayRule(
                        SchoolLunchMark.VENDOR_REQUIRED
                        if day == date(2026, 8, 3)
                        else SchoolLunchMark.SCHOOL_PROVIDES
                    ),
                )
                for day in _month_days(2026, 8)
                if day.weekday() < 5
            ),
        )
        states[
            EntityRef(SCHOOL_LUNCH_SCHEDULE_KIND, schedule.entity_id)
        ] = school_lunch_school_schedule_state(schedule)

    price_schedule = UnitPriceSchedule(
        changes=(
            UnitPriceChange(
                uuid4(),
                (Meal.BREAKFAST,),
                11_000,
                date(2026, 8, 16),
                datetime(2026, 8, 1, tzinfo=UTC),
            ),
        )
    )
    states[
        EntityRef(UNIT_PRICE_SCHEDULE_KIND, UNIT_PRICE_SCHEDULE_ID)
    ] = unit_price_schedule_state(price_schedule)

    adjustment = ServiceAdjustmentChange(
        uuid4(),
        InclusiveDateRange(date(2026, 8, 15), date(2026, 8, 15)),
        (Meal.BREAKFAST,),
        ServiceTreatment.NOT_PROVIDED,
        None,
        (),
        "센터 휴무",
        datetime(2026, 8, 2, tzinfo=UTC),
    )
    states[
        EntityRef(
            SERVICE_ADJUSTMENT_SCHEDULE_KIND,
            SERVICE_ADJUSTMENT_SCHEDULE_ID,
        )
    ] = service_adjustment_schedule_state(
        ServiceAdjustmentSchedule((adjustment,))
    )

    status = StatusEvent(
        uuid4(),
        revision_id,
        person_id,
        Meal.DINNER,
        MealStatus.PAUSED,
        InclusiveDateRange(date(2026, 8, 10), date(2026, 8, 12)),
        datetime(2026, 8, 3, tzinfo=UTC),
        "8월 10~12일 일시중지",
    )
    states[EntityRef("status.event", status.event_id)] = status_event_state(status)
    return HistoricalSnapshot(revision_id, states)


def test_calculates_shared_invoice_and_confirmation_facts_from_source_revision():
    prepared = prepare_monthly_document_data(
        _source_snapshot(),
        date(2026, 8, 1),
        date(2026, 8, 31),
        tuple(Meal),
    )

    assert len(prepared.batch.deliveries) == 1
    delivery = prepared.batch.deliveries[0]
    assert (
        delivery.breakfast_count,
        delivery.lunch_count,
        delivery.dinner_count,
    ) == (13, 1, 18)
    assert delivery.billing_amount == 327_000
    assert delivery.amount(prepared.batch.prices) == 327_000
    assert sum(item.breakfast_count for item in delivery.weekly) == 13
    assert sum(item.lunch_count for item in delivery.weekly) == 1
    assert sum(item.dinner_count for item in delivery.weekly) == 18
    assert prepared.global_note == (
        "조식 단가: 1~15일 10,000원 / 16~31일 11,000원"
    )
    assert prepared.calculation_manifest["selected_meals"] == (
        "조식",
        "중식",
        "석식",
    )
    assert prepared.calculation_manifest["billing"]["total_amount"] == 327_000
    assert prepared.calculation_manifest["calculation_contract_version"] == 3
    assert prepared.calculation_manifest["recurring_service_policy"]["version"] == 3


def test_blocks_enrolled_lunch_when_reviewed_school_period_is_missing():
    with pytest.raises(ValueError, match="푸른초.*중식 일정이 없습니다"):
        prepare_monthly_document_data(
            _source_snapshot(include_school=False),
            date(2026, 8, 1),
            date(2026, 8, 31),
            (Meal.LUNCH,),
        )


def test_physical_delivery_mapping_does_not_change_breakfast_billing_days():
    default = prepare_monthly_document_data(
        _source_snapshot(), date(2026, 8, 1), date(2026, 8, 31), (Meal.BREAKFAST,)
    )
    mapped = prepare_monthly_document_data(
        _source_snapshot(delivery_settings=(
            HouseholdDeliverySetting(Meal.BREAKFAST, delivery_weekdays=(0, 1, 2, 3, 4, 3, 4)),
        )), date(2026, 8, 1), date(2026, 8, 31), (Meal.BREAKFAST,)
    )
    assert default.batch.deliveries[0].breakfast_count == 13
    assert mapped.batch.deliveries[0].breakfast_count == 13
    assert mapped.batch.deliveries[0].billing_amount == default.batch.deliveries[0].billing_amount
    assert tuple(item.service_on for item in mapped.entitlements) == tuple(item.service_on for item in default.entitlements)


def test_household_weekdays_change_billing_but_custom_form_does_not():
    weekdays = HouseholdDeliverySetting(Meal.DINNER, (0, 1, 2, 3, 4), "식재료 박스")
    default = prepare_monthly_document_data(
        _source_snapshot(), date(2026, 8, 1), date(2026, 8, 31), (Meal.DINNER,)
    )
    custom = prepare_monthly_document_data(
        _source_snapshot(delivery_settings=(weekdays,)),
        date(2026, 8, 1), date(2026, 8, 31), (Meal.DINNER,)
    )
    assert len(default.entitlements) == len(custom.entitlements) == 18
    assert default.billing.total_amount == custom.billing.total_amount

    mondays = prepare_monthly_document_data(
        _source_snapshot(delivery_settings=(
            HouseholdDeliverySetting(Meal.DINNER, (0,), "식재료 박스"),
        )),
        date(2026, 8, 1), date(2026, 8, 31), (Meal.DINNER,)
    )
    assert tuple(item.service_on for item in mondays.entitlements) == (
        date(2026, 8, 3), date(2026, 8, 17), date(2026, 8, 24), date(2026, 8, 31)
    )
    assert mondays.billing.total_amount == 40_000


def test_custom_lunch_weekday_skips_unneeded_school_schedule_dates():
    snapshot = _source_snapshot(delivery_settings=(
        HouseholdDeliverySetting(Meal.LUNCH, (0,)),
    ))
    states = dict(snapshot.entities)
    school_ref = next(ref for ref in states if ref.entity_kind == SCHOOL_LUNCH_SCHEDULE_KIND)
    monday = date(2026, 8, 3)
    schedule = SchoolLunchSchoolSchedule(
        "푸른초", (InclusiveDateRange(monday, monday),),
        (SchoolLunchDay(monday, LunchDayRule(SchoolLunchMark.VENDOR_REQUIRED)),),
    )
    states[school_ref] = school_lunch_school_schedule_state(schedule)

    prepared = prepare_monthly_document_data(
        HistoricalSnapshot(snapshot.revision_id, states),
        date(2026, 8, 1), date(2026, 8, 7), (Meal.LUNCH,),
    )

    assert tuple(item.service_on for item in prepared.entitlements) == (monday,)
