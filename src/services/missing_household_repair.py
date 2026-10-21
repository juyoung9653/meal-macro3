"""Audited repair for household memberships omitted by old manual row additions."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime
from uuid import UUID, uuid4

from src.domain.projections import MEMBERSHIP_KIND, PERSON_KIND, memberships_from_state, roster_audit_changes, roster_from_historical_snapshot
from src.domain.revisions import ChangeOperation, HistoricalSnapshot, RevisionActor, RevisionMetadata, RevisionRecord, RevisionSource
from src.domain.roster import Household, HouseholdMembership, InclusiveDateRange, Person, RosterSnapshot

REPAIR_KEY = "missing_household_repair"


@dataclass(frozen=True, slots=True)
class RepairCandidate:
    person: Person
    starts_on: date | None
    original_membership: HouseholdMembership | None = None


@dataclass(frozen=True, slots=True)
class MissingHouseholdContext:
    roster: RosterSnapshot
    snapshot: HistoricalSnapshot
    candidates: tuple[RepairCandidate, ...]
    skipped: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MissingHouseholdPlan:
    context: MissingHouseholdContext
    roster: RosterSnapshot

    @property
    def candidates(self) -> tuple[RepairCandidate, ...]:
        return self.context.candidates

    def revision(self, *, actor: RevisionActor, recorded_at: datetime, revision_id: UUID) -> RevisionRecord | None:
        changes = roster_audit_changes(self.context.roster, self.roster)
        if not changes:
            return None
        record = RevisionRecord(RevisionMetadata(
            revision_id, self.context.roster.revision_id, recorded_at,
            recorded_at.date(), actor, "가구 소속 누락·시작일 복구", RevisionSource.UI,
            decisions={REPAIR_KEY: {"person_ids": [str(item.person.person_id) for item in self.candidates]}},
        ), changes)
        # Repair only household membership facts. Shared-column expansion must
        # not alter another person's enrollment or profile.
        from src.domain.household_shared import expand_household_shared_revision
        expanded = expand_household_shared_revision(self.context.snapshot, record)
        if expanded.changes != record.changes:
            raise ValueError("가구 공용 설정이 다른 항목까지 변경할 수 있어 복구를 중단했습니다.")
        return record


def load_missing_household_context(database) -> MissingHouseholdContext:
    latest = database.latest_revision()
    if latest is None:
        raise ValueError("저장된 명단이 없습니다.")
    records, seen = [], set()
    record = latest
    while record is not None:
        revision_id = record.metadata.revision_id
        if revision_id in seen:
            raise ValueError("변경 이력 연결을 확인할 수 없습니다.")
        seen.add(revision_id)
        records.append(record)
        parent = record.metadata.parent_revision_id
        record = database.revision(parent) if parent is not None else None
    records.reverse()
    snapshot = database.snapshot(latest.metadata.revision_id)
    roster = roster_from_historical_snapshot(snapshot)
    # Track the entire lineage: a later insert/delete of membership is an
    # intentional or ambiguous history, even when its current state is empty.
    creation = {}
    original_manual_memberships = {}
    later_membership_changes = set()
    later_person_changes = set()
    for item in records:
        decision = item.metadata.decisions.get("roster_row_change")
        added_ids = set()
        if (item.metadata.source is RevisionSource.UI and isinstance(decision, Mapping)
                and decision.get("operation") == "add"):
            try:
                added_ids.add(UUID(decision["person_id"]))
            except (ValueError, TypeError, KeyError, AttributeError):
                pass
        inserted = {change.entity_id for change in item.changes
                    if change.entity_kind == PERSON_KIND and change.operation is ChangeOperation.INSERT}
        if (item.metadata.source is RevisionSource.IMPORT and item.import_batch is not None
                and item.metadata.decisions.get("import_purpose") == "vacation_lunch"):
            added_ids.update(inserted)
        created_here = added_ids & inserted
        for added_id in created_here:
            if added_id in creation:
                continue
            membership = next((change for change in item.changes
                               if change.entity_kind == MEMBERSHIP_KIND and change.entity_id == added_id), None)
            creation[added_id] = (membership is not None and membership.operation is ChangeOperation.INSERT
                                  and membership.after is not None
                                  and membership.after.get("person_id") == str(added_id)
                                  and not membership.after.get("periods"))
            if (item.metadata.source is RevisionSource.UI and membership is not None
                    and membership.operation is ChangeOperation.INSERT and membership.after is not None):
                original_members = memberships_from_state(added_id, membership.after)
                if len(original_members) == 1:
                    original_manual_memberships[added_id] = original_members[0]
        for change in item.changes:
            person_id = change.entity_id
            if person_id not in creation or person_id in created_here:
                continue
            if change.entity_kind == MEMBERSHIP_KIND:
                later_membership_changes.add(person_id)
            elif change.entity_kind == PERSON_KIND and change.operation is not ChangeOperation.UPDATE:
                later_person_changes.add(person_id)
    candidates, skipped = [], []
    for person in roster.persons:
        person_id = person.person_id
        current_members = tuple(member for member in roster.memberships if member.person_id == person_id)
        original_member = original_manual_memberships.get(person_id)
        if current_members:
            if original_member is None:
                continue
            if (current_members != (original_member,) or person_id in later_membership_changes
                    or person_id in later_person_changes):
                skipped.append(f"{person.profile.name}: 이후 가구 소속 또는 대상자 이력이 있어 시작일 정정에서 제외")
                continue
            if original_member.period.starts_on is None or original_member.period.ends_on is not None:
                continue
            enrollments = [item for item in roster.enrollments if item.person_id == person_id]
            if not enrollments:
                continue
            starts_on = min((item.period.starts_on for item in enrollments),
                            key=lambda value: (value is not None, value or date.min))
            if starts_on is None or starts_on < original_member.period.starts_on:
                candidates.append(RepairCandidate(person, starts_on, original_member))
            continue
        if not creation.get(person_id):
            skipped.append(f"{person.profile.name}: 최초 추가 기록에서 소속 누락을 확인할 수 없어 제외")
            continue
        if person_id in later_membership_changes or person_id in later_person_changes:
            skipped.append(f"{person.profile.name}: 이후 가구 소속 또는 대상자 이력이 있어 제외")
            continue
        enrollments = [item for item in roster.enrollments if item.person_id == person_id]
        if not enrollments:
            skipped.append(f"{person.profile.name}: 저장된 식종 가입 시작일이 없어 제외")
            continue
        starts_on = min((item.period.starts_on for item in enrollments),
                        key=lambda value: (value is not None, value or date.min))
        candidates.append(RepairCandidate(person, starts_on))
    return MissingHouseholdContext(roster, snapshot, tuple(candidates), tuple(skipped))


def plan_missing_household_repair(context: MissingHouseholdContext, *, household_id_factory=uuid4) -> MissingHouseholdPlan:
    households = list(context.roster.households)
    memberships = list(context.roster.memberships)
    used_ids = {item.household_id for item in households}
    for item in context.candidates:
        if item.original_membership is not None:
            index = memberships.index(item.original_membership)
            memberships[index] = replace(item.original_membership, period=InclusiveDateRange(item.starts_on))
            continue
        household_id = household_id_factory()
        if household_id in used_ids:
            raise ValueError("새 가구 ID가 중복됐습니다.")
        used_ids.add(household_id)
        households.append(Household(household_id))
        memberships.append(HouseholdMembership(item.person.person_id, household_id, InclusiveDateRange(item.starts_on)))
    return MissingHouseholdPlan(context, replace(context.roster, households=tuple(households), memberships=tuple(memberships)))
