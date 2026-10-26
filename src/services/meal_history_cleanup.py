"""Plan and audit redundant delivery/stop removal without deleting enrollments."""
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from uuid import uuid5

from src.domain.meal_history_cancellation import (
    CANCELLATION_KIND, MealHistoryTarget, _validate_shared_status,
)
from src.domain.projections import STATUS_EVENT_KIND, roster_from_entity_states, status_event_from_state
from src.domain.revisions import AuditChange, ChangeOperation, EntityRef, RevisionMetadata, RevisionRecord, RevisionSource
from src.domain.status import MealStatus, StatusEvent

CLEANUP_DECISION = "meal_history_deduplication"


@dataclass(frozen=True)
class DuplicateMealEvent:
    removed: StatusEvent
    retained: StatusEvent
    reason: str


def plan_meal_history_cleanup(snapshot, revisions):
    """Same start: latest registration; consecutive starts: earliest covering event.

    Finite records that extend coverage are kept: deleting them would lose dates.
    Different people/meals and intervening other states always break a run.
    """
    if not revisions or revisions[-1].metadata.revision_id != snapshot.revision_id:
        raise ValueError("현재 명단과 변경 이력의 변경번호가 다릅니다.")
    positions = {}
    for revision_index, record in enumerate(revisions):
        if "workspace_branch" in record.metadata.decisions:
            continue
        for change_index, change in enumerate(record.changes):
            if change.entity_kind == STATUS_EVENT_KIND and change.after is not None:
                positions[change.entity_id] = (revision_index, change_index)
    groups = defaultdict(list)
    for ref, state in snapshot.entities.items():
        if ref.entity_kind == STATUS_EVENT_KIND:
            event = status_event_from_state(ref.entity_id, state)
            groups[event.person_id, event.meal].append(event)
    removed = {}
    for events in groups.values():
        same_day = defaultdict(list)
        for event in events:
            if event.status in (MealStatus.STOPPED, MealStatus.DELIVERY) and event.period.starts_on:
                same_day[event.status, event.period.starts_on].append(event)
        for duplicates in same_day.values():
            latest = max(duplicates, key=lambda event: (event.recorded_at, positions.get(event.event_id, (-1, -1))))
            for event in duplicates:
                if event.event_id != latest.event_id:
                    removed[event.event_id] = DuplicateMealEvent(event, latest, "같은 시작일 · 최신 등록 유지")
        ordered = sorted((event for event in events if event.event_id not in removed),
            key=lambda event: (event.period.starts_on or date.min, event.recorded_at,
                               positions.get(event.event_id, (-1, -1))))
        previous = None
        for event in ordered:
            if (previous is not None and event.status is previous.status
                    and event.status in (MealStatus.STOPPED, MealStatus.DELIVERY)
                    and previous.period.starts_on is not None and event.period.starts_on is not None
                    and previous.period.starts_on < event.period.starts_on
                    and previous.period.contains(event.period.starts_on)
                    and (previous.period.ends_on or date.max) >= (event.period.ends_on or date.max)):
                removed[event.event_id] = DuplicateMealEvent(event, previous, "연속된 같은 상태 · 최초 시작일 유지")
            else:
                previous = event
    # Same-day winners can themselves be redundant with an earlier start.
    result = []
    for item in removed.values():
        retained = item.retained
        while retained.event_id in removed:
            retained = removed[retained.event_id].retained
        result.append(DuplicateMealEvent(item.removed, retained, item.reason))
    return tuple(sorted(result, key=lambda item: (str(item.removed.person_id), item.removed.meal.value,
        item.removed.period.starts_on or date.min, item.removed.recorded_at, str(item.removed.event_id))))


def build_meal_history_cleanup(snapshot, revisions, *, actor, recorded_at, revision_id):
    plan = plan_meal_history_cleanup(snapshot, revisions)
    if not plan:
        return None
    changes = []
    for item in plan:
        event = item.removed
        state = snapshot.entity(STATUS_EVENT_KIND, event.event_id)
        changes.append(AuditChange(STATUS_EVENT_KIND, event.event_id, ChangeOperation.DELETE, state, None))
        # Hide every affirmative audit version of this event in personal history,
        # while retaining the immutable ledger and all enrollment records.
        for record in revisions:
            if "workspace_branch" in record.metadata.decisions:
                continue
            if any(change.entity_kind == STATUS_EVENT_KIND and change.entity_id == event.event_id
                   and change.after is not None for change in record.changes):
                target = MealHistoryTarget(record.metadata.revision_id, STATUS_EVENT_KIND, event.event_id)
                marker_id = uuid5(revision_id, f"{target.revision_id}:{target.entity_id}")
                changes.append(AuditChange(CANCELLATION_KIND, marker_id, ChangeOperation.INSERT, None, target.state()))
    entities = dict(snapshot.entities)
    for change in changes:
        ref = EntityRef(change.entity_kind, change.entity_id)
        if change.after is None:
            entities.pop(ref, None)
        else:
            entities[ref] = change.after
    before = roster_from_entity_states(snapshot.revision_id, snapshot.entities)
    after = roster_from_entity_states(revision_id, entities)
    for person_id, meal in {(item.removed.person_id, item.removed.meal) for item in plan}:
        _validate_shared_status(snapshot, before, after, entities, person_id, meal)
    return RevisionRecord(RevisionMetadata(
        revision_id, snapshot.revision_id, recorded_at, None, actor,
        f"중복 중지·배송 이력 정리 · {len(plan)}건", RevisionSource.UI,
        decisions={CLEANUP_DECISION: {"removed_event_ids": [str(item.removed.event_id) for item in plan]}},
    ), tuple(changes))


def validate_meal_history_cleanup(snapshot, revisions, record):
    expected = build_meal_history_cleanup(snapshot, revisions, actor=record.metadata.actor,
        recorded_at=record.metadata.recorded_at, revision_id=record.metadata.revision_id)
    if record != expected:
        raise ValueError("중복 식종 이력 정리 대상과 저장 내용이 다릅니다.")
