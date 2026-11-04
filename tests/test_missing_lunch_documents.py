from dataclasses import replace
from datetime import date

import pytest

from src.domain.projections import roster_entity_states, roster_from_historical_snapshot
from src.domain.revisions import HistoricalSnapshot
from src.domain.roster import Meal
from src.domain.school_calendar import SCHOOL_LUNCH_SCHEDULE_KIND
from src.services.sqlite_delivery_report_service import prepare_delivery_report_data, prepare_physical_delivery_data
from src.services.sqlite_document_service import prepare_monthly_document_data
from src.services.happy_delivery_report import prepare_happy_report_data
from src.services.sqlite_delivery_substitute_service import prepare_delivery_substitute_data
from src.services.lunch_schedule_exclusions import lunch_schedule_exclusion_lines
from tests.test_sqlite_delivery_report_service import _snapshot


@pytest.mark.parametrize("kind", ["daily", "happy", "monthly", "physical", "substitute"])
def test_missing_school_excludes_its_lunch_and_keeps_other_school_and_meals(kind):
    snapshot, ids = _snapshot(people=2, meals=tuple(Meal))
    roster = roster_from_historical_snapshot(snapshot)
    changed = replace(roster, persons=tuple(replace(person, profile=replace(person.profile,
        school_name="누락학교")) if person.person_id == ids[1] else person for person in roster.persons))
    snapshot = HistoricalSnapshot(snapshot.revision_id, {**snapshot.entities, **roster_entity_states(changed)})
    first, last = date(2026, 8, 17), date(2026, 8, 21)
    if kind == "substitute":
        result = prepare_delivery_substitute_data(snapshot, last, Meal.LUNCH)
        assert {row.person_id for row in result.rows} == {ids[0]}
    elif kind == "monthly":
        result = prepare_monthly_document_data(snapshot, first, last, tuple(Meal))
        assert {item.person_id for item in result.entitlements if item.meal is Meal.LUNCH} == {ids[0]}
        assert any(item.person_id == ids[1] and item.meal is Meal.DINNER for item in result.entitlements)
    else:
        prepare = {"daily": prepare_delivery_report_data, "happy": prepare_happy_report_data,
                   "physical": prepare_physical_delivery_data}[kind]
        result = prepare(snapshot, first, last, tuple(Meal))
        assert {item.person_id for item in result.shipments if item.meal is Meal.LUNCH} == {ids[0]}
        assert any(item.person_id == ids[1] and item.meal is Meal.DINNER for item in result.shipments)
    lines = lunch_schedule_exclusion_lines(result.calculation_manifest)
    assert len(lines) == 1 and lines[0].startswith("누락학교 · 1명")
    assert "2026-08-21" in lines[0]
    assert "2026-07" not in lines[0]  # comparison-only dates never appear as output omissions


def test_partial_calendar_keeps_known_days_and_invalid_calendar_still_fails():
    snapshot, ids = _snapshot(meals=(Meal.LUNCH,))
    states = dict(snapshot.entities)
    ref = next(ref for ref in states if ref.entity_kind == SCHOOL_LUNCH_SCHEDULE_KIND)
    original = states[ref]
    states[ref] = {**original, "covered_periods": [{"starts_on": "2026-08-21", "ends_on": "2026-08-21"}],
                   "days": [day for day in original["days"] if day["service_on"] == "2026-08-21"]}
    result = prepare_physical_delivery_data(HistoricalSnapshot(snapshot.revision_id, states),
        date(2026, 8, 17), date(2026, 8, 21), (Meal.LUNCH,))
    assert [day for item in result.shipments for day in item.service_dates] == [date(2026, 8, 21)]
    assert result.calculation_manifest["missing_lunch_schedules"][0]["service_dates"] == (
        "2026-08-17", "2026-08-18", "2026-08-19", "2026-08-20")
    states[ref] = {**original, "days": []}
    with pytest.raises(ValueError, match="평일 범위"):
        prepare_delivery_report_data(HistoricalSnapshot(snapshot.revision_id, states), date(2026, 8, 21), date(2026, 8, 21), (Meal.LUNCH,))
