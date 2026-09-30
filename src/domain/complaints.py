"""Immutable, append-only records of complaints received about a person."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from .intake import IntakeChannel
from .revisions import (
    AuditChange,
    ChangeOperation,
    HistoricalSnapshot,
    RevisionActor,
    RevisionMetadata,
    RevisionRecord,
    RevisionSource,
)
from .roster import Meal, RosterSnapshot

COMPLAINT_KIND = "complaint"
COMPLAINT_POLICY_VERSION = 1
MAX_COMPLAINT_CONTENT_LENGTH = 32_767


@dataclass(frozen=True, slots=True)
class Complaint:
    complaint_id: UUID
    person_id: UUID
    received_at: datetime
    channel: IntakeChannel
    content: str
    meal: Meal | None = None
    meals: tuple[Meal, ...] = ()

    def __post_init__(self) -> None:
        _require_uuid(self.complaint_id, "민원 ID")
        _require_uuid(self.person_id, "사람 ID")
        _require_aware_datetime(self.received_at, "접수 시각")
        if not isinstance(self.channel, IntakeChannel):
            raise TypeError("접수 경로는 IntakeChannel이어야 합니다.")
        if not isinstance(self.content, str):
            raise TypeError("민원 내용은 문자열이어야 합니다.")
        content = self.content.strip()
        if not content:
            raise ValueError("민원 내용은 비어 있을 수 없습니다.")
        if len(content) > MAX_COMPLAINT_CONTENT_LENGTH:
            raise ValueError(f"민원 내용은 {MAX_COMPLAINT_CONTENT_LENGTH:,}자 이하여야 합니다.")
        object.__setattr__(self, "content", content)
        if self.meal is not None and not isinstance(self.meal, Meal):
            raise TypeError("식종은 Meal 또는 None이어야 합니다.")
        meals = tuple(self.meals)
        if any(not isinstance(selected, Meal) for selected in meals):
            raise TypeError("식종 목록은 Meal 목록이어야 합니다.")
        if not meals and self.meal is not None:
            meals = (self.meal,)
        if len(set(meals)) != len(meals):
            raise ValueError("식종은 중복 선택할 수 없습니다.")
        canonical_meals = tuple(meal for meal in Meal if meal in meals)
        if meals != canonical_meals:
            raise ValueError("식종은 조식, 중식, 석식 순서여야 합니다.")
        if self.meal is not None and canonical_meals != (self.meal,):
            raise ValueError("단일 식종과 식종 목록이 일치하지 않습니다.")
        object.__setattr__(self, "meals", canonical_meals)
        object.__setattr__(
            self, "meal", canonical_meals[0] if len(canonical_meals) == 1 else None
        )


def complaint_state(complaint: Complaint) -> dict[str, object]:
    """Translate a complaint into the literal JSON state held by the ledger."""

    if not isinstance(complaint, Complaint):
        raise TypeError("complaint는 Complaint이어야 합니다.")
    state: dict[str, object] = {
        "person_id": str(complaint.person_id),
        "received_at": complaint.received_at.isoformat(),
        "channel": complaint.channel.value,
        "content": complaint.content,
    }
    if len(complaint.meals) > 1:
        state["meals"] = [meal.value for meal in complaint.meals]
    else:
        state["meal"] = None if complaint.meal is None else complaint.meal.value
    return state


def complaints_from_snapshot(snapshot: HistoricalSnapshot) -> tuple[Complaint, ...]:
    if not isinstance(snapshot, HistoricalSnapshot):
        raise TypeError("snapshot은 HistoricalSnapshot이어야 합니다.")
    complaints = [
        _complaint_from_state(reference.entity_id, state)
        for reference, state in snapshot.entities.items()
        if reference.entity_kind == COMPLAINT_KIND
    ]
    return tuple(sorted(complaints, key=lambda complaint: (complaint.received_at, str(complaint.complaint_id))))


def build_complaint_revision(
    roster: RosterSnapshot,
    complaint: Complaint,
    *,
    actor: RevisionActor,
    recorded_at: datetime,
    revision_id: UUID,
    reason: str = "민원 접수",
) -> RevisionRecord:
    """Build the one insert-only revision which records a received complaint."""

    return build_complaints_revision(
        roster, (complaint,), actor=actor, recorded_at=recorded_at,
        revision_id=revision_id, reason=reason,
    )


def build_complaints_revision(
    roster: RosterSnapshot,
    complaints: tuple[Complaint, ...],
    *,
    actor: RevisionActor,
    recorded_at: datetime,
    revision_id: UUID,
    reason: str = "민원 접수",
) -> RevisionRecord:
    """Build one atomic, insert-only revision for one or more people."""

    if not isinstance(roster, RosterSnapshot):
        raise TypeError("roster는 RosterSnapshot이어야 합니다.")
    complaints = tuple(complaints)
    if not complaints or any(not isinstance(item, Complaint) for item in complaints):
        raise ValueError("하나 이상의 Complaint가 필요합니다.")
    if len({item.complaint_id for item in complaints}) != len(complaints):
        raise ValueError("민원 ID는 중복될 수 없습니다.")
    if len({item.person_id for item in complaints}) != len(complaints):
        raise ValueError("한 사람에게 같은 민원을 중복 접수할 수 없습니다.")
    for complaint in complaints:
        roster.person(complaint.person_id)
    if not isinstance(actor, RevisionActor):
        raise TypeError("actor는 RevisionActor이어야 합니다.")
    _require_aware_datetime(recorded_at, "recorded_at")
    _require_uuid(revision_id, "revision ID")
    if revision_id == roster.revision_id:
        raise ValueError("새 revision ID는 현재 revision과 달라야 합니다.")
    return RevisionRecord(
        RevisionMetadata(
            revision_id=revision_id,
            parent_revision_id=roster.revision_id,
            recorded_at=recorded_at,
            effective_on=None,
            actor=actor,
            reason=reason,
            source=RevisionSource.UI,
            decisions={"complaint_policy_version": COMPLAINT_POLICY_VERSION},
            rule_versions={"complaint_policy_version": COMPLAINT_POLICY_VERSION},
        ),
        tuple(
            AuditChange(COMPLAINT_KIND, complaint.complaint_id, ChangeOperation.INSERT, None, complaint_state(complaint))
            for complaint in complaints
        ),
    )


def _complaint_from_state(complaint_id: UUID, state: Mapping[str, object]) -> Complaint:
    if not isinstance(state, Mapping):
        raise TypeError("민원 상태는 mapping이어야 합니다.")
    try:
        person_id = UUID(_required_state_text(state, "person_id"))
        received_at = datetime.fromisoformat(_required_state_text(state, "received_at"))
        channel = IntakeChannel(_required_state_text(state, "channel"))
        content = _required_state_text(state, "content")
        if "meals" in state:
            raw_meals = state["meals"]
            if not isinstance(raw_meals, (list, tuple)) or any(not isinstance(item, str) for item in raw_meals):
                raise TypeError("민원 meals는 문자열 목록이어야 합니다.")
            meals = tuple(Meal(item) for item in raw_meals)
            return Complaint(complaint_id, person_id, received_at, channel, content, meals=meals)
        meal_text = state.get("meal")
        meal = None if meal_text is None else Meal(_required_state_text(state, "meal"))
    except (TypeError, ValueError) as error:
        raise ValueError("저장된 민원 상태가 올바르지 않습니다.") from error
    return Complaint(complaint_id, person_id, received_at, channel, content, meal)


def _required_state_text(state, key: str) -> str:
    value = state.get(key)
    if not isinstance(value, str):
        raise TypeError(f"민원 {key}은 문자열이어야 합니다.")
    return value


def _require_uuid(value: object, label: str) -> None:
    if not isinstance(value, UUID):
        raise TypeError(f"{label}는 UUID여야 합니다.")


def _require_aware_datetime(value: object, label: str) -> None:
    if not isinstance(value, datetime):
        raise TypeError(f"{label}은 datetime이어야 합니다.")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label}에는 시간대가 있어야 합니다.")
