"""Append-only correction of coverage introduced by the first roster import."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime
from uuid import UUID

from src.domain.projections import (PERSON_KIND, MEMBERSHIP_KIND, ENROLLMENT_KIND, enrollment_entity_id,
                                    roster_audit_changes, roster_from_historical_snapshot)
from src.domain.revisions import ChangeOperation, EntityRef, HistoricalSnapshot, RevisionActor, RevisionMetadata, RevisionRecord, RevisionSource
from src.domain.roster import InclusiveDateRange, RosterSnapshot

CORRECTION_KEY = "initial_roster_effective_on"


@dataclass(frozen=True, slots=True)
class InitialRosterDateContext:
    initial_revision_id: UUID
    initial_date: date
    initial_roster: RosterSnapshot
    current_roster: RosterSnapshot
    current_snapshot: HistoricalSnapshot
    allowed_starts: frozenset[date]
    changed_boundaries: frozenset[EntityRef] = frozenset()


@dataclass(frozen=True, slots=True)
class InitialRosterDatePlan:
    context: InitialRosterDateContext
    effective_on: date
    roster: RosterSnapshot
    affected_people: int
    skipped: tuple[str, ...]

    def revision(self, *, actor: RevisionActor, recorded_at: datetime, revision_id: UUID) -> RevisionRecord | None:
        changes = roster_audit_changes(self.context.current_roster, self.roster)
        if not changes:
            return None
        record = RevisionRecord(RevisionMetadata(
            revision_id, self.context.current_roster.revision_id, recorded_at,
            self.effective_on, actor, "초기 명단 적용일 정정", RevisionSource.UI,
            decisions={CORRECTION_KEY: {
                "source_revision_id": str(self.context.initial_revision_id),
                "original_date": self.context.initial_date.isoformat(),
                "effective_on": self.effective_on.isoformat(),
                "affected_people": self.affected_people,
            }},
        ), changes)
        # Shared-column propagation must not turn a historical correction into
        # new enrollments for other household members or overwrite other fields.
        from src.domain.household_shared import expand_household_shared_revision
        try:
            expanded = expand_household_shared_revision(self.context.current_snapshot, record)
        except ValueError as error:
            raise ValueError("가구 공용 설정과 처음 명단의 값이 달라 적용일을 바꿀 수 없습니다. 공용 설정을 확인한 뒤 다시 시도해주세요.") from error
        if expanded.changes != record.changes:
            raise ValueError("가구 공용 설정 때문에 다른 가입 기간도 함께 바뀔 수 있습니다. 해당 식종의 공용 설정을 확인한 뒤 다시 시도해주세요.")
        return record


def load_initial_roster_date_context(database) -> InitialRosterDateContext:
    latest = database.latest_revision()
    if latest is None:
        raise ValueError("처음 가져온 명단이 없습니다.")
    records, visited = [], set()
    record = latest
    while record is not None:
        if record.metadata.revision_id in visited:
            raise ValueError("변경 이력 연결을 확인할 수 없습니다.")
        visited.add(record.metadata.revision_id)
        records.append(record)
        parent = record.metadata.parent_revision_id
        record = None if parent is None else database.revision(parent)
    records.reverse()
    if hasattr(database, "branch_revision_ids"):
        records = [database.revision(value) for value in database.branch_revision_ids(latest.metadata.revision_id)]
    # Settings-only revisions may precede the first roster. A later reimport
    # must never become the baseline, even if older people were deleted.
    initial = next((item for item in records if any(
        change.entity_kind == PERSON_KIND for change in item.changes
    )), None)
    if initial is None or initial.metadata.source is not RevisionSource.IMPORT or initial.import_batch is None:
        raise ValueError("처음 가져온 Excel 명단의 기록을 확인할 수 없어 적용일을 바꿀 수 없습니다.")
    batch = initial.import_batch
    purpose = initial.metadata.decisions.get("import_purpose")
    if (purpose not in (None, "database") or
            (purpose is None and initial.metadata.reason != "Excel 명단 가져오기") or
            batch.effective_on != initial.metadata.effective_on or
            any(change.operation is not ChangeOperation.INSERT for change in initial.changes if change.entity_kind == PERSON_KIND)):
        raise ValueError("처음 가져온 명단의 적용일 기록이 일치하지 않습니다.")
    baseline = roster_from_historical_snapshot(database.snapshot(initial.metadata.revision_id))
    applied = batch.decisions.get("applied")
    if not isinstance(applied, Mapping):
        raise ValueError("처음 가져온 명단의 대상자 연결 기록이 없습니다.")
    try:
        imported_ids = {UUID(value) for value in applied.values()}
    except (ValueError, TypeError, AttributeError) as error:
        raise ValueError("처음 가져온 명단의 대상자 연결 기록을 확인할 수 없습니다.") from error
    if imported_ids != {person.person_id for person in baseline.persons} or not imported_ids:
        raise ValueError("처음 가져온 명단과 대상자 연결 기록이 일치하지 않습니다.")
    allowed = {batch.effective_on}
    initial_refs = {EntityRef(change.entity_kind, change.entity_id) for change in initial.changes
                    if change.entity_kind in (MEMBERSHIP_KIND, ENROLLMENT_KIND)}
    blocked = frozenset()
    blocked_at_revision = {}
    for item in records:
        correction = item.metadata.decisions.get(CORRECTION_KEY)
        is_correction = isinstance(correction, Mapping) and correction.get("source_revision_id") == str(initial.metadata.revision_id)
        if is_correction:
            allowed.add(date.fromisoformat(correction["effective_on"]))
        restore_target = item.metadata.decisions.get("snapshot_restore_target")
        if restore_target is not None:
            # Exact restore recovers the eligibility of that historical state.
            blocked = blocked_at_revision.get(UUID(restore_target), frozenset(initial_refs))
        elif item.metadata.revision_id != initial.metadata.revision_id and not is_correction:
            changed = set(blocked)
            for change in item.changes:
                ref = EntityRef(change.entity_kind, change.entity_id)
                if ref in initial_refs and _first_boundary(change.before) != _first_boundary(change.after):
                    changed.add(ref)
            blocked = frozenset(changed)
        blocked_at_revision[item.metadata.revision_id] = blocked
    current_snapshot = database.snapshot(latest.metadata.revision_id)
    return InitialRosterDateContext(
        initial.metadata.revision_id, batch.effective_on, baseline,
        roster_from_historical_snapshot(current_snapshot), current_snapshot,
        frozenset(allowed), blocked,
    )


def _first_boundary(state):
    if state is None or not state.get("periods"):
        return None
    period = min(state["periods"], key=lambda item: item["starts_on"] or "")
    return period["starts_on"], period.get("household_id")


def plan_initial_roster_date(context: InitialRosterDateContext, effective_on: date) -> InitialRosterDatePlan:
    if type(effective_on) is not date:
        raise TypeError("적용일은 날짜여야 합니다.")
    if effective_on > context.initial_date:
        raise ValueError("처음 가져온 명단의 적용일보다 이른 날짜를 선택해주세요.")
    initial, current = context.initial_roster, context.current_roster
    members, meals = list(current.memberships), list(current.enrollments)
    current_ids = {person.person_id for person in current.persons}
    changed, skipped = set(), []
    for person in initial.persons:
        person_id = person.person_id
        if person_id not in current_ids:
            skipped.append(f"{person.profile.name}: 현재 명단에서 삭제되어 제외")
            continue
        original_members = [item for item in initial.memberships if item.person_id == person_id]
        current_members = [item for item in members if item.person_id == person_id]
        if EntityRef(MEMBERSHIP_KIND, person_id) in context.changed_boundaries:
            skipped.append(f"{person.profile.name}: 처음 가구 적용일을 나중에 수정하여 제외")
            continue
        if len(original_members) != 1 or original_members[0].period.starts_on != context.initial_date:
            skipped.append(f"{person.profile.name}: 처음 가구 적용일을 확인할 수 없어 제외")
            continue
        original_member = original_members[0]
        member = next((item for item in current_members if
                       item.household_id == original_member.household_id and item.period.starts_on in context.allowed_starts), None)
        if member is None or any(item.period.starts_on is None or item.period.starts_on < member.period.starts_on
                                 for item in current_members if item is not member):
            skipped.append(f"{person.profile.name}: 처음 가구 적용일이 변경되어 제외")
            continue
        updates = []
        for original in (item for item in initial.enrollments if item.person_id == person_id):
            if EntityRef(ENROLLMENT_KIND, enrollment_entity_id(person_id, original.meal)) in context.changed_boundaries:
                skipped.append(f"{person.profile.name} · {original.meal.value}: 처음 가입 기간을 나중에 수정하여 제외")
                continue
            if original.period.starts_on != context.initial_date:
                skipped.append(f"{person.profile.name} · {original.meal.value}: 별도 시작일을 유지")
                continue
            candidates = [item for item in meals if item.person_id == person_id and item.meal is original.meal]
            candidate = next((item for item in candidates if item.period.starts_on in context.allowed_starts), None)
            if candidate is None or any(item.period.starts_on is None or item.period.starts_on < candidate.period.starts_on
                                        for item in candidates if item is not candidate):
                skipped.append(f"{person.profile.name} · {original.meal.value}: 처음 가입 기간이 변경되어 제외")
                continue
            if effective_on < candidate.period.starts_on:
                updates.append((candidate, replace(candidate, period=InclusiveDateRange(effective_on, candidate.period.ends_on))))
        if not updates:
            continue
        for before, after in updates:
            meals[meals.index(before)] = after
        if effective_on < member.period.starts_on:
            members[members.index(member)] = replace(member, period=InclusiveDateRange(effective_on, member.period.ends_on))
        changed.add(person_id)
    roster = replace(current, memberships=tuple(members), enrollments=tuple(meals))
    return InitialRosterDatePlan(context, effective_on, roster, len(changed), tuple(skipped))
