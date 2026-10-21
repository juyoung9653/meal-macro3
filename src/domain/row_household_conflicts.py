"""Resolve only a new row's differing household shared values before saving."""

from dataclasses import dataclass, replace
from uuid import UUID

from .column_input_rules import load_rules, rule_for, validate_input, validate_new_profile
from .household_shared import (
    HOUSEHOLD_SHARED_ID, HOUSEHOLD_SHARED_KIND, _ReadContext, _set_values,
    _shared_state, _values, expand_household_shared_revision,
    household_shared_components, shared_columns_from_state, shared_exceptions_from_state,
    row_add_comparison_days,
)
from .projections import ROSTER_ENTITY_KINDS, roster_audit_changes, roster_from_historical_snapshot
from .revisions import AuditChange, ChangeOperation, EntityRef, HistoricalSnapshot


@dataclass(frozen=True)
class RowHouseholdConflict:
    key: str
    components: tuple
    entered: tuple[str, ...]
    existing: tuple[tuple[str, ...], ...]


def _proposed_snapshot(snapshot, record):
    entities = dict(snapshot.entities)
    for change in record.changes:
        ref = EntityRef(change.entity_kind, change.entity_id)
        if change.after is None:
            entities.pop(ref, None)
        else:
            entities[ref] = change.after
    return HistoricalSnapshot(record.metadata.revision_id, entities)


def row_household_conflicts(snapshot, prepared):
    if snapshot is None:
        return ()
    pid = UUID(prepared.record.metadata.decisions["roster_row_change"]["person_id"])
    roster = prepared.snapshot
    on = prepared.record.metadata.effective_on
    state = snapshot.entity(HOUSEHOLD_SHARED_KIND, HOUSEHOLD_SHARED_ID)
    exceptions = shared_exceptions_from_state(state)
    context = _ReadContext(_proposed_snapshot(snapshot, prepared.record))
    conflicts = []
    for key in sorted(shared_columns_from_state(state)):
        if (pid, key) in exceptions:
            continue
        differing = set()
        for day in row_add_comparison_days(roster, pid, on):
            household = roster.household_for(pid, on=day)
            if household is None:
                continue
            existing = {
                _values(roster, key, member.person_id, day, context=context)
                for member in roster.household_members(household.household_id, on=day)
                if member.person_id != pid and (member.person_id, key) not in exceptions
            }
            candidate = _values(roster, key, pid, day, context=context)
            if existing and (len(existing) != 1 or candidate not in existing):
                differing.update(existing)
        entered = _values(roster, key, pid, on, context=context)
        if differing:
            conflicts.append(RowHouseholdConflict(key, household_shared_components(roster, key), entered, tuple(sorted(differing))))
    return tuple(conflicts)


def resolve_row_household_conflicts(snapshot, prepared, corrections, exception_keys):
    """Return one atomic row/exception revision; never change existing people."""
    conflicts = {item.key: item for item in row_household_conflicts(snapshot, prepared)}
    exception_keys = frozenset(exception_keys)
    if not (set(corrections) | exception_keys) <= conflicts.keys():
        raise ValueError("서로 다른 공용 항목만 수정하거나 예외로 지정할 수 있습니다.")
    pid = UUID(prepared.record.metadata.decisions["roster_row_change"]["person_id"])
    working = prepared.snapshot
    rules = load_rules(snapshot)
    for key, values in corrections.items():
        if key in exception_keys:
            continue
        if len(values) != len(conflicts[key].components):
            raise ValueError("공용 항목의 입력값 수가 올바르지 않습니다.")
        if key.startswith("status:"):
            raise ValueError("식종 상태는 입력 화면에서 수정하거나 예외로 지정해주세요.")
        if key == "sequence":
            for value in values:
                validate_input(key, value, rule_for(rules, key))
        working = _set_values(working, key, pid, values, prepared.record.metadata.effective_on)
    validate_new_profile(working.person(pid).profile, rules)
    before = roster_from_historical_snapshot(snapshot)
    extra = [c for c in prepared.record.changes if c.entity_kind not in ROSTER_ENTITY_KINDS]
    if exception_keys:
        state = snapshot.entity(HOUSEHOLD_SHARED_KIND, HOUSEHOLD_SHARED_ID)
        exceptions = shared_exceptions_from_state(state) | {(pid, key) for key in exception_keys}
        extra.append(AuditChange(HOUSEHOLD_SHARED_KIND, HOUSEHOLD_SHARED_ID,
                                ChangeOperation.UPDATE if state is not None else ChangeOperation.INSERT,
                                state, _shared_state(shared_columns_from_state(state), exceptions)))
    record = replace(prepared.record, changes=(*roster_audit_changes(before, working), *extra))
    # The same merge guard runs again inside the database transaction.
    expand_household_shared_revision(snapshot, record)
    return replace(prepared, snapshot=working, record=record)
