"""Atomic, dated household merges after a UI mobile-number change."""
from collections import defaultdict
from dataclasses import replace
from datetime import date, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from .household_shared import (
    HOUSEHOLD_SHARED_ID, HOUSEHOLD_SHARED_KIND, HouseholdSharedConflict,
    build_household_shared_revision, expand_household_shared_revision,
    household_shared_rows,
    shared_columns_from_state, shared_exceptions_from_state,
)
from .household_grouping import GROUPING_KIND, PROFILE_COLUMNS, grouping_columns, profile_grouping_key
from .delivery_routing import ROUTING_EVENT_KIND, routing_state_from_snapshot
from .import_shared import import_candidate_snapshot
from .projections import PERSON_KIND, roster_audit_changes, roster_from_historical_snapshot
from .revisions import AuditChange, ChangeOperation, RevisionSource
from .roster import InclusiveDateRange, normalize_phone

MERGE_DECISION = "mobile_household_merge"
REVIEW_DECISION = "mobile_household_shared_values"


class MobileHouseholdConflict(HouseholdSharedConflict):
    def __init__(self, key, on, candidate, household_ids):
        super().__init__(key, on, "합칠 가구의 공용 값을 확인해주세요.")
        self.candidate = candidate
        self.household_ids = frozenset(household_ids)


def needs_mobile_household_merge(record):
    if record.metadata.source not in (RevisionSource.UI, RevisionSource.IMPORT):
        return False
    if "snapshot_restore_target" in record.metadata.decisions:
        return False
    return record.metadata.source is RevisionSource.IMPORT or MERGE_DECISION in record.metadata.decisions or any(
        (c.entity_kind == PERSON_KIND and c.after is not None
         and c.before != c.after)
        or c.entity_kind in (GROUPING_KIND, ROUTING_EVENT_KIND)
        or (c.entity_kind == HOUSEHOLD_SHARED_KIND
            and "mobile" in shared_columns_from_state(c.after)
            and "mobile" not in shared_columns_from_state(c.before))
        for c in record.changes
    )


def with_mobile_shared_values(record, key, values):
    choices = dict(record.metadata.decisions.get(REVIEW_DECISION, {}))
    choices[key] = {str(hid): tuple(value) for hid, value in values.items()}
    return replace(record, metadata=replace(record.metadata,
        decisions={**record.metadata.decisions, REVIEW_DECISION: choices}))


def _compose(snapshot, candidate, record):
    changes = []
    for ref in dict.fromkeys((*snapshot.entities, *candidate.entities)):
        before, after = snapshot.entities.get(ref), candidate.entities.get(ref)
        if before != after:
            operation = (ChangeOperation.INSERT if before is None else
                         ChangeOperation.DELETE if after is None else ChangeOperation.UPDATE)
            changes.append(AuditChange(ref.entity_kind, ref.entity_id, operation, before, after))
    return replace(record, changes=tuple(changes))


def merge_mobile_households(roster, on, *, changed_person_ids=None, columns=("mobile",), snapshot=None):
    """Merge equal nonblank column tuples; preserve earlier and scheduled memberships."""
    buckets = defaultdict(set)
    active = {m.person_id: m for m in roster.memberships if m.period.contains(on)}
    seeds = set()
    routing = routing_state_from_snapshot(snapshot) if snapshot is not None and set(columns) & {"address", "detail_address", "jurisdiction"} else None
    for person in roster.persons:
        pid = person.person_id
        number = profile_grouping_key(person.profile, columns,
            routing=routing.resolve(roster, pid, on) if routing is not None else None)
        if not all(number) or pid not in active:
            continue
        buckets[number].add(active[pid].household_id)
        if changed_person_ids is None or pid in changed_person_ids:
            seeds.add(active[pid].household_id)
    parents = {m.household_id: m.household_id for m in active.values()}
    def root(hid):
        while parents[hid] != hid:
            parents[hid] = parents[parents[hid]]
            hid = parents[hid]
        return hid
    for household_ids in buckets.values():
        ordered = sorted(household_ids, key=str)
        for hid in ordered[1:]:
            a, b = sorted((root(ordered[0]), root(hid)), key=str)
            parents[b] = a
    groups = defaultdict(set)
    for hid in parents:
        groups[root(hid)].add(hid)
    groups = {target: members for target, members in groups.items()
              if len(members) > 1 and members & seeds}
    if not groups:
        return roster, {}
    destinations = {hid: target for target, members in groups.items() for hid in members}
    memberships = []
    for membership in roster.memberships:
        target = destinations.get(membership.household_id, membership.household_id)
        if target == membership.household_id or not membership.period.contains(on):
            memberships.append(membership)
            continue
        if on > date.min and (membership.period.starts_on is None or membership.period.starts_on < on):
            memberships.append(replace(membership,
                period=InclusiveDateRange(membership.period.starts_on, on - timedelta(days=1))))
        memberships.append(replace(membership, household_id=target,
            period=InclusiveDateRange(on, membership.period.ends_on)))
    # Keep existing per-person delivery behaviour when household defaults differ.
    households = {h.household_id: h for h in roster.households}
    people = []
    for person in roster.persons:
        membership = active.get(person.person_id)
        target = destinations.get(membership.household_id) if membership else None
        if target is not None and person.delivery_settings is None:
            previous = households[membership.household_id].delivery_settings
            if previous != households[target].delivery_settings:
                person = replace(person, delivery_settings=previous)
        people.append(person)
    merged = replace(roster, memberships=tuple(memberships), persons=tuple(people))
    return merged, groups


def prepare_mobile_household_revision(snapshot, record):
    """Dry-run and storage share this function; review choices never commit alone."""
    if not needs_mobile_household_merge(record):
        return record
    # A persisted merge already contains the reviewed final states. Replay must
    # not propagate its profile/routing changes again or create new event IDs.
    if MERGE_DECISION in record.metadata.decisions:
        return record
    expanded = expand_household_shared_revision(snapshot, record)
    candidate = import_candidate_snapshot(snapshot, expanded)
    roster = roster_from_historical_snapshot(candidate)
    stored = candidate.entity(HOUSEHOLD_SHARED_KIND, HOUSEHOLD_SHARED_ID)
    exceptions = shared_exceptions_from_state(stored)
    on = record.metadata.effective_on or record.metadata.recorded_at.astimezone(ZoneInfo("Asia/Seoul")).date()
    enabling = any(c.entity_kind == GROUPING_KIND for c in record.changes) or any(c.entity_kind == HOUSEHOLD_SHARED_KIND
        and "mobile" in shared_columns_from_state(c.after)
        and "mobile" not in shared_columns_from_state(c.before) for c in record.changes)
    columns = grouping_columns(candidate)
    def criterion_value(state, key):
        if key.startswith("custom:"):
            return state.get("custom_values", {}).get(key[7:])
        return state.get(PROFILE_COLUMNS[key][0])

    changed = {c.entity_id for c in expanded.changes if c.entity_kind == PERSON_KIND
        and c.after is not None and (c.before is None or any(
            criterion_value(c.before, key) != criterion_value(c.after, key) for key in columns))}
    if set(columns) & {"address", "detail_address", "jurisdiction"}:
        changed.update(UUID((c.after or c.before)["person_id"]) for c in expanded.changes
            if c.entity_kind == ROUTING_EVENT_KIND)
    merged, groups = merge_mobile_households(roster, on,
        changed_person_ids=None if enabling or record.metadata.source is RevisionSource.IMPORT else changed,
        columns=columns, snapshot=candidate)
    if not groups:
        return expanded
    candidate = import_candidate_snapshot(candidate,
        replace(record, changes=roster_audit_changes(roster, merged)))
    choices = record.metadata.decisions.get(REVIEW_DECISION, {})
    selected = shared_columns_from_state(stored)
    if not set(choices) <= selected:
        raise ValueError("공용으로 설정된 칼럼만 검토할 수 있습니다.")
    for key, raw_values in choices.items():
        current = roster_from_historical_snapshot(candidate)
        rows = household_shared_rows(current, key, on, snapshot=candidate)
        values = {row.household_id: row.values for row in rows}
        proposed = {UUID(hid): tuple(value) for hid, value in raw_values.items()}
        if not set(proposed) <= set(groups) or not set(proposed) <= set(values):
            raise ValueError("합칠 가구의 공용 값만 선택해주세요.")
        candidate = _apply_values(candidate, record, key, proposed, on)
    current = roster_from_historical_snapshot(candidate)
    for key in sorted(selected):
        rows = household_shared_rows(current, key, on, snapshot=candidate)
        for row in rows:
            if row.household_id not in groups or not any(v is None for v in row.values):
                continue
            if key == "mobile":
                numbers = {normalize_phone(v[0]) for v in row.existing_values}
                if len(numbers) == 1 and "" not in numbers:
                    # Formatting-only differences use the edited number if possible.
                    members = current.household_members(row.household_id, on=on)
                    chosen = next((p for p in members if p.person_id in changed
                                   and (p.person_id, key) not in exceptions), None)
                    value = chosen.profile.mobile_phone if chosen else row.existing_values[0][0]
                    candidate = _apply_values(candidate, record, key, {row.household_id: (value,)}, on)
                    current = roster_from_historical_snapshot(candidate)
                    continue
            raise MobileHouseholdConflict(key, on, candidate, groups)
    result = _compose(snapshot, candidate, expanded)
    return replace(result, metadata=replace(result.metadata, decisions={
        **result.metadata.decisions, MERGE_DECISION: {
            "effective_on": on.isoformat(),
            "households": {str(target): sorted(map(str, members)) for target, members in groups.items()},
        }}))


def _apply_values(candidate, record, key, values, on):
    """Use the existing shared-column editor on only the merged households."""
    roster = roster_from_historical_snapshot(candidate)
    stored = candidate.entity(HOUSEHOLD_SHARED_KIND, HOUSEHOLD_SHARED_ID)
    member_ids = {p.person_id for hid in values for p in roster.household_members(hid, on=on)}
    # Restrict the roster, not the snapshot: routing/status context stays available.
    subset = replace(roster, persons=tuple(p for p in roster.persons if p.person_id in member_ids),
        memberships=tuple(m for m in roster.memberships if m.person_id in member_ids),
        enrollments=tuple(e for e in roster.enrollments if e.person_id in member_ids),
        assignments=tuple(a for a in roster.assignments if a.person_id in member_ids))
    rows = household_shared_rows(subset, key, on, snapshot=candidate)
    complete = {row.household_id: row.values for row in rows}
    complete.update(values)
    if all(complete[row.household_id] == row.values for row in rows):
        return candidate
    correction = build_household_shared_revision(subset, stored, key, True, complete,
        actor=record.metadata.actor, recorded_at=record.metadata.recorded_at,
        effective_on=on, revision_id=record.metadata.revision_id, snapshot=candidate)
    return import_candidate_snapshot(candidate, correction)
