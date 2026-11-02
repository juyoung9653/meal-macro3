"""Monthly output groups at the document date while retaining dated meal facts."""
from dataclasses import replace
from datetime import date
from uuid import uuid4

import pytest

from src.domain.projections import roster_entity_states, roster_from_historical_snapshot
from src.domain.revisions import HistoricalSnapshot
from src.domain.roster import Household, HouseholdMembership, InclusiveDateRange, Meal
from src.generators.confirm_generator import make_confirm_publication
from src.services.sqlite_document_service import prepare_monthly_document_data
from tests.test_sqlite_document_service import _source_snapshot


def _with_roster(source, roster):
    states = dict(source.entities)
    states.update(roster_entity_states(roster))
    return HistoricalSnapshot(uuid4(), states)


@pytest.mark.parametrize("end_day", [15, 16, 31])
@pytest.mark.parametrize("returns_to_original", [False, True])
def test_midmonth_household_change_preserves_meals_prices_and_old_source(end_day, returns_to_original):
    source = _source_snapshot()
    roster = roster_from_historical_snapshot(source)
    original = roster.memberships[0]
    other, future = Household(uuid4()), Household(uuid4())
    memberships = (
        replace(original, period=InclusiveDateRange(None, date(2026, 8, 15))),
        replace(original, household_id=other.household_id,
                period=InclusiveDateRange(date(2026, 8, 16), date(2026, 8, 16) if returns_to_original else date(2026, 8, 31))),
        *((replace(original, period=InclusiveDateRange(date(2026, 8, 17), date(2026, 8, 31))),) if returns_to_original else ()),
        replace(original, household_id=future.household_id,
                period=InclusiveDateRange(date(2026, 9, 1), None)),
    )
    changed = _with_roster(source, replace(roster, memberships=memberships,
                                          households=(*roster.households, other, future)))
    start, end = date(2026, 8, 1), date(2026, 8, end_day)
    baseline = prepare_monthly_document_data(source, start, end, tuple(Meal))
    current = prepare_monthly_document_data(changed, start, end, tuple(Meal))
    expected = original.household_id if end_day < 16 or (returns_to_original and end_day > 16) else other.household_id
    assert current.batch.deliveries[0].household_key == str(expected)
    before, after = baseline.batch.deliveries[0], current.batch.deliveries[0]
    assert after.roster.extra == before.roster.extra
    assert replace(after, roster=before.roster) == replace(before, household_key=str(expected))
    assert current.entitlements == baseline.entitlements
    assert current.billing == baseline.billing
    assert roster_from_historical_snapshot(changed).memberships == memberships
    old_again = prepare_monthly_document_data(source, start, end, tuple(Meal))
    assert old_again.entitlements == baseline.entitlements
    assert old_again.billing == baseline.billing
    assert old_again.calculation_manifest == baseline.calculation_manifest
    assert old_again.batch.deliveries[0].household_key == str(original.household_id)


def test_month_end_household_wins_even_after_last_meal_and_confirmation_merges(tmp_path):
    source = _source_snapshot()
    roster = roster_from_historical_snapshot(source)
    person = roster.persons[0]
    original = roster.memberships[0]
    target = Household(uuid4())
    sibling = replace(person, person_id=uuid4(), profile=replace(person.profile, name="홍둘째"))
    # Both stop receiving meals before the move; output still groups at month end.
    enrollments = tuple(replace(e, period=InclusiveDateRange(None, date(2026, 8, 10)))
                        for e in roster.enrollments)
    changed = replace(roster, persons=(*roster.persons, sibling),
                      households=(*roster.households, target),
                      memberships=(
                          replace(original, period=InclusiveDateRange(None, date(2026, 8, 15))),
                          replace(original, household_id=target.household_id, period=InclusiveDateRange(date(2026, 8, 16), None)),
                          HouseholdMembership(sibling.person_id, target.household_id),
                      ),
                      enrollments=(*enrollments, *(replace(e, person_id=sibling.person_id) for e in enrollments)))
    prepared = prepare_monthly_document_data(_with_roster(source, changed), date(2026, 8, 1), date(2026, 8, 31), (Meal.BREAKFAST,))
    assert len(prepared.batch.deliveries) == 2
    assert {d.household_key for d in prepared.batch.deliveries} == {str(target.household_id)}
    assert all(e.service_on <= date(2026, 8, 10) for e in prepared.entitlements)
    created, publication = make_confirm_publication(
        prepared.batch, {}, tmp_path / "month-end-household.xlsx",
        "assets/급식/급식_확인서템플릿.xlsx",
    )
    assert publication.output_path.exists()
    assert created[0]["total_hh"] == 1
    assert created[0]["count_2"] == 1


def test_month_end_gap_uses_latest_included_service_household_not_future():
    source = _source_snapshot()
    roster = roster_from_historical_snapshot(source)
    original = roster.memberships[0]
    other, future = Household(uuid4()), Household(uuid4())
    changed = replace(roster, households=(*roster.households, other, future), memberships=(
        replace(original, period=InclusiveDateRange(None, date(2026, 8, 15))),
        replace(original, household_id=other.household_id,
                period=InclusiveDateRange(date(2026, 8, 16), date(2026, 8, 20))),
        replace(original, household_id=future.household_id,
                period=InclusiveDateRange(date(2026, 9, 1), None)),
    ))
    prepared = prepare_monthly_document_data(_with_roster(source, changed),
                                             date(2026, 8, 1), date(2026, 8, 31), (Meal.DINNER,))
    baseline = prepare_monthly_document_data(source, date(2026, 8, 1), date(2026, 8, 31), (Meal.DINNER,))
    assert prepared.batch.deliveries[0].household_key == str(other.household_id)
    assert prepared.entitlements == baseline.entitlements
    assert prepared.billing == baseline.billing


def test_bundled_next_month_service_does_not_override_document_date_household():
    source = _source_snapshot()
    roster = roster_from_historical_snapshot(source)
    original = roster.memberships[0]
    other = Household(uuid4())
    changed = replace(roster, households=(*roster.households, other), memberships=(
        replace(original, period=InclusiveDateRange(None, date(2026, 10, 31))),
        replace(original, household_id=other.household_id, period=InclusiveDateRange(date(2026, 11, 1), None)),
    ))
    prepared = prepare_monthly_document_data(_with_roster(source, changed),
                                             date(2026, 10, 1), date(2026, 10, 31), (Meal.BREAKFAST,))
    assert any(e.service_on == date(2026, 11, 1) for e in prepared.entitlements)
    assert prepared.batch.deliveries[0].household_key == str(original.household_id)



def test_document_grouping_does_not_replace_historical_household_weekdays():
    from src.domain.roster import HouseholdDeliverySetting

    source = _source_snapshot(delivery_settings=(HouseholdDeliverySetting(Meal.DINNER, (0,)),))
    roster = roster_from_historical_snapshot(source)
    original = roster.memberships[0]
    tuesdays = Household(uuid4(), (HouseholdDeliverySetting(Meal.DINNER, (1,)),))
    changed = replace(roster, households=(*roster.households, tuesdays), memberships=(
        replace(original, period=InclusiveDateRange(None, date(2026, 8, 15))),
        replace(original, household_id=tuesdays.household_id, period=InclusiveDateRange(date(2026, 8, 16), None)),
    ))
    prepared = prepare_monthly_document_data(
        _with_roster(source, changed), date(2026, 8, 1), date(2026, 8, 31), (Meal.DINNER,),
    )
    # The old household provided Mondays, the new one Tuesdays; Aug 10 was paused.
    assert tuple(e.service_on for e in prepared.entitlements) == (
        date(2026, 8, 3), date(2026, 8, 18), date(2026, 8, 25),
    )
    assert prepared.billing.total_amount == 30_000
    assert prepared.batch.deliveries[0].household_key == str(tuesdays.household_id)
