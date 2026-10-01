"""Immutable revisions for user-defined roster fields."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID

from .projections import roster_audit_changes
from .revisions import RevisionActor, RevisionMetadata, RevisionRecord, RevisionSource
from .roster import (
    CustomFieldDefinition,
    CustomFieldType,
    Person,
    PersonProfile,
    RosterSnapshot,
)


CUSTOM_FIELD_EDIT_POLICY_VERSION = 1
# Labels already represented by the canonical person profile.  They are never
# materialized as custom definitions, so accepting them would create ambiguity.
CANONICAL_FIELD_LABELS = frozenset(
    {
        "성명",
        "생년월일",
        "취학유형",
        "학교명",
        "학년",
        "반",
        "세대주 성명",
        "아동과의 관계",
        "관할동",
        "제공기관",
        "도로명주소",
        "상세주소",
        "전화번호",
        "핸드폰",
        "비고",
        "조식",
        "중식",
        "석식",
        "학교",
        "주소",
        "전화",
        "휴대폰",
        "연락처",
        "배송담당",
        "배송 담당",
        "순서",
        "배송순서",
        "식종",
        "이용 식종",
    }
)


@dataclass(frozen=True, slots=True)
class PreparedCustomFieldRevision:
    snapshot: RosterSnapshot
    record: RevisionRecord
    definition: CustomFieldDefinition | None
    removed_value_count: int = 0


def build_custom_field_create_revision(
    current_roster: RosterSnapshot,
    name: str,
    field_type: CustomFieldType,
    *,
    actor: RevisionActor,
    recorded_at: datetime,
    effective_on: date,
    revision_id: UUID,
    definition_id: UUID,
    choices: tuple[str, ...] = (),
    reason: str = "사용자 정의 필드 추가",
) -> PreparedCustomFieldRevision:
    _validate_common(current_roster, actor, recorded_at, effective_on, revision_id)
    if not isinstance(field_type, CustomFieldType):
        raise TypeError("field_type은 CustomFieldType이어야 합니다.")
    normalized = _valid_name(name, current_roster)
    definition = CustomFieldDefinition(
        definition_id,
        normalized,
        field_type,
        len(current_roster.custom_fields),
        choices,
    )
    snapshot = RosterSnapshot(
        revision_id,
        current_roster.persons,
        current_roster.households,
        current_roster.memberships,
        current_roster.enrollments,
        current_roster.assignments,
        (*current_roster.custom_fields, definition),
    )
    return _prepared(
        current_roster,
        snapshot,
        actor,
        recorded_at,
        effective_on,
        revision_id,
        reason,
        {
            "custom_field_edit": {
                "action": "create",
                "definition_id": str(definition_id),
                "name": normalized,
            }
        },
        definition,
    )


def build_custom_field_delete_revision(
    current_roster: RosterSnapshot,
    definition_id: UUID,
    *,
    actor: RevisionActor,
    recorded_at: datetime,
    effective_on: date,
    revision_id: UUID,
    reason: str = "사용자 정의 필드 삭제",
) -> PreparedCustomFieldRevision:
    _validate_common(current_roster, actor, recorded_at, effective_on, revision_id)
    definition = next(
        (
            item
            for item in current_roster.custom_fields
            if item.definition_id == definition_id
        ),
        None,
    )
    if definition is None:
        raise KeyError(f"사용자 정의 필드를 찾을 수 없습니다: {definition_id}")
    # Canonical labels cannot normally occur in a validated snapshot, but retain
    # the guard for legacy ledgers.
    if definition.name.casefold() in {
        label.casefold() for label in CANONICAL_FIELD_LABELS
    }:
        raise ValueError("기본 항목은 삭제할 수 없습니다.")
    removed = sum(
        definition_id in person.profile.custom_values
        for person in current_roster.persons
    )
    persons = tuple(
        _without_value(person, definition_id) for person in current_roster.persons
    )
    fields = tuple(
        item
        for item in current_roster.custom_fields
        if item.definition_id != definition_id
    )
    snapshot = RosterSnapshot(
        revision_id,
        persons,
        current_roster.households,
        current_roster.memberships,
        current_roster.enrollments,
        current_roster.assignments,
        fields,
    )
    return _prepared(
        current_roster,
        snapshot,
        actor,
        recorded_at,
        effective_on,
        revision_id,
        reason,
        {
            "custom_field_edit": {
                "action": "delete",
                "definition_id": str(definition_id),
                "removed_value_count": removed,
            }
        },
        None,
        removed,
    )


def _without_value(person: Person, definition_id: UUID) -> Person:
    values = dict(person.profile.custom_values)
    values.pop(definition_id, None)
    return Person(
        person.person_id,
        PersonProfile(
            name=person.profile.name,
            birth_date=person.profile.birth_date,
            school_type=person.profile.school_type,
            school_name=person.profile.school_name,
            grade=person.profile.grade,
            class_name=person.profile.class_name,
            household_head_name=person.profile.household_head_name,
            relationship_to_child=person.profile.relationship_to_child,
            jurisdiction=person.profile.jurisdiction,
            provider=person.profile.provider,
            road_address=person.profile.road_address,
            detail_address=person.profile.detail_address,
            telephone=person.profile.telephone,
            mobile_phone=person.profile.mobile_phone,
            note=person.profile.note,
            custom_values=values,
        ),
        person.delivery_settings,
    )


def _valid_name(name: str, roster: RosterSnapshot) -> str:
    if not isinstance(name, str):
        raise TypeError("사용자 정의 필드명은 문자열이어야 합니다.")
    value = " ".join(name.split())
    if not value:
        raise ValueError("사용자 정의 필드명은 비워둘 수 없습니다.")
    used = {item.name.casefold() for item in roster.custom_fields}
    if value.casefold() in used:
        raise ValueError("같은 이름의 사용자 정의 필드가 이미 있습니다.")
    if value.casefold() in {label.casefold() for label in CANONICAL_FIELD_LABELS}:
        raise ValueError("기본 항목 이름은 사용자 정의 필드로 사용할 수 없습니다.")
    return value


def _validate_common(roster, actor, recorded_at, effective_on, revision_id) -> None:
    if not isinstance(roster, RosterSnapshot):
        raise TypeError("current roster는 RosterSnapshot이어야 합니다.")
    if not isinstance(actor, RevisionActor):
        raise TypeError("actor는 RevisionActor이어야 합니다.")
    if (
        not isinstance(recorded_at, datetime)
        or recorded_at.tzinfo is None
        or recorded_at.utcoffset() is None
    ):
        raise ValueError("recorded_at에는 시간대가 있어야 합니다.")
    if not isinstance(effective_on, date):
        raise TypeError("effective_on은 date여야 합니다.")
    if not isinstance(revision_id, UUID):
        raise TypeError("revision ID는 UUID여야 합니다.")
    if revision_id == roster.revision_id:
        raise ValueError("새 revision ID는 현재 revision과 달라야 합니다.")


def _prepared(
    before,
    snapshot,
    actor,
    recorded_at,
    effective_on,
    revision_id,
    reason,
    decisions,
    definition,
    removed=0,
):
    metadata = RevisionMetadata(
        revision_id,
        before.revision_id,
        recorded_at,
        effective_on,
        actor,
        reason,
        RevisionSource.UI,
        decisions=decisions,
        rule_versions={
            "custom_field_edit_policy_version": CUSTOM_FIELD_EDIT_POLICY_VERSION
        },
    )
    return PreparedCustomFieldRevision(
        snapshot,
        RevisionRecord(metadata, roster_audit_changes(before, snapshot)),
        definition,
        removed,
    )
