"""Build one reviewed person-profile edit as an immutable UI revision."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID

from .projections import roster_audit_changes
from .revisions import (
    RevisionActor,
    RevisionMetadata,
    RevisionRecord,
    RevisionSource,
)
from .roster import Person, PersonProfile, RosterSnapshot


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
) -> PreparedPersonProfileRevision:
    """Replace one profile without touching household, meal, or delivery state."""

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
    if not changed_fields:
        raise ValueError("변경된 사람 정보가 없습니다.")
    cleared_fields = tuple(
        key
        for key in changed_fields
        if getattr(current_person.profile, key) is not None
        and getattr(updated_profile, key) is None
    )

    persons = tuple(
        Person(person.person_id, updated_profile)
        if person.person_id == person_id
        else person
        for person in current_roster.persons
    )
    snapshot = RosterSnapshot(
        revision_id,
        persons=persons,
        households=current_roster.households,
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
        }
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
            "person_profile_edit_policy_version": (
                PERSON_PROFILE_EDIT_POLICY_VERSION
            )
        },
    )
    return PreparedPersonProfileRevision(
        snapshot,
        RevisionRecord(metadata, changes),
        changed_fields,
        cleared_fields,
    )
