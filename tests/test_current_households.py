"""Household dates are audit details; current criteria control operations."""

from dataclasses import replace
from datetime import UTC, date, datetime
from uuid import uuid4

import pytest

from src.domain.household_grouping import build_grouping_revision
from src.domain.household_shared import HOUSEHOLD_SHARED_ID, HOUSEHOLD_SHARED_KIND
from src.domain.mobile_household_merge import MobileHouseholdConflict, with_mobile_shared_values
from src.domain.profile_edits import build_person_profile_revision
from src.domain.projections import roster_audit_changes, roster_from_historical_snapshot
from src.domain.revisions import AuditChange, ChangeOperation, RevisionActor, RevisionMetadata, RevisionRecord, RevisionSource
from src.domain.roster import DeliveryAssignment, Household, HouseholdMembership, InclusiveDateRange, Meal, MealEnrollment, Person, PersonProfile, RosterSnapshot
from src.domain.delivery_routing import RoutingState
from src.services.delivery_households import require_delivery_household
from src.services.driver_households import build_driver_household_rows
from src.services.sqlite_document_service import _household_for_document
from src.storage.database_session import DatabaseSession


DAY = date(2026, 11, 2)
NOW = datetime(2026, 11, 2, tzinfo=UTC)
ACTOR = RevisionActor("합성 담당", "current-household-test")


def test_edit_into_legacy_mixed_household_only_joins_matching_member(tmp_path):
    roster = fixture_roster()
    first, second, third = roster.persons
    # Legacy target links second and third despite their different phone numbers.
    roster = replace(roster, memberships=tuple(
        replace(m, household_id=roster.memberships[1].household_id)
        if m.person_id == third.person_id else m for m in roster.memberships))
    with DatabaseSession.create(tmp_path / "mixed-target.db", "password") as session:
        initialize(session, roster)
        session.append_revision(edit(session, first.person_id, mobile_phone="010 1234 5678"))
        current = session.database.current_roster()
        assert current.current_membership(first.person_id).household_id == current.current_membership(second.person_id).household_id
        assert current.current_membership(third.person_id).household_id != current.current_membership(first.person_id).household_id
        assert current.person(third.person_id) == third
        session.database.check_integrity()


def fixture_roster(*, missing=False, shared=False):
    people = tuple(Person(uuid4(), PersonProfile(name, mobile_phone=phone, road_address=address,
                                                note=name)) for name, phone, address in (
        ("첫째", "010-1234-5678", "주소 A"), ("둘째", "010 1234 5678", "주소 A"),
        ("다른 가구", "01099999999", "주소 B"),
    ))
    households = tuple(Household(uuid4()) for _ in people)
    return RosterSnapshot(uuid4(), persons=people, households=() if missing else households,
        memberships=() if missing else tuple(HouseholdMembership(p.person_id,
            households[0 if shared and i == 1 else i].household_id,
            InclusiveDateRange(date(2026, 12, 1), date(2026, 12, 31))) for i, p in enumerate(people)),
        enrollments=tuple(MealEnrollment(p.person_id, Meal.DINNER, InclusiveDateRange(DAY)) for p in people),
        assignments=tuple(DeliveryAssignment(p.person_id, Meal.DINNER, driver=f"기사 {i}", sequence=str(i + 1)) for i, p in enumerate(people)))


def initialize(session, roster, *, shared_columns=()):
    changes = list(roster_audit_changes(None, roster))
    if shared_columns:
        changes.append(AuditChange(HOUSEHOLD_SHARED_KIND, HOUSEHOLD_SHARED_ID, ChangeOperation.INSERT,
                                   None, {"columns": list(shared_columns)}))
    session.append_revision(RevisionRecord(
        RevisionMetadata(roster.revision_id, None, NOW, DAY, ACTOR, "합성 명단", RevisionSource.IMPORT), tuple(changes)))


def regroup(session, columns=("mobile",)):
    roster = session.database.current_roster()
    record = build_grouping_revision(session.database.snapshot(roster.revision_id), roster, columns, DAY,
                                    actor=ACTOR, recorded_at=NOW, revision_id=uuid4())
    result = session.append_revision(record)
    assert result.error is None
    return session.database.current_roster()


def edit(session, pid, **fields):
    roster = session.database.current_roster()
    return build_person_profile_revision(roster, pid, replace(roster.person(pid).profile, **fields),
        actor=ACTOR, recorded_at=NOW, effective_on=DAY, revision_id=uuid4()).record


@pytest.mark.parametrize("missing", [False, True])
def test_household_bounds_and_missing_links_never_block_operations(missing):
    roster = fixture_roster(missing=missing)
    pid = roster.persons[0].person_id
    expected = roster.current_membership(pid).household_id
    for day in (date(2000, 1, 1), DAY, date(2100, 1, 1)):
        assert roster.household_for(pid, on=day).household_id == expected
        assert require_delivery_household(roster, pid, (day, DAY)) == expected
        assert _household_for_document(roster, pid, (day,), effective_on=DAY) == expected
        assert roster.person(pid) in roster.household_members(expected, on=day)
    # Meal enrollment dates still restrict meals; only household dates are ignored.
    assert roster.meals_for(pid, on=date(2000, 1, 1)) == ()
    assert roster.meals_for(pid, on=DAY) == (Meal.DINNER,)
    assert len(build_driver_household_rows(roster, RoutingState(), DAY)) == 3


@pytest.mark.parametrize("missing", [False, True])
def test_grouping_is_immediate_and_old_revision_and_backup_remain_exact(tmp_path, missing):
    roster = fixture_roster(missing=missing)
    first, second, third = (p.person_id for p in roster.persons)
    backups = tmp_path / "backups"
    backups.mkdir()
    with DatabaseSession.create(tmp_path / "groups.sqlite3", "password", backup_directory=backups) as session:
        initialize(session, roster)
        updated = regroup(session)
        for day in (date(2000, 1, 1), DAY, date(2100, 1, 1)):
            assert updated.household_for(first, on=day) == updated.household_for(second, on=day)
            assert updated.household_for(first, on=day) != updated.household_for(third, on=day)
        historical = roster_from_historical_snapshot(session.database.snapshot(roster.revision_id))
        assert historical.household_for(first, on=DAY) != historical.household_for(second, on=DAY)
        assert set(historical.memberships) == set(roster.memberships)
        assert set(updated.assignments) == set(roster.assignments)
        assert set(updated.enrollments) == set(roster.enrollments)
        session.database.check_integrity()
    with DatabaseSession.open(tmp_path / "groups.sqlite3", "password") as session:
        assert session.database.current_roster() == updated


@pytest.mark.parametrize("phone", ["01055555555", "", None])
def test_changed_or_cleared_key_splits_even_when_phone_is_shared(tmp_path, phone):
    roster = fixture_roster(shared=True)
    first, second, third = (p.person_id for p in roster.persons)
    with DatabaseSession.create(tmp_path / "split.sqlite3", "password") as session:
        initialize(session, roster, shared_columns=("mobile",))
        record = edit(session, first, mobile_phone=phone)
        session.append_revision(record)
        updated = session.database.current_roster()
        assert updated.household_for(first, on=DAY) != updated.household_for(second, on=DAY)
        assert updated.person(second).profile.mobile_phone == roster.person(second).profile.mobile_phone
        assert set(updated.assignments) == set(roster.assignments)


def test_multiple_keys_split_on_mismatch_and_merge_on_match(tmp_path):
    roster = fixture_roster(shared=True)
    first, second, _ = (p.person_id for p in roster.persons)
    with DatabaseSession.create(tmp_path / "columns.sqlite3", "password") as session:
        initialize(session, roster)
        regroup(session, ("mobile", "address"))
        session.append_revision(edit(session, first, road_address="새 주소"))
        changed = session.database.current_roster()
        assert changed.household_for(first, on=DAY) != changed.household_for(second, on=DAY)
        session.append_revision(edit(session, second, road_address="새 주소"))
        changed = session.database.current_roster()
        assert changed.household_for(first, on=DAY) == changed.household_for(second, on=DAY)


def test_split_and_new_merge_require_shared_conflict_review_atomically(tmp_path):
    roster = fixture_roster(shared=True)
    first, second, third = (p.person_id for p in roster.persons)
    with DatabaseSession.create(tmp_path / "review.sqlite3", "password") as session:
        initialize(session, roster, shared_columns=("note",))
        record = edit(session, first, mobile_phone="01099999999")
        with pytest.raises(MobileHouseholdConflict) as caught:
            session.append_revision(record)
        assert session.database.latest_revision_id() == roster.revision_id
        choices = {hid: ("확인한 공용 값",) for hid in caught.value.household_ids}
        session.append_revision(with_mobile_shared_values(record, "note", choices))
        changed = session.database.current_roster()
        assert changed.household_for(first, on=DAY) == changed.household_for(third, on=DAY)
        assert changed.household_for(first, on=DAY) != changed.household_for(second, on=DAY)
        assert changed.person(first).profile.note == changed.person(third).profile.note == "확인한 공용 값"
        assert changed.person(second).profile.note == roster.person(second).profile.note
