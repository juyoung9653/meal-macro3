"""Synthetic integration coverage for the legacy builder.py/memo.py port."""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import openpyxl
import pytest

from src.domain.projections import roster_entity_states, status_event_state
from src.domain.delivery_routing import DRIVER_KIND, ROUTING_EVENT_KIND
from src.domain.revisions import EntityRef, HistoricalSnapshot
from src.domain.roster import (
    DeliveryAssignment, Household, HouseholdDeliverySetting, HouseholdMembership,
    InclusiveDateRange, Meal, MealEnrollment, Person, PersonProfile, RosterSnapshot,
)
from src.domain.status import MealStatus, StatusEvent
from src.generators.report_generator import make_report_publication
from src.services.delivery_report_remarks import remark_sample_dates
from src.services.sqlite_delivery_report_service import prepare_delivery_report_data


MONDAY = date(2026, 8, 17)


def source(*, count=2, starts=None, statuses=(), forms=None, missing_driver=()):
    revision = uuid4()
    ids = tuple(uuid4() for _ in range(count))
    households = tuple(uuid4() for _ in ids)
    starts = starts or {}
    forms = forms or {}
    roster = RosterSnapshot(
        revision,
        persons=tuple(Person(
            person_id,
            PersonProfile(f"아동{index}", jurisdiction="정자동"),
            (HouseholdDeliverySetting(Meal.DINNER, form=forms[index]),)
            if index in forms else None,
        ) for index, person_id in enumerate(ids)),
        households=tuple(Household(item) for item in households),
        memberships=tuple(HouseholdMembership(person_id, household_id, InclusiveDateRange())
                          for person_id, household_id in zip(ids, households)),
        enrollments=tuple(MealEnrollment(
            person_id, Meal.DINNER, InclusiveDateRange(starts.get(index, date(2025, 1, 1)), None),
        ) for index, person_id in enumerate(ids)),
        assignments=tuple(DeliveryAssignment(
            person_id, None, None if index in missing_driver else "김기사", str(index),
        ) for index, person_id in enumerate(ids)),
    )
    events = tuple(StatusEvent(
        uuid4(), revision, ids[index], meal, status, InclusiveDateRange(start, end),
        datetime(2026, 8, 1, tzinfo=UTC) + timedelta(seconds=position),
        "신규 1/1~; 일시중지 8/1~8/31",  # Deliberately stale text must be ignored.
    ) for position, (index, meal, status, start, end) in enumerate(statuses))
    return snapshot(roster, events), roster, events


def snapshot(roster, events):
    states = dict(roster_entity_states(roster))
    states.update({EntityRef("status.event", event.event_id): status_event_state(event) for event in events})
    return HistoricalSnapshot(roster.revision_id, states)


def prepare(source_snapshot, day=MONDAY, **kwargs):
    return prepare_delivery_report_data(source_snapshot, day, day, (Meal.DINNER,), **kwargs)


def memo(data):
    return "\n".join(row.memo for row in data.reports[0].rows)


def test_new_uses_meal_start_and_propagates_only_in_legacy_week_window():
    historical, _, _ = source(starts={1: MONDAY})
    assert memo(prepare(historical)) == "신규 8/17~\n정자동(아동1)"
    assert "신규 8/17~" in memo(prepare(historical, MONDAY + timedelta(days=3)))
    assert memo(prepare(historical, MONDAY + timedelta(days=7))) == ""


@pytest.mark.parametrize("status,end,label", [
    (MealStatus.STOPPED, None, "중지 8/17~"),
    (MealStatus.PAUSED, MONDAY, "일시중지 8/17"),
    (MealStatus.PAUSED, MONDAY + timedelta(days=2), "일시중지 8/17~8/19"),
])
def test_nondelivered_status_overrides_initial_new_and_keeps_counts(status, end, label):
    historical, _, _ = source(starts={1: MONDAY}, statuses=((1, Meal.DINNER, status, MONDAY, end),))
    prepared = prepare(historical)
    assert memo(prepared) == f"{label}\n정자동(아동1)"
    assert prepared.reports[0].totals.person_total == len(prepared.shipments) == 1


def test_pause_expiry_restores_but_explicit_restart_uses_effective_delivery():
    historical, _, _ = source(statuses=((1, Meal.DINNER, MealStatus.PAUSED, MONDAY, MONDAY),))
    assert memo(prepare(historical, MONDAY + timedelta(days=1))) == "복원\n정자동(아동1)"
    historical, _, _ = source(statuses=(
        (1, Meal.DINNER, MealStatus.STOPPED, MONDAY, None),
        (1, Meal.DINNER, MealStatus.DELIVERY, MONDAY + timedelta(days=1), None),
    ))
    assert memo(prepare(historical)) == "일시중지 8/17\n정자동(아동1)"
    assert memo(prepare(historical, MONDAY + timedelta(days=1))) == "배송 8/18~\n정자동(아동1)"


def test_pause_expiry_over_prior_delivery_interval_is_automatic_restore():
    historical, _, _ = source(statuses=(
        (1, Meal.DINNER, MealStatus.DELIVERY, date(2026, 1, 1), None),
        (1, Meal.DINNER, MealStatus.PAUSED, MONDAY, MONDAY),
    ))
    assert memo(prepare(historical, MONDAY + timedelta(days=1))) == "복원\n정자동(아동1)"


def test_memo_groups_under_target_date_driver_and_address():
    historical, roster, _ = source(statuses=((1, Meal.DINNER, MealStatus.PAUSED, MONDAY, MONDAY),))
    driver_id = uuid4()
    states = dict(historical.entities)
    states[EntityRef(DRIVER_KIND, driver_id)] = {"name": "새기사", "telephone": ""}
    states[EntityRef(ROUTING_EVENT_KIND, uuid4())] = {
        "person_id": str(roster.persons[1].person_id), "effective_on": MONDAY.isoformat(),
        "position": 1, "driver_ids": {Meal.DINNER.value: str(driver_id)},
        "road_address": "위례대로", "detail_address": "333동 444호", "jurisdiction": "위례동",
    }
    prepared = prepare(HistoricalSnapshot(historical.revision_id, states))
    row = next(row for row in prepared.reports[0].rows if row.staff_label == "새기사\n(0)")
    assert row.memo == "일시중지 8/17\n333동 444호(아동1)"
    assert row.person_total == 0


def test_overwritten_events_and_other_meals_do_not_leak_into_remarks():
    historical, _, _ = source(statuses=(
        (1, Meal.DINNER, MealStatus.PAUSED, MONDAY, MONDAY + timedelta(days=3)),
        (1, Meal.DINNER, MealStatus.DELIVERY, MONDAY, None),
        (1, Meal.BREAKFAST, MealStatus.STOPPED, MONDAY, None),
    ))
    prepared = prepare(historical)
    assert memo(prepared) == "배송 8/17~\n정자동(아동1)"
    assert prepared.reports[0].totals.person_total == 2


def test_memo_only_missing_driver_has_zero_counts_and_target_missing_driver_still_fails():
    historical, _, _ = source(missing_driver=(1,), statuses=(
        (1, Meal.DINNER, MealStatus.STOPPED, MONDAY, None),
    ))
    prepared = prepare(historical)
    assert prepared.reports[0].rows[-1].staff_label == "미지정\n(0)"
    assert prepared.reports[0].rows[-1].person_total == 0
    assert "중지 8/17~" in prepared.reports[0].rows[-1].memo
    invalid, _, _ = source(missing_driver=(1,))
    with pytest.raises(ValueError, match="배송담당이 비어"):
        prepare(invalid)


def test_form_summaries_counts_and_filter_are_preserved_with_status_memos():
    historical, _, _ = source(starts={0: MONDAY, 1: MONDAY}, forms={1: "박스"})
    all_forms = prepare(historical)
    assert memo(all_forms) == "신규 8/17~\n정자동(아동0, 아동1)\n기본 1가구·1식\n박스 1가구·1식"
    selected = prepare(historical, delivery_forms=(None,))
    assert memo(selected) == "신규 8/17~\n정자동(아동0)"
    assert selected.reports[0].totals.person_total == len(selected.shipments) == 1


def test_status_heading_keeps_red_emphasis_with_one_form_summary(tmp_path):
    historical, _, _ = source(count=1, starts={0: MONDAY}, forms={0: "박스"})
    prepared = prepare(historical)
    output = tmp_path / "report-colors.xlsx"
    make_report_publication(prepared.reports, output, "assets/report_template.xlsx")
    workbook = openpyxl.load_workbook(output, rich_text=True)
    try:
        value = workbook.active["H4"].value
        assert str(value) == "신규 8/17~\n정자동(아동0)\n박스 1가구·1식"
        assert value[0].font.color.rgb == "FFFF0000"
        assert value[1].font.color.rgb == "FF000000"
    finally:
        workbook.close()


def test_area_name_sorting_and_wirye_address_grouping(tmp_path):
    historical, roster, events = source(count=4, starts={0: MONDAY, 1: MONDAY, 2: MONDAY, 3: MONDAY})
    profiles = (
        PersonProfile("나", jurisdiction="위례동", road_address="대로 100", detail_address="1234-5678"),
        PersonProfile("가", jurisdiction="위례동", road_address="대로 100", detail_address="1234-5678"),
        PersonProfile("다", jurisdiction="위례동", road_address="대로", detail_address="101동 202호"),
        PersonProfile("라", jurisdiction="정자동"),
    )
    roster = replace(roster, persons=tuple(replace(person, profile=profile) for person, profile in zip(roster.persons, profiles)))
    prepared = prepare(snapshot(roster, events))
    expected = "신규 8/17~\n101동 202호(다), 1234-5678(가, 나), 정자동(라)"
    assert memo(prepared) == expected
    output = tmp_path / "report.xlsx"
    make_report_publication(prepared.reports, output, "assets/report_template.xlsx")
    workbook = openpyxl.load_workbook(output)
    try:
        assert workbook.active["H4"].value == expected
    finally:
        workbook.close()


def test_sampling_crosses_year_and_keeps_breakfast_special_and_22_day_limit():
    day = date(2027, 1, 7)
    active = {date(2026, 12, 31), date(2027, 1, 4), day}
    assert remark_sample_dates(day, Meal.DINNER, active) == tuple(sorted(active))
    assert remark_sample_dates(day, Meal.BREAKFAST, active) == (date(2026, 12, 31), day)
    monday = day - timedelta(days=day.weekday())
    assert remark_sample_dates(day, Meal.DINNER, {monday - timedelta(days=24), day}) == (day,)
    historical, _, _ = source(starts={1: date(2026, 12, 31)})
    prepared = prepare(historical, date(2027, 1, 4))
    assert memo(prepared) == "신규 12/31~\n정자동(아동1)"
    assert prepared.reports[0].totals.person_total == len(prepared.shipments) == 2


def test_no_new_report_dates_are_created_for_stopped_people():
    historical, _, _ = source(count=1, statuses=((0, Meal.DINNER, MealStatus.STOPPED, MONDAY, None),))
    assert prepare(historical).reports == ()
