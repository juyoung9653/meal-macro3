"""Build one reviewed person-profile edit as an immutable UI revision."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime
from uuid import UUID

from .projections import roster_audit_changes
from .revisions import (
    RevisionActor,
    RevisionMetadata,
    RevisionRecord,
    RevisionSource,
)
from .roster import HouseholdDeliverySetting, Person, PersonProfile, RosterSnapshot

PERSON_PROFILE_EDIT_POLICY_VERSION = 1
PERSON_PROFILE_FIELD_KEYS = (
    "name",
    "birth_date",
    "school_type",
    "school_name",
    "grade",
    "class_name",
    "household_head_name",
    "relationship_to_child",
    "jurisdiction",
    "provider",
    "road_address",
    "detail_address",
    "telephone",
    "mobile_phone",
    "note",
)


@dataclass(frozen=True, slots=True)
class PreparedPersonProfileRevision:
    snapshot: RosterSnapshot
    record: RevisionRecord
    changed_fields: tuple[str, ...]
    cleared_fields: tuple[str, ...]
    changed_custom_fields: tuple[str, ...] = ()
    cleared_custom_fields: tuple[str, ...] = ()


def profile_household_members(roster: RosterSnapshot, person_id: UUID, on: date) -> tuple[Person, ...]:
    """Resolve stored household membership on the edit date, including the subject."""
    person = roster.person(person_id)
    for membership in roster.memberships:
        if membership.person_id == person_id and membership.period.contains(on):
            return roster.household_members(membership.household_id, on=on)
    return (person,)


def profile_household_id(roster: RosterSnapshot, person_id: UUID, on: date) -> UUID | None:
    roster.person(person_id)
    for membership in roster.memberships:
        if membership.person_id == person_id and membership.period.contains(on):
            return membership.household_id
    return None


def build_person_profile_revision(
    current_roster: RosterSnapshot,
    person_id: UUID,
    updated_profile: PersonProfile,
    *,
    actor: RevisionActor,
    recorded_at: datetime,
    effective_on: date,
    revision_id: UUID,
    reason: str = "사람 정보 수정",
    household_mobile_phone: bool = False,
    household_delivery_settings: tuple[HouseholdDeliverySetting, ...] | None = None,
    person_delivery_settings: tuple[HouseholdDeliverySetting, ...] | None = None,
    delivery_settings_to_household: bool = False,
) -> PreparedPersonProfileRevision:
    """Edit a profile and optionally share its mobile phone with its household."""

    if not isinstance(current_roster, RosterSnapshot):
        raise TypeError("current roster는 RosterSnapshot이어야 합니다.")
    if not isinstance(person_id, UUID):
        raise TypeError("person ID는 UUID여야 합니다.")
    if not isinstance(updated_profile, PersonProfile):
        raise TypeError("updated profile은 PersonProfile이어야 합니다.")
    if not isinstance(actor, RevisionActor):
        raise TypeError("actor는 RevisionActor여야 합니다.")
    if not isinstance(recorded_at, datetime):
        raise TypeError("recorded_at은 datetime이어야 합니다.")
    if recorded_at.tzinfo is None or recorded_at.utcoffset() is None:
        raise ValueError("recorded_at에는 시간대가 있어야 합니다.")
    if not isinstance(effective_on, date):
        raise TypeError("effective_on은 date여야 합니다.")
    if not isinstance(revision_id, UUID):
        raise TypeError("revision ID는 UUID여야 합니다.")
    if revision_id == current_roster.revision_id:
        raise ValueError("새 revision ID는 현재 revision과 달라야 합니다.")

    current_person = current_roster.person(person_id)
    changed_fields = tuple(
        key
        for key in PERSON_PROFILE_FIELD_KEYS
        if getattr(current_person.profile, key) != getattr(updated_profile, key)
    )
    changed_custom_fields = tuple(
        str(definition_id)
        for definition_id in sorted(
            set(current_person.profile.custom_values)
            | set(updated_profile.custom_values),
            key=str,
        )
        if current_person.profile.custom_values.get(definition_id)
        != updated_profile.custom_values.get(definition_id)
    )

    cleared_fields = tuple(
        key
        for key in changed_fields
        if getattr(current_person.profile, key) is not None
        and getattr(updated_profile, key) is None
    )
    cleared_custom_fields = tuple(
        definition_id
        for definition_id in changed_custom_fields
        if UUID(definition_id) in current_person.profile.custom_values
        and UUID(definition_id) not in updated_profile.custom_values
    )

    household_id = profile_household_id(current_roster, person_id, effective_on)
    if delivery_settings_to_household and (person_delivery_settings is None or household_id is None):
        raise ValueError("가구 구성원에게 적용하려면 배송 설정과 가구 소속이 필요합니다.")
    delivery_members = (
        profile_household_members(current_roster, person_id, effective_on)
        if delivery_settings_to_household else (current_person,)
    )
    delivery_member_ids = {member.person_id for member in delivery_members}
    members = profile_household_members(current_roster, person_id, effective_on) if household_mobile_phone else (current_person,)
    member_ids = {member.person_id for member in members}
    persons = tuple(
        replace(person, profile=updated_profile,
                delivery_settings=person_delivery_settings if person_delivery_settings is not None and person.person_id in delivery_member_ids else person.delivery_settings)
        if person.person_id == person_id
        else replace(person,
                     profile=replace(person.profile, mobile_phone=updated_profile.mobile_phone) if household_mobile_phone and person.person_id in member_ids else person.profile,
                     delivery_settings=person_delivery_settings if person_delivery_settings is not None and person.person_id in delivery_member_ids else person.delivery_settings)
        if (household_mobile_phone and person.person_id in member_ids) or (person_delivery_settings is not None and person.person_id in delivery_member_ids)
        else person
        for person in current_roster.persons
    )
    if household_delivery_settings is not None and household_id is None:
        raise ValueError("배송 설정을 저장할 가구가 연결되지 않았습니다.")
    households = tuple(
        replace(household, delivery_settings=household_delivery_settings)
        if household_delivery_settings is not None and household.household_id == household_id
        else household
        for household in current_roster.households
    )
    snapshot = RosterSnapshot(
        revision_id,
        persons=persons,
        households=households,
        memberships=current_roster.memberships,
        enrollments=current_roster.enrollments,
        assignments=current_roster.assignments,
        custom_fields=current_roster.custom_fields,
    )
    changes = roster_audit_changes(current_roster, snapshot)
    if not changes:
        raise ValueError("변경된 사람 정보가 없습니다.")

    decisions = {
        "person_profile_edit": {
            "person_id": str(person_id),
            "changed_fields": list(changed_fields),
            "cleared_fields": list(cleared_fields),
            "changed_custom_fields": list(changed_custom_fields),
            "cleared_custom_fields": list(cleared_custom_fields),
        }
    }
    if household_mobile_phone:
        decisions["person_profile_edit"]["household_mobile_phone"] = {
            "person_ids": [str(member.person_id) for member in members],
            "field": "mobile_phone",
        }
    if household_delivery_settings is not None:
        decisions["person_profile_edit"]["household_delivery_settings"] = {
            "household_id": str(household_id),
            "meals": [setting.meal.value for setting in household_delivery_settings],
        }
    if person_delivery_settings is not None:
        decisions["person_profile_edit"]["person_delivery_settings"] = {
            "person_ids": [str(member.person_id) for member in delivery_members],
            "meals": [setting.meal.value for setting in person_delivery_settings],
        }
    metadata = RevisionMetadata(
        revision_id=revision_id,
        parent_revision_id=current_roster.revision_id,
        recorded_at=recorded_at,
        effective_on=effective_on,
        actor=actor,
        reason=reason,
        source=RevisionSource.UI,
        decisions=decisions,
        rule_versions={
            "person_profile_edit_policy_version": (PERSON_PROFILE_EDIT_POLICY_VERSION)
        },
    )
    return PreparedPersonProfileRevision(
        snapshot,
        RevisionRecord(metadata, changes),
        changed_fields,
        cleared_fields,
        changed_custom_fields,
        cleared_custom_fields,
    )
