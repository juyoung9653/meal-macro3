"""Revision for meal-specific source roster delivery sequence edits."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from uuid import UUID

from .projections import roster_audit_changes
from .revisions import RevisionActor, RevisionMetadata, RevisionRecord, RevisionSource
from .roster import DeliveryAssignment, Meal, RosterSnapshot


@dataclass(frozen=True, slots=True)
class PreparedSequenceRevision:
    snapshot: RosterSnapshot
    record: RevisionRecord


def build_sequence_revision(
    roster: RosterSnapshot,
    person_id: UUID,
    values: Mapping[Meal, str],
    *,
    actor: RevisionActor,
    recorded_at: datetime,
    revision_id: UUID,
) -> PreparedSequenceRevision:
    """Replace changed effective sequences without disturbing driver assignments."""

    return build_scoped_sequence_revision(
        roster, {(person_id, meal): value for meal, value in values.items()},
        actor=actor, recorded_at=recorded_at, revision_id=revision_id,
    )


def build_scoped_sequence_revision(
    roster: RosterSnapshot,
    values: Mapping[tuple[UUID, Meal], str],
    *,
    actor: RevisionActor,
    recorded_at: datetime,
    revision_id: UUID,
) -> PreparedSequenceRevision:
    """Persist only the explicit person/meal pairs together in one revision."""
    if not isinstance(actor, RevisionActor):
        raise TypeError("actor는 RevisionActor여야 합니다.")
    if not isinstance(recorded_at, datetime) or recorded_at.tzinfo is None or recorded_at.utcoffset() is None:
        raise ValueError("기록 시각에는 시간대가 있어야 합니다.")
    if not isinstance(revision_id, UUID) or revision_id == roster.revision_id:
        raise ValueError("새 revision ID가 올바르지 않습니다.")
    changed: dict[tuple[UUID, Meal], str] = {}
    for (person_id, meal), value in values.items():
        roster.person(person_id)
        if not isinstance(meal, Meal) or not isinstance(value, str):
            raise TypeError("식종별 순서 값이 올바르지 않습니다.")
        normalized = value.strip()
        if normalized == (roster.resolve_assignment(person_id, meal).sequence or ""):
            continue
        if "\n" in normalized or "\r" in normalized:
            raise ValueError(f"{meal.value} 순서는 한 줄로 입력해주세요.")
        changed[(person_id, meal)] = normalized
    if not changed:
        raise ValueError("변경된 배송순서가 없습니다.")

    assignments = {(item.person_id, item.meal): item for item in roster.assignments}
    # A blank meal sequence must not reveal an older person-wide sequence.
    # Move that common value to unaffected meals before removing it.
    for person_id in {person_id for person_id, _ in changed}:
        common_key = (person_id, None)
        common = assignments.get(common_key)
        if common is None or common.sequence is None or not any(
            changed.get((person_id, meal)) == "" for meal in Meal
        ):
            continue
        for meal in Meal:
            key = (person_id, meal)
            if key in changed:
                continue
            existing = assignments.get(key)
            if existing is None:
                assignments[key] = DeliveryAssignment(person_id, meal, sequence=common.sequence)
            elif existing.sequence is None:
                assignments[key] = replace(existing, sequence=common.sequence)
        if common.driver is None:
            del assignments[common_key]
        else:
            assignments[common_key] = replace(common, sequence=None)
    for (person_id, meal), sequence in changed.items():
        key = (person_id, meal)
        existing = assignments.get(key)
        if existing is None:
            if sequence:
                assignments[key] = DeliveryAssignment(person_id, meal, sequence=sequence)
        elif not sequence and existing.driver is None:
            del assignments[key]
        else:
            assignments[key] = replace(existing, sequence=sequence or None)
    snapshot = replace(roster, revision_id=revision_id, assignments=tuple(assignments.values()))
    people = {person_id for person_id, _ in changed}
    decisions = (
        {"person_id": str(next(iter(people))), "meals": [meal.value for _, meal in changed]}
        if len(people) == 1 else
        {"targets": [{"person_id": str(person_id), "meal": meal.value} for person_id, meal in changed]}
    )
    record = RevisionRecord(
        RevisionMetadata(
            revision_id=revision_id,
            parent_revision_id=roster.revision_id,
            recorded_at=recorded_at,
            effective_on=None,
            actor=actor,
            reason="배송순서 수정",
            source=RevisionSource.UI,
            decisions={"sequence_edit": decisions},
            rule_versions={"sequence_edit_policy_version": 1},
        ),
        roster_audit_changes(roster, snapshot),
    )
    return PreparedSequenceRevision(snapshot, record)
