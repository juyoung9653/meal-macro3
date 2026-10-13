"""Apply confirmed staging identities to a new immutable roster snapshot."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, timedelta
from enum import StrEnum
from types import MappingProxyType
from uuid import UUID

from src.domain.roster import (
    CustomFieldDefinition,
    CustomFieldType,
    InclusiveDateRange,
    Meal,
    MealEnrollment,
    Person,
    PersonProfile,
    RosterSnapshot,
    additional_mobile_phone_field_id,
    additional_mobile_phone_field_name,
    additional_mobile_phone_field_ordinal,
    additional_telephone_field_id,
    additional_telephone_field_name,
    additional_telephone_field_ordinal,
    normalize_phone,
)

from .reconciliation import (
    ReconciliationDisposition,
    ReconciliationPlan,
    StagedPerson,
)

PROFILE_UPDATE_POLICY_VERSION = 1
PROFILE_BLANK_VALUE_POLICY = "preserve_existing"


class IdentityResolution(StrEnum):
    LINK_EXISTING = "link_existing"
    CREATE_NEW = "create_new"
    IGNORE = "ignore"


@dataclass(frozen=True, slots=True)
class ReconciliationDecision:
    observation_id: UUID
    resolution: IdentityResolution
    person_id: UUID | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.observation_id, UUID):
            raise TypeError("observation ID는 UUID여야 합니다.")
        if not isinstance(self.resolution, IdentityResolution):
            raise TypeError("resolution은 IdentityResolution이어야 합니다.")
        if self.resolution is IdentityResolution.IGNORE:
            if self.person_id is not None:
                raise ValueError("ignore 결정에는 person ID를 둘 수 없습니다.")
        elif not isinstance(self.person_id, UUID):
            raise TypeError("link/create 결정에는 person UUID가 필요합니다.")


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    snapshot: RosterSnapshot
    person_ids_by_observation: Mapping[UUID, UUID]
    ignored_observation_ids: tuple[UUID, ...]
    decision_manifest: Mapping[str, object]

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot, RosterSnapshot):
            raise TypeError("snapshot은 RosterSnapshot이어야 합니다.")
        mapping = dict(self.person_ids_by_observation)
        if any(
            not isinstance(observation_id, UUID) or not isinstance(person_id, UUID)
            for observation_id, person_id in mapping.items()
        ):
            raise TypeError("observation/person mapping은 UUID여야 합니다.")
        ignored = tuple(self.ignored_observation_ids)
        if any(not isinstance(item, UUID) for item in ignored):
            raise TypeError("ignored observation ID는 UUID여야 합니다.")
        if tuple(sorted(set(ignored), key=str)) != ignored:
            raise ValueError("ignored observation ID는 중복 없이 UUID순이어야 합니다.")
        object.__setattr__(
            self,
            "person_ids_by_observation",
            MappingProxyType(mapping),
        )
        object.__setattr__(self, "ignored_observation_ids", ignored)
        object.__setattr__(
            self,
            "decision_manifest",
            MappingProxyType(dict(self.decision_manifest)),
        )


def apply_reconciliation(
    roster: RosterSnapshot | None,
    plan: ReconciliationPlan,
    *,
    new_revision_id: UUID,
    decisions: Mapping[UUID, ReconciliationDecision] | None = None,
    custom_fields: Sequence[CustomFieldDefinition] = (),
) -> ReconciliationResult:
    """Apply confirmed identities; phone-based household candidates stay untouched."""

    if roster is not None and not isinstance(roster, RosterSnapshot):
        raise TypeError("roster는 RosterSnapshot 또는 None이어야 합니다.")
    if not isinstance(plan, ReconciliationPlan):
        raise TypeError("plan은 ReconciliationPlan이어야 합니다.")
    roster_revision_id = None if roster is None else roster.revision_id
    if plan.roster_revision_id != roster_revision_id:
        raise ValueError("reconciliation plan의 기준 revision이 현재 roster와 다릅니다.")
    if not isinstance(new_revision_id, UUID):
        raise TypeError("새 revision ID는 UUID여야 합니다.")
    if roster is not None and new_revision_id == roster.revision_id:
        raise ValueError("새 revision ID는 기준 revision과 달라야 합니다.")
    decision_by_id = dict(decisions or {})
    for observation_id, decision in decision_by_id.items():
        if not isinstance(observation_id, UUID):
            raise TypeError("decision key는 observation UUID여야 합니다.")
        if not isinstance(decision, ReconciliationDecision):
            raise TypeError("decisions에는 ReconciliationDecision만 허용됩니다.")
        if decision.observation_id != observation_id:
            raise ValueError("decision key와 decision observation ID가 다릅니다.")
    staged_ids = {item.observation.observation_id for item in plan.staged_people}
    unknown = set(decision_by_id) - staged_ids
    if unknown:
        raise ValueError(f"plan에 없는 observation 결정입니다: {sorted(unknown, key=str)}")

    persons_by_id = {
        person.person_id: person
        for person in (() if roster is None else roster.persons)
    }
    assignments_by_key = {
        (assignment.person_id, assignment.meal): assignment
        for assignment in (() if roster is None else roster.assignments)
    }
    imported_custom_fields = tuple(custom_fields)
    custom_fields = list(() if roster is None else roster.custom_fields)
    for definition in imported_custom_fields:
        if not isinstance(definition, CustomFieldDefinition):
            raise TypeError("custom_fields에는 CustomFieldDefinition만 허용됩니다.")
        existing = next((item for item in custom_fields if item.definition_id == definition.definition_id), None)
        if existing is not None:
            if existing.field_type is not definition.field_type:
                raise ValueError(f"‘{existing.name}’ 사용자 정의 필드 형식이 가져올 값과 다릅니다.")
            continue
        names = {item.name.casefold() for item in custom_fields}
        name = definition.name
        ordinal = 2
        while name.casefold() in names:
            name = f"{definition.name} ({ordinal})"
            ordinal += 1
        custom_fields.append(replace(
            definition, name=name,
            display_order=max((item.display_order for item in custom_fields), default=-1) + 1,
        ))
    imported_custom_ids = frozenset(definition.definition_id for definition in imported_custom_fields)
    enrollments = list(() if roster is None else roster.enrollments)
    applied: dict[UUID, UUID] = {}
    ignored: list[UUID] = []
    used_person_ids: set[UUID] = set()
    preserved_blank_profile_fields: dict[UUID, tuple[str, ...]] = {}
    additional_mobile_phone_fields: dict[int, CustomFieldDefinition] = {}
    additional_telephone_fields: dict[int, CustomFieldDefinition] = {}

    for staged in plan.staged_people:
        decision = decision_by_id.get(staged.observation.observation_id)
        resolution, person_id = _resolve_identity(staged, decision)
        if resolution is IdentityResolution.IGNORE:
            ignored.append(staged.observation.observation_id)
            continue
        assert person_id is not None
        if person_id in used_person_ids:
            raise ValueError(
                "한 import batch의 둘 이상의 observation을 같은 사람에게 적용할 수 없습니다: "
                f"{person_id}"
            )
        used_person_ids.add(person_id)

        if resolution is IdentityResolution.CREATE_NEW:
            if person_id in persons_by_id:
                raise ValueError(f"새 person ID가 이미 roster에 있습니다: {person_id}")
            base_profile = None
        else:
            if person_id not in persons_by_id:
                raise ValueError(f"연결할 person ID가 roster에 없습니다: {person_id}")
            base_profile = persons_by_id[person_id].profile
            preserved_fields = staged.observation.blank_profile_fields_preserved_from(
                base_profile
            )
            if preserved_fields:
                preserved_blank_profile_fields[staged.observation.observation_id] = (
                    tuple(field.value for field in preserved_fields)
                )

        merged_profile = staged.observation.merged_profile(base_profile)
        merged_profile, used_telephone_fields = _merge_additional_phones(
            merged_profile,
            merged_profile.telephone,
            staged.observation.additional_telephones,
            custom_fields,
            field_id=additional_telephone_field_id,
            field_name=additional_telephone_field_name,
            field_ordinal=additional_telephone_field_ordinal,
            protected_ids=imported_custom_ids | frozenset(staged.observation.custom_values),
        )
        additional_telephone_fields.update(
            _definitions_by_ordinal(
                used_telephone_fields,
                additional_telephone_field_ordinal,
            )
        )
        merged_profile, used_mobile_phone_fields = _merge_additional_phones(
            merged_profile,
            merged_profile.mobile_phone,
            staged.observation.additional_mobile_phones,
            custom_fields,
            field_id=additional_mobile_phone_field_id,
            field_name=additional_mobile_phone_field_name,
            field_ordinal=additional_mobile_phone_field_ordinal,
            protected_ids=imported_custom_ids | frozenset(staged.observation.custom_values),
        )
        additional_mobile_phone_fields.update(
            _definitions_by_ordinal(
                used_mobile_phone_fields,
                additional_mobile_phone_field_ordinal,
            )
        )
        persons_by_id[person_id] = Person(
            person_id, merged_profile,
            None if person_id not in persons_by_id else persons_by_id[person_id].delivery_settings,
        )
        enrollments = _replace_active_meals(
            enrollments,
            person_id,
            set(staged.observation.meals),
            plan.effective_on,
        )
        for observed_assignment in staged.observation.assignments:
            assignment = observed_assignment.for_person(person_id)
            assignments_by_key[(person_id, assignment.meal)] = assignment
        applied[staged.observation.observation_id] = person_id

    snapshot = RosterSnapshot(
        revision_id=new_revision_id,
        persons=tuple(sorted(persons_by_id.values(), key=lambda item: str(item.person_id))),
        **({} if roster is None else {"delivery_defaults": roster.delivery_defaults}),
        households=() if roster is None else roster.households,
        memberships=() if roster is None else roster.memberships,
        enrollments=tuple(
            sorted(
                enrollments,
                key=lambda item: (
                    str(item.person_id),
                    item.meal.value,
                    item.period.starts_on or date.min,
                    item.period.ends_on or date.max,
                ),
            )
        ),
        assignments=tuple(
            sorted(
                assignments_by_key.values(),
                key=lambda item: (
                    str(item.person_id),
                    "" if item.meal is None else item.meal.value,
                ),
            )
        ),
        custom_fields=tuple(
            sorted(custom_fields, key=lambda item: item.display_order)
        ),
    )
    ignored_ids = tuple(sorted(ignored, key=str))
    manifest = {
        "effective_on": plan.effective_on.isoformat(),
        "applied": {
            str(observation_id): str(person_id)
            for observation_id, person_id in sorted(
                applied.items(),
                key=lambda item: str(item[0]),
            )
        },
        "ignored": [str(observation_id) for observation_id in ignored_ids],
        "profile_update_policy": {
            "version": PROFILE_UPDATE_POLICY_VERSION,
            "mapped_blank_values": PROFILE_BLANK_VALUE_POLICY,
        },
        "preserved_blank_profile_fields": {
            str(observation_id): list(fields)
            for observation_id, fields in sorted(
                preserved_blank_profile_fields.items(),
                key=lambda item: str(item[0]),
            )
        },
        "additional_mobile_phone_fields": {
            additional_mobile_phone_field_name(ordinal): str(definition.definition_id)
            for ordinal, definition in sorted(additional_mobile_phone_fields.items())
        },
        "additional_telephone_fields": {
            additional_telephone_field_name(ordinal): str(definition.definition_id)
            for ordinal, definition in sorted(additional_telephone_fields.items())
        },
    }
    return ReconciliationResult(snapshot, applied, ignored_ids, manifest)


def _merge_additional_phones(
    profile: PersonProfile,
    primary_phone: str | None,
    observed_additional_phones: tuple[str, ...],
    custom_fields: list[CustomFieldDefinition],
    *,
    field_id: Callable[[int], UUID],
    field_name: Callable[[int], str],
    field_ordinal: Callable[[str], int | None],
    protected_ids: frozenset[UUID] = frozenset(),
) -> tuple[PersonProfile, tuple[CustomFieldDefinition, ...]]:
    # A user-selected column can have a numbered phone header too. Only slots
    # created by the automatic phone merger may be deduplicated or reassigned.
    phone_definitions = tuple(
        sorted(
            (
                definition
                for definition in custom_fields
                if definition.field_type is CustomFieldType.TEXT
                and (ordinal := field_ordinal(definition.name)) is not None
                and definition.definition_id == field_id(ordinal)
                and definition.definition_id not in protected_ids
            ),
            key=lambda definition: field_ordinal(definition.name) or 0,
        )
    )
    existing_additional_phones = tuple(
        value
        for definition in phone_definitions
        if isinstance(
            (value := profile.custom_values.get(definition.definition_id)),
            str,
        )
        and value.strip()
    )
    primary_key = _phone_identity_key(primary_phone)
    merged_phones: list[str] = []
    seen = {primary_key} if primary_key is not None else set()
    for phone in (*observed_additional_phones, *existing_additional_phones):
        key = _phone_identity_key(phone)
        if key is None or key in seen:
            continue
        seen.add(key)
        merged_phones.append(phone)

    custom_values = dict(profile.custom_values)
    for definition in phone_definitions:
        custom_values.pop(definition.definition_id, None)

    used_definitions: list[CustomFieldDefinition] = []
    automatic_ids = {definition.definition_id for definition in phone_definitions}
    reserved_names = {
        definition.name.casefold() for definition in custom_fields
        if definition.definition_id not in automatic_ids
    }
    ordinal = 2
    for phone in merged_phones:
        while field_name(ordinal).casefold() in reserved_names:
            ordinal += 1
        definition = _ensure_additional_phone_field(
            custom_fields,
            ordinal,
            field_id=field_id,
            field_name=field_name,
        )
        custom_values[definition.definition_id] = phone
        used_definitions.append(definition)
        ordinal += 1
    return (
        replace(profile, custom_values=custom_values),
        tuple(used_definitions),
    )


def _ensure_additional_phone_field(
    custom_fields: list[CustomFieldDefinition],
    ordinal: int,
    *,
    field_id: Callable[[int], UUID],
    field_name: Callable[[int], str],
) -> CustomFieldDefinition:
    name = field_name(ordinal)
    by_name = next(
        (
            definition
            for definition in custom_fields
            if definition.name.casefold() == name.casefold()
        ),
        None,
    )
    if by_name is not None:
        if by_name.field_type is not CustomFieldType.TEXT:
            raise ValueError(
                f"기존 ‘{name}’ 사용자 정의 필드가 문자 형식이 아닙니다. "
                "필드 형식을 확인해주세요."
            )
        return by_name

    definition_id = field_id(ordinal)
    id_collision = next(
        (
            definition
            for definition in custom_fields
            if definition.definition_id == definition_id
        ),
        None,
    )
    if id_collision is not None:
        raise ValueError(
            f"‘{name}’ 자동 필드 ID가 기존 ‘{id_collision.name}’ 필드와 겹칩니다."
        )
    definition = CustomFieldDefinition(
        definition_id,
        name,
        CustomFieldType.TEXT,
        max(
            (item.display_order for item in custom_fields),
            default=-1,
        )
        + 1,
    )
    custom_fields.append(definition)
    return definition


def _definitions_by_ordinal(
    definitions: tuple[CustomFieldDefinition, ...],
    field_ordinal: Callable[[str], int | None],
) -> dict[int, CustomFieldDefinition]:
    result: dict[int, CustomFieldDefinition] = {}
    for definition in definitions:
        ordinal = field_ordinal(definition.name)
        if ordinal is not None:
            result[ordinal] = definition
    return result


def _phone_identity_key(value: str | None) -> tuple[str, str] | None:
    if value is None:
        return None
    digits = normalize_phone(value)
    if digits:
        return "digits", digits
    compact = "".join(value.split()).casefold()
    return None if not compact else ("text", compact)


def _resolve_identity(
    staged: StagedPerson,
    decision: ReconciliationDecision | None,
) -> tuple[IdentityResolution, UUID | None]:
    if staged.disposition is ReconciliationDisposition.MATCHED:
        if decision is None:
            assert staged.matched_person_id is not None
            return IdentityResolution.LINK_EXISTING, staged.matched_person_id
        if decision.resolution is not IdentityResolution.IGNORE:
            raise ValueError("자동 matching 항목은 ignore만 별도 지정할 수 있습니다.")
        return IdentityResolution.IGNORE, None
    if decision is None:
        raise ValueError(
            "신규·검토 staging 항목의 identity 결정이 필요합니다: "
            f"{staged.observation.observation_id}"
        )
    return decision.resolution, decision.person_id


def _replace_active_meals(
    enrollments: list[MealEnrollment],
    person_id: UUID,
    desired_meals: set[Meal],
    effective_on: date,
) -> list[MealEnrollment]:
    result = list(enrollments)
    for meal in Meal:
        matching = [
            item
            for item in result
            if item.person_id == person_id and item.meal is meal
        ]
        active = next(
            (item for item in matching if item.period.contains(effective_on)),
            None,
        )
        if meal in desired_meals and active is None:
            future_starts = [
                item.period.starts_on
                for item in matching
                if item.period.starts_on is not None
                and item.period.starts_on > effective_on
            ]
            ends_on = (
                None
                if not future_starts
                else min(future_starts) - timedelta(days=1)
            )
            result.append(
                MealEnrollment(
                    person_id,
                    meal,
                    InclusiveDateRange(effective_on, ends_on),
                )
            )
        elif meal not in desired_meals and active is not None:
            result.remove(active)
            if active.period.starts_on is None or active.period.starts_on < effective_on:
                if effective_on > date.min:
                    result.append(
                        MealEnrollment(
                            person_id,
                            meal,
                            InclusiveDateRange(
                                active.period.starts_on,
                                effective_on - timedelta(days=1),
                            ),
                        )
                    )
    return result
