from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Final
from uuid import UUID

from .entitlements import BreakfastDeliveryRule, ClosedFridayMode
from .revisions import (
    AuditChange,
    ChangeOperation,
    HistoricalSnapshot,
    JsonValue,
    RevisionActor,
    RevisionMetadata,
    RevisionRecord,
    RevisionSource,
    freeze_json_object,
    plain_json,
)


BREAKFAST_DELIVERY_POLICY_VERSION: Final = 1
BREAKFAST_DELIVERY_POLICY_KIND: Final = "delivery.breakfast-policy"
BREAKFAST_DELIVERY_POLICY_ID: Final = UUID(
    "bfa57d7d-6264-5a36-a132-8e1b24b5139e"
)
DEFAULT_CLOSED_FRIDAY_MODE: Final = ClosedFridayMode.THURSDAY_WEEKEND_ONLY


@dataclass(frozen=True, slots=True)
class BreakfastDeliveryPolicyChange:
    change_id: UUID
    effective_on: date
    closed_friday_mode: ClosedFridayMode
    recorded_at: datetime

    def __post_init__(self) -> None:
        _validate_uuid(self.change_id, "조식 배송 규칙 변경 ID")
        _validate_date(self.effective_on, "조식 배송 규칙 적용일")
        if not isinstance(self.closed_friday_mode, ClosedFridayMode):
            raise TypeError("금요일 휴무 방식은 ClosedFridayMode여야 합니다.")
        _validate_datetime(self.recorded_at, "조식 배송 규칙 기록 시각")


@dataclass(frozen=True, slots=True)
class BreakfastDeliveryPolicySchedule:
    """Append-only, effective-dated closed-Friday breakfast policy."""

    changes: tuple[BreakfastDeliveryPolicyChange, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        changes = tuple(self.changes)
        if any(
            not isinstance(change, BreakfastDeliveryPolicyChange)
            for change in changes
        ):
            raise TypeError(
                "changes에는 BreakfastDeliveryPolicyChange만 허용됩니다."
            )
        ids = tuple(change.change_id for change in changes)
        if len(set(ids)) != len(ids):
            raise ValueError("조식 배송 규칙 변경 ID는 중복될 수 없습니다.")
        object.__setattr__(self, "changes", changes)

    def effective_change_on(
        self,
        service_on: date,
    ) -> BreakfastDeliveryPolicyChange | None:
        _validate_date(service_on, "조식 서비스일")
        winner = None
        for change in self.changes:
            if change.effective_on <= service_on:
                winner = change
        return winner

    def mode_on(self, service_on: date) -> ClosedFridayMode:
        winner = self.effective_change_on(service_on)
        return (
            DEFAULT_CLOSED_FRIDAY_MODE
            if winner is None
            else winner.closed_friday_mode
        )

    def rule_on(self, service_on: date) -> BreakfastDeliveryRule:
        winner = self.effective_change_on(service_on)
        identity = "default" if winner is None else str(winner.change_id)
        return BreakfastDeliveryRule(
            f"breakfast-delivery-policy-v{BREAKFAST_DELIVERY_POLICY_VERSION}@{identity}",
            self.mode_on(service_on),
        )

    def with_change(
        self,
        change: BreakfastDeliveryPolicyChange,
    ) -> BreakfastDeliveryPolicySchedule:
        if not isinstance(change, BreakfastDeliveryPolicyChange):
            raise TypeError("change는 BreakfastDeliveryPolicyChange여야 합니다.")
        return BreakfastDeliveryPolicySchedule((*self.changes, change))

    def public_manifest(self) -> Mapping[str, JsonValue]:
        state = breakfast_delivery_policy_state(self)
        encoded = json.dumps(
            plain_json(state),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return freeze_json_object(
            {
                "policy_version": BREAKFAST_DELIVERY_POLICY_VERSION,
                "default_closed_friday_mode": DEFAULT_CLOSED_FRIDAY_MODE.value,
                "change_count": len(self.changes),
                "content_sha256": hashlib.sha256(encoded).hexdigest(),
            }
        )


@dataclass(frozen=True, slots=True)
class PreparedBreakfastDeliveryPolicyRevision:
    schedule: BreakfastDeliveryPolicySchedule
    change: BreakfastDeliveryPolicyChange
    record: RevisionRecord


def breakfast_delivery_policy_state(
    schedule: BreakfastDeliveryPolicySchedule,
) -> Mapping[str, JsonValue]:
    if not isinstance(schedule, BreakfastDeliveryPolicySchedule):
        raise TypeError("schedule은 BreakfastDeliveryPolicySchedule이어야 합니다.")
    return freeze_json_object(
        {
            "policy_version": BREAKFAST_DELIVERY_POLICY_VERSION,
            "default_closed_friday_mode": DEFAULT_CLOSED_FRIDAY_MODE.value,
            "changes": tuple(
                {
                    "change_id": str(change.change_id),
                    "effective_on": change.effective_on.isoformat(),
                    "closed_friday_mode": change.closed_friday_mode.value,
                    "recorded_at": change.recorded_at.isoformat(),
                }
                for change in schedule.changes
            ),
        }
    )


def breakfast_delivery_policy_from_state(
    state: Mapping[str, JsonValue] | None,
) -> BreakfastDeliveryPolicySchedule:
    if state is None:
        return BreakfastDeliveryPolicySchedule()
    if not isinstance(state, Mapping):
        raise TypeError("조식 배송 규칙 state는 mapping이어야 합니다.")
    expected = {
        "policy_version",
        "default_closed_friday_mode",
        "changes",
    }
    if set(state.keys()) != expected:
        raise ValueError("조식 배송 규칙 state 필드가 올바르지 않습니다.")
    if state["policy_version"] != BREAKFAST_DELIVERY_POLICY_VERSION:
        raise ValueError(
            "지원하지 않는 조식 배송 규칙 policy version입니다: "
            f"{state['policy_version']}"
        )
    if state["default_closed_friday_mode"] != DEFAULT_CLOSED_FRIDAY_MODE.value:
        raise ValueError("저장된 조식 기본 배송 규칙이 현재 계약과 다릅니다.")
    raw_changes = state["changes"]
    if not isinstance(raw_changes, (list, tuple)):
        raise TypeError("조식 배송 규칙 changes는 배열이어야 합니다.")
    changes = tuple(
        BreakfastDeliveryPolicyChange(
            UUID(_required_text(raw, "change_id")),
            date.fromisoformat(_required_text(raw, "effective_on")),
            ClosedFridayMode(_required_text(raw, "closed_friday_mode")),
            datetime.fromisoformat(_required_text(raw, "recorded_at")),
        )
        for raw in raw_changes
    )
    schedule = BreakfastDeliveryPolicySchedule(changes)
    if plain_json(breakfast_delivery_policy_state(schedule)) != plain_json(state):
        raise ValueError("조식 배송 규칙 state가 canonical 형식이 아닙니다.")
    return schedule


def breakfast_delivery_policy_from_snapshot(
    snapshot: HistoricalSnapshot,
) -> BreakfastDeliveryPolicySchedule:
    if not isinstance(snapshot, HistoricalSnapshot):
        raise TypeError("snapshot은 HistoricalSnapshot이어야 합니다.")
    return breakfast_delivery_policy_from_state(
        snapshot.entity(
            BREAKFAST_DELIVERY_POLICY_KIND,
            BREAKFAST_DELIVERY_POLICY_ID,
        )
    )


def build_breakfast_delivery_policy_revision(
    current_revision_id: UUID,
    stored_state,
    effective_on: date,
    closed_friday_mode: ClosedFridayMode,
    *,
    actor: RevisionActor,
    recorded_at: datetime,
    revision_id: UUID,
    change_id: UUID,
    reason: str = "조식 배송 규칙 저장",
) -> PreparedBreakfastDeliveryPolicyRevision:
    _validate_uuid(current_revision_id, "현재 revision ID")
    _validate_uuid(revision_id, "새 revision ID")
    if revision_id == current_revision_id:
        raise ValueError("새 revision ID는 현재 revision과 달라야 합니다.")
    if not isinstance(actor, RevisionActor):
        raise TypeError("actor는 RevisionActor여야 합니다.")
    _validate_datetime(recorded_at, "조식 배송 규칙 기록 시각")
    schedule = breakfast_delivery_policy_from_state(stored_state)
    change = BreakfastDeliveryPolicyChange(
        change_id,
        effective_on,
        closed_friday_mode,
        recorded_at,
    )
    if any(existing.change_id == change.change_id for existing in schedule.changes):
        raise ValueError("이미 사용한 조식 배송 규칙 변경 ID입니다.")
    updated = schedule.with_change(change)
    after = breakfast_delivery_policy_state(updated)
    audit_change = AuditChange(
        BREAKFAST_DELIVERY_POLICY_KIND,
        BREAKFAST_DELIVERY_POLICY_ID,
        ChangeOperation.INSERT if stored_state is None else ChangeOperation.UPDATE,
        before=stored_state,
        after=after,
    )
    metadata = RevisionMetadata(
        revision_id,
        current_revision_id,
        recorded_at,
        effective_on,
        actor,
        reason,
        RevisionSource.UI,
        decisions={
            "breakfast_delivery_policy_change": {
                "change_id": str(change.change_id),
                "effective_on": change.effective_on.isoformat(),
                "closed_friday_mode": change.closed_friday_mode.value,
            }
        },
        rule_versions={
            "breakfast_delivery_policy_version": (
                BREAKFAST_DELIVERY_POLICY_VERSION
            )
        },
    )
    return PreparedBreakfastDeliveryPolicyRevision(
        updated,
        change,
        RevisionRecord(metadata, (audit_change,)),
    )


def _required_text(value: object, key: str) -> str:
    if not isinstance(value, Mapping):
        raise TypeError("조식 배송 규칙 change는 mapping이어야 합니다.")
    raw = value.get(key)
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"조식 배송 규칙 {key} 값이 비어 있습니다.")
    return raw.strip()


def _validate_uuid(value: object, label: str) -> None:
    if not isinstance(value, UUID):
        raise TypeError(f"{label}는 UUID여야 합니다.")


def _validate_date(value: object, label: str) -> None:
    if isinstance(value, datetime) or not isinstance(value, date):
        raise TypeError(f"{label}은 date여야 합니다.")


def _validate_datetime(value: object, label: str) -> None:
    if not isinstance(value, datetime):
        raise TypeError(f"{label}은 datetime이어야 합니다.")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label}에는 시간대가 있어야 합니다.")
