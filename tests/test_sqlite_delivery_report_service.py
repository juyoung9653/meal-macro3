from datetime import UTC, date, datetime, timedelta
from dataclasses import replace
from uuid import UUID, uuid4

import pytest

from src.domain.entitlements import ClosedFridayMode, LunchDayRule, SchoolLunchMark
from src.domain.delivery_routing import DRIVER_KIND, ROUTING_EVENT_KIND
from src.domain.projections import roster_entity_states, roster_from_historical_snapshot, status_event_state
from src.domain.revisions import EntityRef, HistoricalSnapshot
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
    delivery_settings: tuple[HouseholdDeliverySetting, ...] = (),
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
            Household(household_id, delivery_settings)
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
    for event in status_events:
        states[EntityRef("status.event", event.event_id)] = status_event_state(event)
    return HistoricalSnapshot(revision_id, states), person_ids


def test_default_dinner_week_delivers_only_monday_through_thursday():
    snapshot, _person_ids = _snapshot(meals=(Meal.DINNER,))
    prepared = prepare_delivery_report_data(
        snapshot, date(2026, 8, 17), date(2026, 8, 23), (Meal.DINNER,),
    )
    assert len(prepared.shipments) == 4
    assert {shipment.delivery_on for shipment in prepared.shipments} == {
        date(2026, 8, day) for day in (17, 18, 19, 20)
    }


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
    assert prepared.calculation_manifest["calculation_contract_version"] == 5
    assert prepared.calculation_manifest["recurring_service_policy"]["version"] == 5
    assert prepared.calculation_manifest["household_grouping"]["policy_version"] == 2
    assert prepared.calculation_manifest["report_period"]["basis"] == (
        "physical_delivery_day"
    )
    breakfast_manifest = prepared.calculation_manifest["breakfast_physical_delivery"]
    assert breakfast_manifest["closed_delivery_day"] == "exclude_entire_bundle"
    assert breakfast_manifest["unreviewed_holidays"] == "normal"


def test_empty_breakfast_report_keeps_empty_rule_usage():
    snapshot, _ = _snapshot(people=0)

    prepared = prepare_delivery_report_data(
        snapshot, date(2026, 8, 21), date(2026, 8, 21), (Meal.BREAKFAST,)
    )

    assert prepared.reports == ()
    assert prepared.shipments == ()
    breakfast_manifest = prepared.calculation_manifest["breakfast_physical_delivery"]
    assert breakfast_manifest["policy_version"] == 2


def test_explicit_breakfast_weekdays_do_not_bundle_weekend_meals():
    friday = date(2026, 8, 21)
    snapshot, _ = _snapshot(delivery_settings=(
        HouseholdDeliverySetting(Meal.BREAKFAST, (0, 1, 2, 3, 4)),
    ))

    prepared = prepare_delivery_report_data(snapshot, friday, friday, (Meal.BREAKFAST,))

    assert len(prepared.shipments) == 1
    assert prepared.shipments[0].service_dates == (friday,)
    assert prepared.calculation_manifest["target_entitlement_count"] == 1


def test_custom_breakfast_delivery_weekday_moves_weekend_meal_to_thursday():
    thursday = date(2026, 8, 20)
    snapshot, _ = _snapshot(delivery_settings=(
        HouseholdDeliverySetting(Meal.BREAKFAST, delivery_weekdays=(0, 1, 2, 3, 4, 3, 4)),
    ))

    thursday_report = prepare_delivery_report_data(snapshot, thursday, thursday, (Meal.BREAKFAST,))
    friday = thursday + timedelta(days=1)
    friday_report = prepare_delivery_report_data(snapshot, friday, friday, (Meal.BREAKFAST,))

    assert thursday_report.shipments[0].service_dates == (friday + timedelta(days=1),)
    assert friday_report.shipments[0].service_dates == (friday, friday + timedelta(days=2))


def test_custom_dinner_delivery_weekday_uses_previous_month():
    friday = date(2026, 7, 31)
    monday = date(2026, 8, 3)
    snapshot, _ = _snapshot(meals=(Meal.DINNER,), delivery_settings=(
        HouseholdDeliverySetting(Meal.DINNER, delivery_weekdays=(4, 1, 2, 3, 4, 5, 6)),
    ))

    friday_report = prepare_delivery_report_data(snapshot, friday, friday, (Meal.DINNER,))
    monday_report = prepare_delivery_report_data(snapshot, monday, monday, (Meal.DINNER,))

    assert any(item.delivery_on == friday and monday in item.service_dates for item in friday_report.shipments)
    assert all(item.delivery_on != monday for item in monday_report.shipments)


def test_service_replacement_takes_precedence_over_household_delivery_weekday():
    monday = date(2026, 8, 3)
    wednesday = monday + timedelta(days=2)
    adjustment = ServiceAdjustmentChange(
        uuid4(), InclusiveDateRange(monday, monday), (Meal.DINNER,),
        ServiceTreatment.REPLACEMENT, wednesday, (), "대체 배송",
        datetime(2026, 8, 1, tzinfo=UTC),
    )
    snapshot, _ = _snapshot(meals=(Meal.DINNER,), adjustment=adjustment, delivery_settings=(
        HouseholdDeliverySetting(Meal.DINNER, delivery_weekdays=(4, 1, 2, 3, 4, 5, 6)),
    ))

    prepared = prepare_delivery_report_data(snapshot, wednesday, wednesday, (Meal.DINNER,))

    assert any(item.delivery_on == wednesday and monday in item.service_dates for item in prepared.shipments)


def test_form_memo_separates_households_and_meals_without_changing_report_totals():
    friday = date(2026, 8, 21)
    standard, _ = _snapshot(people=2)
    # Rebuild one independent household source with a custom form, then merge
    # their roster entities into a single report snapshot.
    boxed, _ = _snapshot(people=2, delivery_settings=(
        HouseholdDeliverySetting(Meal.BREAKFAST, form="식재료 박스"),
    ))
    states = dict(standard.entities)
    states.update(boxed.entities)
    snapshot = HistoricalSnapshot(standard.revision_id, states)

    prepared = prepare_delivery_report_data(snapshot, friday, friday, (Meal.BREAKFAST,))

    report = prepared.reports[0]
    assert report.totals.household_counts == (0, 2, 0, 0, 0)
    assert report.totals.person_total == 4
    assert report.rows[0].memo == "기본 1가구·6식\n식재료 박스 1가구·6식"
    assert prepared.calculation_manifest["target_entitlement_count"] == 12


def test_same_household_members_with_different_forms_keep_one_household_total():
    friday = date(2026, 8, 21)
    snapshot, _ = _snapshot(people=2)
    roster = roster_from_historical_snapshot(snapshot)
    boxed = HouseholdDeliverySetting(Meal.BREAKFAST, form="식재료 박스")
    changed = replace(roster, persons=(replace(roster.persons[0], delivery_settings=(boxed,)), roster.persons[1]))
    states = dict(snapshot.entities)
    states.update(roster_entity_states(changed))
    prepared = prepare_delivery_report_data(HistoricalSnapshot(snapshot.revision_id, states),
                                            friday, friday, (Meal.BREAKFAST,))
    report = prepared.reports[0]
    assert report.totals.household_counts == (0, 1, 0, 0, 0)
    assert report.totals.person_total == 2
    assert report.rows[0].memo == "기본 1가구·3식\n식재료 박스 1가구·3식"


def test_replacement_does_not_add_meal_outside_explicit_weekdays():
    wednesday = date(2026, 9, 9)
    thursday = date(2026, 9, 10)
    adjustment = ServiceAdjustmentChange(
        uuid4(), InclusiveDateRange(thursday, thursday), (Meal.DINNER,),
        ServiceTreatment.REPLACEMENT, wednesday, (), "대체식",
        datetime(2026, 9, 1, tzinfo=UTC),
    )
    snapshot, _ = _snapshot(
        meals=(Meal.DINNER,), adjustment=adjustment,
        delivery_settings=(HouseholdDeliverySetting(Meal.DINNER, (2,)),),
    )

    prepared = prepare_delivery_report_data(snapshot, wednesday, wednesday, (Meal.DINNER,))

    assert len(prepared.shipments) == 1
    assert prepared.shipments[0].service_dates == (wednesday,)
    assert prepared.calculation_manifest["target_entitlement_count"] == 1


def test_weekday_dinner_has_five_meals_and_retains_replacement_on_delivery_day():
    wednesday = date(2026, 9, 9)
    thursday = date(2026, 9, 10)
    adjustment = ServiceAdjustmentChange(
        uuid4(), InclusiveDateRange(thursday, thursday), (Meal.DINNER,),
        ServiceTreatment.REPLACEMENT, wednesday, (), "대체식",
        datetime(2026, 9, 1, tzinfo=UTC),
    )
    snapshot, _ = _snapshot(
        meals=(Meal.DINNER,), adjustment=adjustment,
        delivery_settings=(HouseholdDeliverySetting(Meal.DINNER, (0, 1, 2, 3, 4)),),
    )

    prepared = prepare_delivery_report_data(
        snapshot, date(2026, 9, 7), date(2026, 9, 13), (Meal.DINNER,),
    )

    assert prepared.calculation_manifest["target_entitlement_count"] == 5
    assert {day for shipment in prepared.shipments for day in shipment.service_dates} == {
        date(2026, 9, 7), date(2026, 9, 8), wednesday, thursday, date(2026, 9, 11)
    }
    moved = next(item for item in prepared.shipments if item.delivery_on == wednesday)
    assert moved.service_dates == (wednesday, thursday)
    assert all(item.delivery_on.weekday() < 5 for item in prepared.shipments)


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
    states = dict(snapshot.entities)
    states[EntityRef(ROUTING_EVENT_KIND, uuid4())] = {
        "person_id": str(_person_ids[0]),
        "effective_on": delivery_on.isoformat(),
        "position": 1,
        "driver_ids": {},
        "road_address": None,
        "detail_address": None,
        "jurisdiction": "판교동",
    }
    snapshot = HistoricalSnapshot(snapshot.revision_id, states)

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
    assert "판교동" in prepared.reports[0].rows[0].reference_areas.split(", ")
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


def test_retired_closed_friday_policy_does_not_override_default_delivery(tmp_path):
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
    snapshot, _person_ids = _snapshot(
        adjustment=closure,
    )
    states = dict(snapshot.entities)
    states[
        EntityRef(
            "delivery.breakfast-policy",
            UUID("bfa57d7d-6264-5a36-a132-8e1b24b5139e"),
        )
    ] = {
        "policy_version": 1,
        "default_closed_friday_mode": ClosedFridayMode.THURSDAY_WEEKEND_ONLY.value,
        "changes": [{
            "change_id": str(uuid4()),
            "effective_on": "2026-01-01",
            "closed_friday_mode": "금토일_미배송",
            "recorded_at": "2026-08-01T00:00:00+00:00",
        }],
    }
    # Retired policy values are persisted data, independent of executable enums.
    from src.domain.revisions import (
        AuditChange, ChangeOperation, RevisionActor, RevisionMetadata,
        RevisionRecord, RevisionSource,
    )
    from src.storage.encrypted_database import EncryptedDatabase

    legacy_ref = EntityRef(
        "delivery.breakfast-policy", UUID("bfa57d7d-6264-5a36-a132-8e1b24b5139e")
    )
    path = tmp_path / "retired-policy.sqlite3"
    key = bytes.fromhex("11" * 32)
    record = RevisionRecord(
        RevisionMetadata(
            snapshot.revision_id, None, datetime(2026, 8, 1, tzinfo=UTC),
            date(2026, 8, 1), RevisionActor("tester", "test-sid"),
            "과거 조식 정책 보존", RevisionSource.UI,
        ),
        tuple(
            AuditChange(ref.entity_kind, ref.entity_id, ChangeOperation.INSERT,
                        None, state)
            for ref, state in states.items()
        ),
    )
    with EncryptedDatabase.create(path, key) as database:
        database.append_revision(record)
    with EncryptedDatabase.open(path, key) as database:
        database.check_integrity()
        snapshot = database.snapshot(snapshot.revision_id)
        assert snapshot.entities[legacy_ref] == record.changes[-1].after

    prepared = prepare_delivery_report_data(
        snapshot,
        thursday,
        thursday,
        (Meal.BREAKFAST,),
    )

    assert prepared.shipments == ()
    assert prepared.calculation_manifest["target_entitlement_count"] == 0
    assert prepared.calculation_manifest["breakfast_physical_delivery"]["closed_delivery_day"] == "exclude_entire_bundle"


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


def test_global_defaults_change_breakfast_delivery_without_rewriting_overrides():
    from src.domain.delivery_defaults import DELIVERY_DEFAULTS_ID, DELIVERY_DEFAULTS_KIND, delivery_defaults_state
    from src.domain.roster import standard_delivery_defaults
    snapshot, _ = _snapshot()
    states = dict(snapshot.entities)
    defaults = tuple(replace(item, weekdays=(0, 2), delivery_weekdays=(6,) * 7) if item.meal is Meal.BREAKFAST else item for item in standard_delivery_defaults())
    states[EntityRef(DELIVERY_DEFAULTS_KIND, DELIVERY_DEFAULTS_ID)] = delivery_defaults_state(defaults)
    prepared = prepare_delivery_report_data(HistoricalSnapshot(snapshot.revision_id, states), date(2026, 8, 23), date(2026, 8, 23), (Meal.BREAKFAST,))
    assert len(prepared.shipments) == 1
    assert prepared.shipments[0].delivery_on == date(2026, 8, 23)
    assert prepared.shipments[0].service_dates == (date(2026, 8, 24), date(2026, 8, 26))
    assert prepared.calculation_manifest["target_entitlement_count"] == 2


def test_global_dinner_mapping_crosses_month_boundary_and_closure_remains():
    from src.domain.delivery_defaults import DELIVERY_DEFAULTS_ID, DELIVERY_DEFAULTS_KIND, delivery_defaults_state
    from src.domain.roster import standard_delivery_defaults
    snapshot, _ = _snapshot(meals=(Meal.DINNER,))
    states = dict(snapshot.entities)
    defaults = tuple(replace(item, weekdays=(1,), delivery_weekdays=(0,) * 7) if item.meal is Meal.DINNER else item for item in standard_delivery_defaults())
    states[EntityRef(DELIVERY_DEFAULTS_KIND, DELIVERY_DEFAULTS_ID)] = delivery_defaults_state(defaults)
    source = HistoricalSnapshot(snapshot.revision_id, states)
    prepared = prepare_delivery_report_data(source, date(2026, 8, 31), date(2026, 8, 31), (Meal.DINNER,))
    assert prepared.shipments[0].service_dates == (date(2026, 9, 1),)
    change = ServiceAdjustmentChange(uuid4(), InclusiveDateRange(date(2026, 8, 31), date(2026, 8, 31)), (Meal.DINNER,), ServiceTreatment.NOT_PROVIDED, None, (), "closure", datetime.now(UTC))
    states[EntityRef(SERVICE_ADJUSTMENT_SCHEDULE_KIND, SERVICE_ADJUSTMENT_SCHEDULE_ID)] = service_adjustment_schedule_state(ServiceAdjustmentSchedule((change,)))
    closed = prepare_delivery_report_data(HistoricalSnapshot(snapshot.revision_id, states), date(2026, 8, 31), date(2026, 8, 31), (Meal.DINNER,))
    assert not closed.shipments


@pytest.mark.parametrize('people,include_driver', [(2, True), (6, True), (2, False)])
def test_physical_delivery_preparation_skips_report_only_requirements(people, include_driver):
    from src.services.sqlite_delivery_report_service import prepare_physical_delivery_data

    snapshot, _ = _snapshot(people=people, include_driver=include_driver)
    day = date(2026, 8, 21)
    physical = prepare_physical_delivery_data(snapshot, day, day, (Meal.BREAKFAST,))
    assert physical.reports == ()
    assert len(physical.shipments) == people
    assert sum(s.entitlement_count for s in physical.shipments) == people * 3
    assert 'household_grouping' not in physical.calculation_manifest
    if people <= 5 and include_driver:
        report = prepare_delivery_report_data(snapshot, day, day, (Meal.BREAKFAST,))
        assert physical.shipments == report.shipments
        assert physical.calculation_manifest == {
            k: v for k, v in report.calculation_manifest.items()
            if k not in {'household_grouping', 'staff_order', 'report_count', 'selected_delivery_forms'}
        }
    else:
        with pytest.raises(ValueError):
            prepare_delivery_report_data(snapshot, day, day, (Meal.BREAKFAST,))


def test_physical_delivery_preparation_keeps_strict_school_calendar_failure():
    from src.services.sqlite_delivery_report_service import prepare_physical_delivery_data
    from src.domain.school_calendar import SchoolLunchCalendarError

    snapshot, _ = _snapshot(meals=(Meal.LUNCH,))
    with pytest.raises(SchoolLunchCalendarError):
        prepare_physical_delivery_data(snapshot, date(2026, 10, 19), date(2026, 10, 19), (Meal.LUNCH,))


@pytest.mark.parametrize('day', [date(2026, 8, 21), date(2027, 1, 1)])
def test_report_preserves_dated_roster_status_and_routing_transitions(day, monkeypatch):
    snapshot, person_ids = _snapshot(people=2, meals=(Meal.BREAKFAST, Meal.DINNER))
    roster = roster_from_historical_snapshot(snapshot)
    new_household = Household(uuid4())
    previous_day = day - timedelta(days=1)
    memberships = tuple(
        item for item in roster.memberships if item.person_id != person_ids[0]
    ) + (
        HouseholdMembership(person_ids[0], roster.households[0].household_id, InclusiveDateRange(None, previous_day)),
        HouseholdMembership(person_ids[0], new_household.household_id, InclusiveDateRange(day, None)),
    )
    enrollments = tuple(
        replace(item, period=InclusiveDateRange(day, None)) if item.person_id == person_ids[0] else item
        for item in roster.enrollments
    )
    changed = replace(roster, households=roster.households + (new_household,), memberships=memberships, enrollments=enrollments)
    states = dict(snapshot.entities)
    states.update(roster_entity_states(changed))
    paused = StatusEvent(uuid4(), snapshot.revision_id, person_ids[1], Meal.BREAKFAST,
                         MealStatus.PAUSED, InclusiveDateRange(day, day), datetime(2026, 1, 1, tzinfo=UTC))
    states[EntityRef('status.event', paused.event_id)] = status_event_state(paused)
    states[EntityRef(ROUTING_EVENT_KIND, uuid4())] = {
        'person_id': str(person_ids[0]), 'effective_on': day.isoformat(), 'position': 1,
        'driver_ids': {}, 'road_address': None, 'detail_address': None, 'jurisdiction': '판교동',
    }
    snapshot = HistoricalSnapshot(snapshot.revision_id, states)
    optimized = prepare_delivery_report_data(snapshot, day, day, (Meal.BREAKFAST, Meal.DINNER))
    assert optimized.shipments
    assert any('판교동' in row.reference_areas for report in optimized.reports for row in report.rows)

    def scan_meals(self, person_id, *, on):
        active = {e.meal for e in self.enrollments if e.person_id == person_id and e.period.contains(on)}
        return tuple(meal for meal in Meal if meal in active)

    def scan_household(self, person_id, *, on):
        household_id = next((m.household_id for m in self.memberships if m.person_id == person_id and m.period.contains(on)), None)
        return next((h for h in self.households if h.household_id == household_id), None)

    monkeypatch.setattr(RosterSnapshot, 'meals_for', scan_meals)
    monkeypatch.setattr(RosterSnapshot, 'household_for', scan_household)
    assert prepare_delivery_report_data(snapshot, day, day, (Meal.BREAKFAST, Meal.DINNER)) == optimized


def test_shipment_household_change_inside_bundled_weekend_remains_error():
    snapshot, person_ids = _snapshot()
    roster = roster_from_historical_snapshot(snapshot)
    friday = date(2026, 8, 21)
    new_household = Household(uuid4())
    changed = replace(roster, households=roster.households + (new_household,), memberships=(
        HouseholdMembership(person_ids[0], roster.households[0].household_id, InclusiveDateRange(None, friday)),
        HouseholdMembership(person_ids[0], new_household.household_id, InclusiveDateRange(friday + timedelta(days=1), None)),
    ))
    states = dict(snapshot.entities)
    states.update(roster_entity_states(changed))
    with pytest.raises(ValueError, match='서로 다른 가구'):
        prepare_delivery_report_data(HistoricalSnapshot(snapshot.revision_id, states), friday, friday, (Meal.BREAKFAST,))



def _with_routing(snapshot, *changes):
    states = dict(snapshot.entities)
    for position, (person_id, meal, effective_on, driver_name) in enumerate(changes, start=1):
        driver_id = None if driver_name is None else uuid4()
        if driver_id is not None:
            states[EntityRef(DRIVER_KIND, driver_id)] = {"name": driver_name, "telephone": ""}
        states[EntityRef(ROUTING_EVENT_KIND, uuid4())] = {
            "person_id": str(person_id),
            "effective_on": effective_on.isoformat(),
            "position": position,
            "driver_ids": {meal.value: None if driver_id is None else str(driver_id)},
            "road_address": None,
            "detail_address": None,
            "jurisdiction": None,
        }
    return HistoricalSnapshot(snapshot.revision_id, states)


@pytest.mark.parametrize("include_sequence", [True, False])
def test_report_accepts_registered_driver_without_legacy_driver(include_sequence):
    day = date(2026, 10, 23)
    snapshot, person_ids = _snapshot(include_driver=False)
    if not include_sequence:
        roster = roster_from_historical_snapshot(snapshot)
        states = dict(snapshot.entities)
        states.update(roster_entity_states(replace(roster, assignments=())))
        snapshot = HistoricalSnapshot(snapshot.revision_id, states)
    snapshot = _with_routing(snapshot, (person_ids[0], Meal.BREAKFAST, day, "새기사"))

    prepared = prepare_delivery_report_data(snapshot, day, day, (Meal.BREAKFAST,))

    report = prepared.reports[0]
    assert report.day == day
    assert report.rows[0].staff_label == "새기사\n(1)"
    assert report.rows[0].household_size_cells == ("1", "", "", "", "")
    assert report.totals.person_total == 1


@pytest.mark.parametrize("change_on", [date(2026, 8, 21), date(2026, 8, 22)])
def test_report_driver_change_uses_physical_delivery_day(change_on):
    friday = date(2026, 8, 21)
    snapshot, person_ids = _snapshot()
    snapshot = _with_routing(snapshot, (person_ids[0], Meal.BREAKFAST, change_on, "새기사"))

    prepared = prepare_delivery_report_data(snapshot, friday, friday, (Meal.BREAKFAST,))

    rows = {row.staff_label: row.household_size_cells for row in prepared.reports[0].rows}
    assert prepared.shipments[0].service_dates == (friday, friday + timedelta(days=1), friday + timedelta(days=2))
    if change_on == friday:
        assert rows == {"김기사\n(0)": ("0(-1)", "", "", "", ""),
                        "새기사\n(1)": ("1(+1)", "", "", "", "")}
    else:
        assert rows == {"김기사\n(1)": ("1", "", "", "", "")}


def test_report_driver_clear_rejects_selected_physical_day():
    day = date(2026, 10, 23)
    snapshot, person_ids = _snapshot()
    snapshot = _with_routing(snapshot, (person_ids[0], Meal.BREAKFAST, day, None))

    with pytest.raises(ValueError, match=r"2026\.10\.23 조식.*배송담당이 비어 있습니다"):
        prepare_delivery_report_data(snapshot, day, day, (Meal.BREAKFAST,))


def test_report_missing_driver_error_uses_selected_day_before_comparison():
    day = date(2026, 10, 23)
    snapshot, _ = _snapshot(include_driver=False)

    with pytest.raises(ValueError, match=r"2026\.10\.23 조식.*배송담당이 비어 있습니다"):
        prepare_delivery_report_data(snapshot, day, day, (Meal.BREAKFAST,))


def test_incomplete_previous_driver_discards_entire_comparison_without_false_deltas():
    day = date(2026, 10, 23)
    previous = day - timedelta(days=1)
    snapshot, person_ids = _snapshot(people=2, delivery_settings=(
        HouseholdDeliverySetting(Meal.BREAKFAST, (0, 1, 2, 3, 4)),
    ))
    snapshot = _with_routing(
        snapshot,
        (person_ids[0], Meal.BREAKFAST, previous, None),
        (person_ids[0], Meal.BREAKFAST, day, "새기사"),
    )

    prepared = prepare_delivery_report_data(snapshot, day, day, (Meal.BREAKFAST,))

    rows = {row.staff_label: row.household_size_cells for row in prepared.reports[0].rows}
    assert rows == {"김기사\n(1)": ("1", "", "", "", ""),
                    "새기사\n(1)": ("1", "", "", "", "")}
    assert prepared.reports[0].totals.person_total == 2


def test_previous_day_inside_selected_period_still_requires_driver():
    day = date(2026, 10, 23)
    previous = day - timedelta(days=1)
    snapshot, person_ids = _snapshot(delivery_settings=(
        HouseholdDeliverySetting(Meal.BREAKFAST, (0, 1, 2, 3, 4)),
    ))
    snapshot = _with_routing(
        snapshot,
        (person_ids[0], Meal.BREAKFAST, previous, None),
        (person_ids[0], Meal.BREAKFAST, day, "새기사"),
    )

    with pytest.raises(ValueError, match=r"2026\.10\.22 조식.*배송담당이 비어 있습니다"):
        prepare_delivery_report_data(snapshot, previous, day, (Meal.BREAKFAST,))
