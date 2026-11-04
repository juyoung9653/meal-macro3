"""Delivery-form selection filters shipments before report grouping."""

from dataclasses import replace
from datetime import date
from uuid import uuid4

import pytest

from src.domain.projections import roster_entity_states, roster_from_historical_snapshot
from src.domain.revisions import HistoricalSnapshot
from src.domain.roster import (
    DeliveryAssignment,
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
from src.services.sqlite_delivery_report_service import (
    delivery_forms_from_snapshot,
    prepare_delivery_report_data,
    prepare_physical_delivery_data,
)


DAY = date(2026, 8, 17)


def _snapshot() -> tuple[HistoricalSnapshot, tuple]:
    revision_id = uuid4()
    ids = tuple(uuid4() for _ in range(4))
    shared, only_box, default = (uuid4() for _ in range(3))
    roster = RosterSnapshot(
        revision_id,
        persons=tuple(
            Person(
                person_id,
                PersonProfile(f"아동{index + 1}"),
                (HouseholdDeliverySetting(Meal.DINNER, form="식재료 박스"),)
                if index in (1, 2) else None,
            )
            for index, person_id in enumerate(ids)
        ),
        households=(
            Household(shared, (HouseholdDeliverySetting(Meal.DINNER, form="도시락"),)),
            Household(only_box),
            Household(default),
        ),
        memberships=tuple(
            HouseholdMembership(person_id, household_id, InclusiveDateRange(date(2026, 1, 1), None))
            for person_id, household_id in zip(ids, (shared, shared, only_box, default), strict=True)
        ),
        enrollments=tuple(
            MealEnrollment(person_id, Meal.DINNER, InclusiveDateRange(date(2026, 1, 1), None))
            for person_id in ids
        ),
        assignments=tuple(
            DeliveryAssignment(person_id, None, "김기사", str(index + 1))
            for index, person_id in enumerate(ids)
        ),
    )
    return HistoricalSnapshot(revision_id, roster_entity_states(roster)), ids


def test_selected_forms_count_only_included_people_and_households():
    snapshot, ids = _snapshot()
    assert delivery_forms_from_snapshot(snapshot) == (None, "도시락", "식재료 박스")

    all_forms = prepare_delivery_report_data(snapshot, DAY, DAY, (Meal.DINNER,))
    selected = prepare_delivery_report_data(
        snapshot, DAY, DAY, (Meal.DINNER,), delivery_forms=("도시락", None),
    )

    assert all_forms.reports[0].totals.household_counts == (2, 1, 0, 0, 0)
    assert selected.reports[0].totals.household_counts == (2, 0, 0, 0, 0)
    assert selected.reports[0].totals.person_total == 2
    assert {item.person_id for item in selected.shipments} == {ids[0], ids[3]}
    assert selected.reports[0].rows[0].memo == "기본 1가구·1식\n도시락 1가구·1식"
    assert selected.calculation_manifest["selected_delivery_forms"] == (None, "도시락")
    assert selected.calculation_manifest["target_entitlement_count"] == 2
    assert selected.calculation_manifest["calculation_contract_version"] == 7
    assert all_forms.calculation_manifest["selected_delivery_forms"] is None


def test_explicit_empty_and_unknown_forms_fail_without_changing_other_shipment_api():
    snapshot, ids = _snapshot()
    with pytest.raises(ValueError, match="하나 이상"):
        prepare_delivery_report_data(snapshot, DAY, DAY, (Meal.DINNER,), delivery_forms=())
    with pytest.raises(ValueError, match="없는 배송 형태"):
        prepare_delivery_report_data(snapshot, DAY, DAY, (Meal.DINNER,), delivery_forms=("미등록",))

    physical = prepare_physical_delivery_data(snapshot, DAY, DAY, (Meal.DINNER,))
    assert {item.person_id for item in physical.shipments} == set(ids)
    assert "selected_delivery_forms" not in physical.calculation_manifest


def test_form_filter_still_applies_with_missing_household_record():
    snapshot, ids = _snapshot()
    roster = roster_from_historical_snapshot(snapshot)
    roster = replace(
        roster,
        memberships=tuple(item for item in roster.memberships if item.person_id != ids[2]),
    )
    source = HistoricalSnapshot(snapshot.revision_id, roster_entity_states(roster))

    complete = prepare_delivery_report_data(source, DAY, DAY, (Meal.DINNER,))
    assert ids[2] in {item.person_id for item in complete.shipments}
    selected = prepare_delivery_report_data(
        source, DAY, DAY, (Meal.DINNER,), delivery_forms=(None, "도시락"),
    )
    assert {item.person_id for item in selected.shipments} == {ids[0], ids[3]}
    assert selected.reports[0].totals.household_counts == (2, 0, 0, 0, 0)


def test_excluded_lunch_form_does_not_require_its_missing_school_calendar():
    snapshot, ids = _snapshot()
    roster = roster_from_historical_snapshot(snapshot)
    excluded_id = ids[1]
    person = replace(
        roster.person(excluded_id),
        delivery_settings=(HouseholdDeliverySetting(Meal.LUNCH, form="식재료 박스"),),
    )
    roster = replace(
        roster,
        persons=(person,),
        households=tuple(
            household for household in roster.households
            if household.household_id in {
                item.household_id for item in roster.memberships
                if item.person_id == excluded_id
            }
        ),
        memberships=tuple(item for item in roster.memberships if item.person_id == excluded_id),
        enrollments=(MealEnrollment(excluded_id, Meal.LUNCH, InclusiveDateRange(date(2026, 1, 1), None)),),
        assignments=tuple(item for item in roster.assignments if item.person_id == excluded_id),
    )
    source = HistoricalSnapshot(snapshot.revision_id, roster_entity_states(roster))

    unfiltered = prepare_delivery_report_data(source, DAY, DAY, (Meal.LUNCH,))
    assert unfiltered.shipments == ()
    assert unfiltered.calculation_manifest["missing_lunch_schedules"]
    selected = prepare_delivery_report_data(
        source, DAY, DAY, (Meal.LUNCH,), delivery_forms=(None,),
    )
    assert selected.reports == ()
    assert selected.shipments == ()
    assert selected.calculation_manifest["missing_lunch_schedules"] == ()
