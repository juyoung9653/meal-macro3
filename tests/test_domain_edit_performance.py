"""Bounded profile-edit projection and identity-preservation contracts."""

from dataclasses import replace
from datetime import UTC, date, datetime
from uuid import UUID, uuid4

import pytest

from src.domain import projections
from src.domain.profile_edits import build_person_profile_revision
from src.domain.revisions import RevisionActor
from src.domain.roster import (
    Household,
    HouseholdDeliverySetting,
    HouseholdMembership,
    Meal,
    Person,
    PersonProfile,
    RosterSnapshot,
)

NOW = datetime(2026, 10, 21, 12, tzinfo=UTC)
TODAY = date(2026, 10, 21)
ACTOR = RevisionActor("performance-test", "test")


def _roster(size: int = 100) -> RosterSnapshot:
    household_id = UUID(int=10_000)
    persons = tuple(
        Person(UUID(int=index + 1), PersonProfile(f"사람 {index}", mobile_phone="010-0000-0000"))
        for index in range(size)
    )
    return RosterSnapshot(
        uuid4(),
        persons=persons,
        households=(Household(household_id),),
        memberships=tuple(
            HouseholdMembership(person.person_id, household_id) for person in persons
        ),
    )


def test_single_profile_edit_projects_only_the_changed_person(monkeypatch):
    roster = _roster()
    subject = roster.persons[0]
    person_state_calls = 0
    household_state_calls = 0
    original_person_state = projections._person_state
    original_household_state = projections._household_state

    def person_state(person):
        nonlocal person_state_calls
        person_state_calls += 1
        return original_person_state(person)

    def household_state(household):
        nonlocal household_state_calls
        household_state_calls += 1
        return original_household_state(household)

    monkeypatch.setattr(projections, "_person_state", person_state)
    monkeypatch.setattr(projections, "_household_state", household_state)
    prepared = build_person_profile_revision(
        roster,
        subject.person_id,
        replace(subject.profile, note="한 칸 수정"),
        actor=ACTOR,
        recorded_at=NOW,
        effective_on=TODAY,
        revision_id=uuid4(),
    )

    assert (person_state_calls, household_state_calls) == (2, 0)
    assert prepared.snapshot.person(subject.person_id) is not subject
    assert all(
        prepared.snapshot.person(person.person_id) is person
        for person in roster.persons[1:]
    )
    assert prepared.record.changes == projections.roster_audit_changes(
        roster, prepared.snapshot
    )


def test_shared_phone_reuses_members_whose_effective_value_is_unchanged():
    roster = _roster(3)
    subject, same_phone, changed_phone = roster.persons
    roster = replace(
        roster,
        persons=(
            subject,
            same_phone,
            replace(
                changed_phone,
                profile=replace(changed_phone.profile, mobile_phone="010-9999-9999"),
            ),
        ),
    )
    changed_phone = roster.persons[2]

    prepared = build_person_profile_revision(
        roster,
        subject.person_id,
        subject.profile,
        actor=ACTOR,
        recorded_at=NOW,
        effective_on=TODAY,
        revision_id=uuid4(),
        household_mobile_phone=True,
    )

    assert prepared.snapshot.person(subject.person_id) is subject
    assert prepared.snapshot.person(same_phone.person_id) is same_phone
    assert prepared.snapshot.person(changed_phone.person_id) is not changed_phone
    assert prepared.record.changes == projections.roster_audit_changes(
        roster, prepared.snapshot
    )


def test_mixed_profile_and_household_change_keeps_global_audit_order_and_freezes_states():
    roster = _roster(2)
    subject = roster.persons[0]
    setting = HouseholdDeliverySetting(Meal.DINNER, (0, 1, 2, 3, 4), "도시락")
    prepared = build_person_profile_revision(
        roster,
        subject.person_id,
        replace(subject.profile, note="수정"),
        actor=ACTOR,
        recorded_at=NOW,
        effective_on=TODAY,
        revision_id=uuid4(),
        household_delivery_settings=(setting,),
    )

    assert tuple(change.entity_kind for change in prepared.record.changes) == (
        projections.HOUSEHOLD_KIND,
        projections.PERSON_KIND,
    )
    assert prepared.record.changes == projections.roster_audit_changes(
        roster, prepared.snapshot
    )
    with pytest.raises(TypeError):
        prepared.record.changes[1].after["note"] = "외부 변경"
    with pytest.raises(TypeError):
        prepared.record.changes[0].after["delivery_settings"][0]["form"] = "외부 변경"


def test_filtered_projection_rejects_non_uuid_identifiers():
    roster = _roster(1)
    with pytest.raises(TypeError, match="UUID"):
        projections.roster_audit_changes_for_people_and_households(
            roster,
            roster,
            person_ids=("not-a-uuid",),
        )
