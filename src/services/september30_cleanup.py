"""Reversible, audited removal of meals with a historical September 30 stop."""
from uuid import uuid5

from src.domain.meal_history_cancellation import CANCELLATION_KIND, MealHistoryTarget
from src.domain.projections import ENROLLMENT_KIND, ASSIGNMENT_KIND, PERSON_KIND, STATUS_EVENT_KIND, enrollments_from_state, status_event_from_state
from src.domain.revisions import AuditChange, ChangeOperation, EntityRef, RevisionMetadata, RevisionRecord, RevisionSource, plain_json
from src.domain.roster import Meal
from src.domain.status import MealStatus

RESET_DECISION = "september30_meal_reset"


def find_targets(records):
    targets = set()
    for record in records:
        for change in record.changes:
            if change.entity_kind != STATUS_EVENT_KIND:
                continue
            for state in (change.before, change.after):
                if state is None:
                    continue
                event = status_event_from_state(change.entity_id, state)
                start = event.period.starts_on
                if event.status is MealStatus.STOPPED and start and (start.month, start.day) == (9, 30):
                    targets.add((str(event.person_id), event.meal.value))
    return frozenset(targets)


def build_cleanup(snapshot, records, *, actor, recorded_at, revision_id):
    if not records or records[-1].metadata.revision_id != snapshot.revision_id:
        raise ValueError("명단이 변경되었습니다. 정리 도구를 다시 실행해주세요.")
    targets = find_targets(records)
    changes = []
    for ref, state in snapshot.entities.items():
        after = plain_json(state)
        pair = (state.get("person_id"), state.get("meal"))
        if pair in targets:
            if ref.entity_kind == STATUS_EVENT_KIND:
                after = None
            elif ref.entity_kind == ENROLLMENT_KIND:
                after["periods"] = []
            elif ref.entity_kind == ASSIGNMENT_KIND:
                after.update(driver=None, sequence=None)
        elif ref.entity_kind == PERSON_KIND and (str(ref.entity_id), Meal.LUNCH.value) in targets:
            after.pop("lunch_period", None)
        if plain_json(state) != after:
            changes.append(AuditChange(ref.entity_kind, ref.entity_id,
                ChangeOperation.DELETE if after is None else ChangeOperation.UPDATE, state, after))
    # Mark original starts/stops hidden in meal-history views without destroying
    # their source records. Snapshot restore naturally restores these markers too.
    existing = {MealHistoryTarget.from_state(state) for ref, state in snapshot.entities.items() if ref.entity_kind == CANCELLATION_KIND}
    for record in records:
        if "workspace_branch" in record.metadata.decisions or "snapshot_restore_target" in record.metadata.decisions:
            continue
        for change in record.changes:
            state = change.after
            if state is None or (state.get("person_id"), state.get("meal")) not in targets:
                continue
            if change.entity_kind == STATUS_EVENT_KIND:
                periods = (None,)
            elif change.entity_kind == ENROLLMENT_KIND:
                periods = tuple(item.period for item in enrollments_from_state(change.entity_id, state))
            else:
                continue
            for period in periods:
                target = MealHistoryTarget(record.metadata.revision_id, change.entity_kind, change.entity_id, period)
                marker = target.state()
                if target in existing:
                    continue
                existing.add(target)
                marker_id = uuid5(revision_id, repr(marker))
                changes.append(AuditChange(CANCELLATION_KIND, marker_id, ChangeOperation.INSERT, None, marker))
    if not changes:
        return None
    return RevisionRecord(RevisionMetadata(
        revision_id, snapshot.revision_id, recorded_at, None, actor,
        f"9월 30일 중지 대상 식종 정리 · {len(targets)}건", RevisionSource.UI,
        decisions={RESET_DECISION: {"targets": [list(pair) for pair in sorted(targets)]}},
    ), tuple(changes))


def validate_cleanup(snapshot, records, record):
    expected = build_cleanup(snapshot, records, actor=record.metadata.actor,
        recorded_at=record.metadata.recorded_at, revision_id=record.metadata.revision_id)
    if record != expected:
        raise ValueError("9월 30일 중지 정리 대상과 저장 내용이 다릅니다. 정리 도구를 다시 실행해주세요.")


def hidden_history_changes(records):
    """Hide old affected meal rows, keeping later registrations and restored history."""
    from src.domain.revision_visibility import effective_history_revisions
    earlier, hidden = [], set()
    for record in effective_history_revisions(records):
        earlier.append(record)
        decision = record.metadata.decisions.get(RESET_DECISION)
        if decision is None:
            continue
        targets = {tuple(pair) for pair in decision["targets"]}
        for source in earlier:
            for change in source.changes:
                state = change.after or change.before or {}
                if change.entity_kind in (STATUS_EVENT_KIND, ENROLLMENT_KIND, ASSIGNMENT_KIND) and (state.get("person_id"), state.get("meal")) in targets:
                    hidden.add((source.metadata.revision_id, EntityRef(change.entity_kind, change.entity_id)))
    return hidden
