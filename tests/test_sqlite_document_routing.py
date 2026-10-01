from __future__ import annotations

from datetime import date
from uuid import uuid4

import openpyxl
import pytest

from src.domain.delivery_routing import (
    DRIVER_KIND,
    ROUTING_EVENT_KIND,
    Driver,
    RoutingEvent,
)
from src.domain.projections import roster_entity_states
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
from src.generators.confirm_generator import make_confirm_publication
from src.services.sqlite_document_service import prepare_monthly_document_data
from src.templates.template_scanner import scan_placeholders


def _snapshot(*, event_specs=(), drivers: tuple[Driver, ...] = ()):
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
                    jurisdiction="정자동",
                    household_head_name="홍보호",
                    road_address="기본 도로",
                    detail_address="101호",
                    mobile_phone="010-1111-1111",
                ),
            ),
        ),
        households=(Household(household_id),),
        memberships=(
            HouseholdMembership(
                person_id, household_id, InclusiveDateRange(date(2026, 8, 1), None)
            ),
        ),
        enrollments=(
            MealEnrollment(
                person_id,
                Meal.BREAKFAST,
                InclusiveDateRange(date(2026, 8, 1), date(2026, 8, 10)),
            ),
            MealEnrollment(
                person_id,
                Meal.DINNER,
                InclusiveDateRange(date(2026, 8, 1), date(2026, 8, 10)),
            ),
        ),
        assignments=(
            DeliveryAssignment(person_id, Meal.BREAKFAST, "기존 기사", "001"),
            DeliveryAssignment(person_id, Meal.DINNER, "기존 기사", "002"),
        ),
    )
    states = dict(roster_entity_states(roster))
    for driver in drivers:
        states[EntityRef(DRIVER_KIND, driver.driver_id)] = {
            "name": driver.name,
            "telephone": driver.telephone,
        }
    for position, (effective_on, driver_ids, road, detail) in enumerate(event_specs, start=1):
        event = RoutingEvent(
            uuid4(), person_id, effective_on, position, driver_ids, road, detail
        )
        states[EntityRef(ROUTING_EVENT_KIND, event.event_id)] = {
            "person_id": str(event.person_id),
            "effective_on": event.effective_on.isoformat(),
            "position": event.position,
            "driver_ids": {
                meal.value: None if driver_id is None else str(driver_id)
                for meal, driver_id in event.driver_ids.items()
            },
            "road_address": event.road_address,
            "detail_address": event.detail_address,
        }
    return HistoricalSnapshot(revision_id, states), person_id


def _counts_and_amount(prepared):
    delivery = prepared.batch.deliveries[0]
    return (
        delivery.breakfast_count,
        delivery.lunch_count,
        delivery.dinner_count,
        delivery.billing_amount,
    )


def test_document_period_end_routing_changes_presentation_only(tmp_path):
    baseline_snapshot, _ = _snapshot()
    baseline = prepare_monthly_document_data(
        baseline_snapshot, date(2026, 8, 1), date(2026, 8, 20), (Meal.BREAKFAST, Meal.DINNER)
    )

    morning = Driver(uuid4(), "등록 기사")
    evening = Driver(uuid4(), "등록 기사")
    routed_snapshot, _ = _snapshot(
        drivers=(morning, evening),
        event_specs=(
            (date(2026, 8, 15), {Meal.BREAKFAST: morning.driver_id, Meal.DINNER: evening.driver_id}, "변경 도로", ""),
            (date(2026, 8, 21), {Meal.BREAKFAST: None}, "미래 도로", "999호"),
        ),
    )

    routed = prepare_monthly_document_data(
        routed_snapshot, date(2026, 8, 1), date(2026, 8, 20), (Meal.BREAKFAST, Meal.DINNER)
    )

    assert _counts_and_amount(routed) == _counts_and_amount(baseline)
    assert len(routed.batch.deliveries) == 1  # entitlement ended before document end
    delivery = routed.batch.deliveries[0]
    assert delivery.delivery_staff == "등록 기사"
    assert delivery.roster.extra["도로명주소"] == "변경 도로"
    assert delivery.roster.extra["상세주소"] == ""
    assert delivery.roster.extra["주소"] == "변경 도로"
    assert routed.calculation_manifest["calculation_contract_version"] == 3
    assert routed.calculation_manifest["document_presentation"] == {
        "policy_version": 1,
        "effective_on": "2026-08-20",
        "routing_basis": "document_period_end",
    }

    template = "assets/급식_확인서템플릿.xlsx"
    if not __import__("os").path.exists(template):
        pytest.skip("급식 확인서 템플릿 없음")
    output = tmp_path / "routing-confirm.xlsx"
    created, _ = make_confirm_publication(routed.batch, {}, output, template, 2026, 8)
    workbook = openpyxl.load_workbook(output, data_only=False)
    try:
        worksheet = workbook[created[0]["sheet_name"]]
        placeholders = scan_placeholders(template, ["1명"])["1명"]
        assert worksheet["K3"].value == "등록 기사"
        assert worksheet.cell(*placeholders["addr"][0]).value == "변경 도로"
        assert (
            worksheet.cell(*placeholders["sum_b_1_1"][0]).value
            == delivery.weekly[0].breakfast_count
        )
    finally:
        workbook.close()


def test_legacy_routing_fallback_and_end_date_unassignment_are_rendered(tmp_path):
    legacy_snapshot, _ = _snapshot()
    legacy = prepare_monthly_document_data(
        legacy_snapshot, date(2026, 8, 1), date(2026, 8, 20), (Meal.BREAKFAST,)
    )
    assert legacy.batch.deliveries[0].delivery_staff == "기존 기사"

    driver = Driver(uuid4(), "등록 기사")
    routed_snapshot, _ = _snapshot(
        drivers=(driver,),
        event_specs=((date(2026, 8, 20), {Meal.BREAKFAST: None}, "새 도로", ""),),
    )
    routed = prepare_monthly_document_data(
        routed_snapshot,
        date(2026, 8, 1), date(2026, 8, 20), (Meal.BREAKFAST,),
    )
    delivery = routed.batch.deliveries[0]
    assert delivery.delivery_staff == ""
    assert delivery.roster.extra["상세주소"] == ""
