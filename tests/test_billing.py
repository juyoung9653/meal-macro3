"""Service-date unit-price splitting and billing evidence tests."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from uuid import UUID

import pytest

from src.domain import (
    SERVICE_DATE_PRICING_MODE,
    Meal,
    MealEntitlement,
    PersonBillingSummary,
    UnitPriceBreakdownLine,
    UnitPriceChange,
    UnitPriceSchedule,
    price_entitlements,
)


PERSON_ID = UUID("77000000-0000-0000-0000-000000000001")
OTHER_PERSON_ID = UUID("77000000-0000-0000-0000-000000000002")
PRICE_CHANGE_ID = UUID("77000000-0000-0000-0000-000000000003")


def _schedule() -> UnitPriceSchedule:
    return UnitPriceSchedule(
        changes=(
            UnitPriceChange(
                PRICE_CHANGE_ID,
                (Meal.LUNCH,),
                11_000,
                date(2026, 9, 15),
                datetime(2026, 8, 26, 1, 0, tzinfo=UTC),
            ),
        )
    )


def _midmonth_lunch_entitlements():
    before = tuple(
        MealEntitlement(PERSON_ID, Meal.LUNCH, date(2026, 9, day))
        for day in range(1, 11)
    )
    after = tuple(
        MealEntitlement(
            PERSON_ID,
            Meal.LUNCH,
            date(2026, 9, 15) + timedelta(days=offset),
        )
        for offset in range(12)
    )
    return (*before, *after)


def test_midmonth_change_keeps_total_meal_count_and_splits_amount_by_service_date():
    billing = price_entitlements(_midmonth_lunch_entitlements(), _schedule())
    summary = billing.summary(PERSON_ID)

    assert summary.count_for(Meal.LUNCH) == 22
    assert summary.amount_for(Meal.LUNCH) == 232_000
    assert summary.price_expression_for(Meal.LUNCH) == (
        "10,000원×10식 + 11,000원×12식"
    )
    assert summary.total_count == 22
    assert summary.total_amount == 232_000
    assert tuple((line.unit_price, line.count) for line in summary.lines) == (
        (10_000, 10),
        (11_000, 12),
    )


def test_each_meal_and_person_is_aggregated_independently_and_deterministically():
    entitlements = (
        MealEntitlement(OTHER_PERSON_ID, Meal.DINNER, date(2026, 9, 20)),
        MealEntitlement(PERSON_ID, Meal.BREAKFAST, date(2026, 9, 20)),
        MealEntitlement(PERSON_ID, Meal.LUNCH, date(2026, 9, 20)),
    )

    first = price_entitlements(entitlements, _schedule())
    second = price_entitlements(reversed(entitlements), _schedule())

    assert first == second
    assert first.total_count == 3
    assert first.total_amount == 31_000
    assert first.summary(PERSON_ID).price_expression_for(Meal.BREAKFAST) == (
        "10,000원×1식"
    )
    assert first.summary(PERSON_ID).price_expression_for(Meal.LUNCH) == (
        "11,000원×1식"
    )


def test_calculation_manifest_records_mixed_price_evidence_for_regeneration():
    schedule = _schedule()
    billing = price_entitlements(_midmonth_lunch_entitlements(), schedule)

    manifest = billing.calculation_manifest(
        schedule,
        period_start=date(2026, 9, 1),
        period_end=date(2026, 9, 30),
    )

    assert manifest["unit_price_mode"] == SERVICE_DATE_PRICING_MODE
    assert manifest["unit_price_policy_version"] == 1
    assert manifest["default_unit_price"] == 10_000
    assert manifest["period_start"] == "2026-09-01"
    assert manifest["period_end"] == "2026-09-30"
    assert manifest["global_unit_price_note"] == (
        "중식 단가: 1~14일 10,000원 / 15~30일 11,000원"
    )
    assert manifest["total_count"] == 22
    assert manifest["total_amount"] == 232_000
    person = manifest["people"][0]
    assert person["person_id"] == str(PERSON_ID)
    assert person["lines"] == (
        {
            "meal": Meal.LUNCH.value,
            "unit_price": 10_000,
            "count": 10,
            "subtotal": 100_000,
        },
        {
            "meal": Meal.LUNCH.value,
            "unit_price": 11_000,
            "count": 12,
            "subtotal": 132_000,
        },
    )
    with pytest.raises(ValueError, match="schedule"):
        billing.calculation_manifest(
            UnitPriceSchedule(),
            period_start=date(2026, 9, 1),
            period_end=date(2026, 9, 30),
        )


def test_calculation_manifest_rejects_service_date_outside_billing_period():
    billing = price_entitlements(_midmonth_lunch_entitlements(), _schedule())

    with pytest.raises(ValueError, match="대상 기간 밖"):
        billing.calculation_manifest(
            _schedule(),
            period_start=date(2026, 9, 15),
            period_end=date(2026, 9, 30),
        )


def test_duplicate_entitlement_is_rejected_and_empty_input_has_zero_totals():
    entitlement = MealEntitlement(
        PERSON_ID,
        Meal.BREAKFAST,
        date(2026, 9, 1),
    )
    with pytest.raises(ValueError, match="중복"):
        price_entitlements((entitlement, entitlement), UnitPriceSchedule())

    empty = price_entitlements((), UnitPriceSchedule())
    assert empty.summaries == ()
    assert empty.total_count == 0
    assert empty.total_amount == 0


def test_person_summary_rejects_same_service_date_split_across_price_lines():
    day = date(2026, 9, 1)
    first = UnitPriceBreakdownLine(
        PERSON_ID,
        Meal.LUNCH,
        10_000,
        (day,),
    )
    second = UnitPriceBreakdownLine(
        PERSON_ID,
        Meal.LUNCH,
        11_000,
        (day,),
    )

    with pytest.raises(ValueError, match="두 번 청구"):
        PersonBillingSummary(PERSON_ID, (first, second))
