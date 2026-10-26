"""Prepare one atomic roster revision from manual Juso lookups."""
from __future__ import annotations

from dataclasses import dataclass, replace
from uuid import uuid4

from src.domain.column_input_rules import PROFILE_KEYS, load_rules, profile_value, rule_for, validate_input
from src.domain.household_shared import profile_with_shared_routing
from src.domain.projections import roster_audit_changes
from src.domain.revisions import RevisionMetadata, RevisionRecord, RevisionSource
from src.domain.roster import CustomFieldDefinition, CustomFieldType
from .juso_api import AddressLookupError

NEW_COLUMN = "new:old_address"


def source_options(roster):
    return {**{key: key for key in PROFILE_KEYS if key != "birth_date"},
            **{f"custom:{field.definition_id}": field.name for field in roster.custom_fields
               if field.field_type is CustomFieldType.TEXT}}


def target_options(roster):
    return {f"custom:{field.definition_id}": field.name for field in roster.custom_fields
            if field.field_type is CustomFieldType.TEXT}


def validate_columns(roster, source, target):
    if source not in source_options(roster):
        raise ValueError("신주소를 읽을 문자형 칼럼을 선택해주세요.")
    if target != NEW_COLUMN and target not in target_options(roster):
        raise ValueError("구주소를 저장할 문자형 사용자 칼럼을 선택해주세요.")
    if source == target:
        raise ValueError("신주소 칼럼과 구주소 칼럼은 서로 달라야 합니다.")


@dataclass(frozen=True)
class AddressUpdateResult:
    roster: object
    record: RevisionRecord | None
    updated: int
    unchanged: int
    skipped: int
    failures: tuple[str, ...]


def prepare_address_update(roster, snapshot, client, *, source, target, only_empty,
                           service_day, actor, recorded_at):
    validate_columns(roster, source, target)
    definition = None
    if target == NEW_COLUMN:
        name, index = "구주소", 2
        names = {field.name for field in roster.custom_fields}
        while name in names:
            name, index = f"구주소{index}", index + 1
        definition = CustomFieldDefinition(uuid4(), name, CustomFieldType.TEXT,
                                           max((f.display_order for f in roster.custom_fields), default=-1) + 1)
        target_key = f"custom:{definition.definition_id}"
    else:
        target_key = target
        definition = next(f for f in roster.custom_fields if target == f"custom:{f.definition_id}")
    rules = load_rules(snapshot)
    persons, failures = [], []
    updated = unchanged = skipped = 0
    # Cache failures too, without carrying addresses across manual runs.
    lookups = {}
    for person in roster.persons:
        old = profile_value(person.profile, target_key)
        if only_empty and old.strip():
            persons.append(person)
            skipped += 1
            continue
        profile = profile_with_shared_routing(roster, person.person_id, service_day, snapshot)
        address = profile_value(profile, source).strip()
        if not address:
            persons.append(person)
            skipped += 1
            continue
        try:
            if address not in lookups:
                try:
                    lookups[address] = client.lookup(address).jibun_address.strip()
                except AddressLookupError as error:
                    lookups[address] = error
            value = lookups[address]
            if isinstance(value, AddressLookupError):
                raise value
            if not value:
                raise ValueError("검색 결과에 구주소가 없습니다.")
            validate_input(target_key, value, rule_for(rules, target_key))
        except (AddressLookupError, ValueError) as error:
            failures.append(f"{person.profile.name}: {error}")
            persons.append(person)
            continue
        if old == value:
            unchanged += 1
            persons.append(person)
            continue
        values = dict(person.profile.custom_values)
        values[definition.definition_id] = value
        persons.append(replace(person, profile=replace(person.profile, custom_values=values)))
        updated += 1
    if not updated:
        return AddressUpdateResult(roster, None, 0, unchanged, skipped, tuple(failures))
    revision_id = uuid4()
    result = replace(roster, revision_id=revision_id, persons=tuple(persons),
                     custom_fields=(*roster.custom_fields, definition) if target == NEW_COLUMN else roster.custom_fields)
    metadata = RevisionMetadata(
        revision_id=revision_id, parent_revision_id=roster.revision_id,
        actor=actor, recorded_at=recorded_at, effective_on=service_day,
        reason="구주소 업데이트", source=RevisionSource.UI,
        decisions={"old_address_update": {"source": source, "target": target_key,
                   "only_empty": only_empty, "updated": updated, "failed": len(failures)}},
    )
    return AddressUpdateResult(result, RevisionRecord(metadata, roster_audit_changes(roster, result)),
                               updated, unchanged, skipped, tuple(failures))
