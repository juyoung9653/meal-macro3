"""Cancel one recorded meal action without rewriting its immutable audit source."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from uuid import UUID

from .projections import (
    ENROLLMENT_KIND, STATUS_EVENT_KIND, enrollment_entity_id, enrollments_from_state,
    roster_from_entity_states, status_event_from_state,
)
from .revisions import (
    AuditChange, ChangeOperation, EntityRef, HistoricalSnapshot,
    RevisionMetadata, RevisionRecord, RevisionSource,
)
from .roster import InclusiveDateRange, Meal
from .status import MealStatus, resolve_status_history

CANCELLATION_KIND = "meal.history_cancellation"
CANCELLATION_DECISION = "meal_history_cancellation"


@dataclass(frozen=True)
class MealHistoryTarget:
    revision_id: UUID
    entity_kind: str
    entity_id: UUID
    period: InclusiveDateRange | None = None

    def state(self):
        return {
            "source_revision_id": str(self.revision_id),
            "entity_kind": self.entity_kind,
            "entity_id": str(self.entity_id),
            "starts_on": self.period.starts_on.isoformat() if self.period and self.period.starts_on else None,
            "ends_on": self.period.ends_on.isoformat() if self.period and self.period.ends_on else None,
        }

    @classmethod
    def from_state(cls, state):
        kind = state["entity_kind"]
        period = None if kind == STATUS_EVENT_KIND else InclusiveDateRange(
            date.fromisoformat(state["starts_on"]) if state["starts_on"] else None,
            date.fromisoformat(state["ends_on"]) if state["ends_on"] else None,
        )
        return cls(UUID(state["source_revision_id"]), kind, UUID(state["entity_id"]), period)


def cancelled_meal_history_targets(revisions):
    """Replay cancellation markers so a later snapshot restore also restores visibility."""
    markers = {}
    for record in revisions:
        if "workspace_branch" in record.metadata.decisions:
            continue
        for change in record.changes:
            if change.entity_kind == CANCELLATION_KIND:
                if change.after is None:
                    markers.pop(change.entity_id, None)
                else:
                    markers[change.entity_id] = MealHistoryTarget.from_state(change.after)
    return frozenset(markers.values())


def build_meal_history_cancellation(current: HistoricalSnapshot, revisions, target, *,
                                    actor, recorded_at, revision_id):
    if not revisions or revisions[-1].metadata.revision_id != current.revision_id:
        raise ValueError("최신 변경 이력을 다시 불러온 뒤 취소해주세요.")
    cancelled = cancelled_meal_history_targets(revisions)
    if target in cancelled:
        raise ValueError("이미 취소한 식종 변경 이력입니다.")
    source = next((r for r in revisions if r.metadata.revision_id == target.revision_id), None)
    if source is None or "workspace_branch" in source.metadata.decisions:
        raise ValueError("선택한 식종 변경 이력을 찾을 수 없습니다.")
    change = next((c for c in source.changes if c.entity_kind == target.entity_kind
                   and c.entity_id == target.entity_id), None)
    if change is None or change.after is None:
        raise ValueError("취소할 수 있는 식종 변경 이력이 아닙니다.")
    entities = dict(current.entities)
    changes = []
    enrollment_change = None
    selected_event = None
    if target.entity_kind == STATUS_EVENT_KIND:
        selected_event = status_event_from_state(target.entity_id, change.after)
        person_id, meal, period = selected_event.person_id, selected_event.meal, selected_event.period
        reference = EntityRef(STATUS_EVENT_KIND, target.entity_id)
        if entities.get(reference) != change.after:
            raise ValueError("선택한 상태 기록이 변경되었거나 현재 적용되지 않습니다.")
        changes.append(AuditChange(STATUS_EVENT_KIND, target.entity_id, ChangeOperation.DELETE, change.after, None))
        if selected_event.status is MealStatus.DELIVERY:
            enrollment_change = next((c for c in source.changes
                if c.entity_kind == ENROLLMENT_KIND
                and c.entity_id == enrollment_entity_id(person_id, meal)), None)
    elif target.entity_kind == ENROLLMENT_KIND:
        added = source.metadata.decisions.get("roster_row_change")
        if (source.metadata.source is not RevisionSource.UI or not isinstance(added, Mapping)
                or added.get("operation") != "add" or change.operation is not ChangeOperation.INSERT
                or change.after["person_id"] != added.get("person_id")):
            raise ValueError("행 추가로 등록한 시작 이력만 취소할 수 있습니다.")
        enrollment = next((e for e in enrollments_from_state(change.entity_id, change.after)
                           if e.period == target.period), None)
        if enrollment is None:
            raise ValueError("선택한 가입 기간을 찾을 수 없습니다.")
        person_id, meal, period = enrollment.person_id, enrollment.meal, enrollment.period
        if any(c.entity_kind == STATUS_EVENT_KIND and c.after is not None
               and c.after["person_id"] == str(person_id) and c.after["meal"] == meal.value
               and MealHistoryTarget(source.metadata.revision_id, STATUS_EVENT_KIND, c.entity_id) not in cancelled
               for c in source.changes):
            raise ValueError("선택한 시작 이력에 연결된 상태 기록을 먼저 확인해주세요.")
        enrollment_change = change
    else:
        raise ValueError("취소할 수 있는 식종 변경 이력이 아닙니다.")
    roster = roster_from_entity_states(current.revision_id, entities)
    roster.person(person_id)
    if enrollment_change is not None:
        reference = EntityRef(ENROLLMENT_KIND, enrollment_change.entity_id)
        if entities.get(reference) != enrollment_change.after:
            raise ValueError("이후 가입 기간이 변경되어 이 시작 기록만 취소할 수 없습니다. 이후 이력을 먼저 확인해주세요.")
        after = enrollment_change.before
        if target.entity_kind == ENROLLMENT_KIND:
            remaining = [p for p in enrollment_change.after["periods"]
                         if p != {"starts_on": period.starts_on.isoformat() if period.starts_on else None,
                                  "ends_on": period.ends_on.isoformat() if period.ends_on else None}]
            after = {**enrollment_change.after, "periods": remaining}
        # Canonical roster entities include an empty enrollment state for each living person/meal.
        if after is None:
            after = {**enrollment_change.after, "periods": []}
        changes.append(AuditChange(ENROLLMENT_KIND, enrollment_change.entity_id,
                                   ChangeOperation.DELETE if after is None else ChangeOperation.UPDATE,
                                   enrollment_change.after, after))
        retained = () if after is None else enrollments_from_state(enrollment_change.entity_id, after)
        original = enrollments_from_state(enrollment_change.entity_id, enrollment_change.after)
        for ref, state in entities.items():
            if ref.entity_kind != STATUS_EVENT_KIND or ref.entity_id == target.entity_id:
                continue
            event = status_event_from_state(ref.entity_id, state)
            if event.person_id != person_id or event.meal is not meal:
                continue
            # A remaining action must not lose any enrollment support it currently has.
            for item in original:
                intersection = _intersection(item.period, event.period)
                if intersection is not None and not _covered(intersection, retained):
                    raise ValueError("이 시작 기록의 가입 기간에 다른 상태 기록이 남아 있어 취소할 수 없습니다. 해당 식종의 다른 이력을 먼저 확인해주세요.")
    marker = target.state()
    changes.append(AuditChange(CANCELLATION_KIND, revision_id, ChangeOperation.INSERT, None, marker))
    for item in changes:
        ref = EntityRef(item.entity_kind, item.entity_id)
        if item.after is None:
            entities.pop(ref, None)
        else:
            entities[ref] = item.after
    category = "시작" if selected_event is None or selected_event.status is MealStatus.DELIVERY else selected_event.status.value
    record = RevisionRecord(RevisionMetadata(
        revision_id, current.revision_id, recorded_at, period.starts_on, actor,
        f"식종 변경 취소 · {roster.person(person_id).profile.name} · {meal.value} {category}",
        RevisionSource.UI, decisions={CANCELLATION_DECISION: marker},
    ), tuple(changes))
    result = roster_from_entity_states(revision_id, entities)
    _validate_shared_status(current, roster, result, entities, person_id, meal)
    # Run existing shared-household rules, but never accept an expansion of this one-item action.
    from .household_shared import expand_household_shared_revision
    expanded = expand_household_shared_revision(current, record)
    key = lambda item: EntityRef(item.entity_kind, item.entity_id)
    if {key(c): c for c in expanded.changes} != {key(c): c for c in record.changes}:
        raise ValueError("가구 공용 설정으로 다른 가구원의 가입도 함께 변경되어 이 한 건만 취소할 수 없습니다. 공용 예외 또는 공용 설정을 먼저 확인해주세요.")
    return result, record


def validate_meal_history_cancellation(current, revisions, record):
    target = MealHistoryTarget.from_state(record.metadata.decisions[CANCELLATION_DECISION])
    _, expected = build_meal_history_cancellation(
        current, revisions, target, actor=record.metadata.actor,
        recorded_at=record.metadata.recorded_at, revision_id=record.metadata.revision_id,
    )
    if record != expected:
        raise ValueError("선택한 식종 변경 한 건의 취소 내용과 저장 내용이 다릅니다.")


def _intersection(first, second):
    start = max(first.starts_on or date.min, second.starts_on or date.min)
    end = min(first.ends_on or date.max, second.ends_on or date.max)
    return None if start > end else (start, end)


def _covered(span, enrollments):
    cursor, end = span
    for enrollment in sorted(enrollments, key=lambda e: e.period.starts_on or date.min):
        start, stop = enrollment.period.starts_on or date.min, enrollment.period.ends_on or date.max
        if stop < cursor:
            continue
        if start > cursor:
            return False
        if stop >= end:
            return True
        cursor = stop + timedelta(days=1)
    return False



def _validate_shared_status(current, before, after, after_entities, person_id, meal):
    """Check all effective boundaries; deleting an event must not split a shared status."""
    from .household_shared import (
        HOUSEHOLD_SHARED_ID, HOUSEHOLD_SHARED_KIND,
        shared_columns_from_state, shared_exceptions_from_state,
    )
    setting = current.entity(HOUSEHOLD_SHARED_KIND, HOUSEHOLD_SHARED_ID)
    key = "status:" + meal.name.lower()
    exceptions = shared_exceptions_from_state(setting)
    if key not in shared_columns_from_state(setting) or (person_id, key) in exceptions:
        return
    grouped = []
    days = {date.min}
    for states in (current.entities, after_entities):
        events = {}
        for ref, state in states.items():
            if ref.entity_kind != STATUS_EVENT_KIND:
                continue
            event = status_event_from_state(ref.entity_id, state)
            if event.meal is meal:
                events.setdefault(event.person_id, []).append(event)
                _add_boundaries(days, event.period)
        grouped.append({pid: resolve_status_history(pid, meal, tuple(items)) for pid, items in events.items()})
    for roster in (before, after):
        for membership in roster.memberships:
            _add_boundaries(days, membership.period)
        for enrollment in roster.enrollments:
            if enrollment.meal is meal:
                _add_boundaries(days, enrollment.period)

    def value(roster, histories, pid, day):
        if meal not in roster.meals_for(pid, on=day):
            return None
        history = histories.get(pid)
        return history.status_on(day) if history is not None else MealStatus.DELIVERY

    for day in sorted(days):
        changed = value(after, grouped[1], person_id, day)
        if changed == value(before, grouped[0], person_id, day):
            continue
        household = after.household_for(person_id, on=day)
        if household is None:
            continue
        for member in after.household_members(household.household_id, on=day):
            if member.person_id == person_id or (member.person_id, key) in exceptions:
                continue
            if changed != value(after, grouped[1], member.person_id, day):
                raise ValueError(
                    f"{day.isoformat()}부터 같은 가구의 {meal.value} 공용 상태가 달라져 이 한 건만 취소할 수 없습니다. "
                    "공용 예외 또는 공용 설정을 먼저 확인해주세요."
                )


def _add_boundaries(days, period):
    days.add(period.starts_on or date.min)
    if period.ends_on is not None and period.ends_on < date.max:
        days.add(period.ends_on + timedelta(days=1))
