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

    roster.person(person_id)
    if not isinstance(actor, RevisionActor):
        raise TypeError("actor는 RevisionActor여야 합니다.")
    if not isinstance(recorded_at, datetime) or recorded_at.tzinfo is None or recorded_at.utcoffset() is None:
        raise ValueError("기록 시각에는 시간대가 있어야 합니다.")
    if not isinstance(revision_id, UUID) or revision_id == roster.revision_id:
        raise ValueError("새 revision ID가 올바르지 않습니다.")
    changed: dict[Meal, str] = {}
    for meal, value in values.items():
        if not isinstance(meal, Meal) or not isinstance(value, str):
            raise TypeError("식종별 순서 값이 올바르지 않습니다.")
        normalized = value.strip()
        if normalized == (roster.resolve_assignment(person_id, meal).sequence or ""):
            continue
        if not normalized:
            raise ValueError(f"{meal.value} 순서를 비울 수 없습니다. 기존 순서는 그대로 두거나 새 순서를 입력해주세요.")
        if "\n" in normalized or "\r" in normalized:
            raise ValueError(f"{meal.value} 순서는 한 줄로 입력해주세요.")
        changed[meal] = normalized
    if not changed:
        raise ValueError("변경된 배송순서가 없습니다.")

    assignments = {(item.person_id, item.meal): item for item in roster.assignments}
    for meal, sequence in changed.items():
        key = (person_id, meal)
        existing = assignments.get(key)
        assignments[key] = (
            DeliveryAssignment(person_id, meal, sequence=sequence)
            if existing is None else replace(existing, sequence=sequence)
        )
    snapshot = replace(roster, revision_id=revision_id, assignments=tuple(assignments.values()))
    record = RevisionRecord(
        RevisionMetadata(
            revision_id=revision_id,
            parent_revision_id=roster.revision_id,
            recorded_at=recorded_at,
            effective_on=None,
            actor=actor,
            reason="배송순서 수정",
            source=RevisionSource.UI,
            decisions={"sequence_edit": {"person_id": str(person_id), "meals": [meal.value for meal in changed]}},
            rule_versions={"sequence_edit_policy_version": 1},
        ),
        roster_audit_changes(roster, snapshot),
    )
    return PreparedSequenceRevision(snapshot, record)
