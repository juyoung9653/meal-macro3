"""Revision-backed delivery-sequence editing mode for one operating database."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from .revisions import (
    AuditChange,
    ChangeOperation,
    HistoricalSnapshot,
    JsonValue,
    RevisionActor,
    RevisionMetadata,
    RevisionRecord,
    RevisionSource,
)

SEQUENCE_MODE_SETTING_KIND = "sequence_mode_setting"
SEQUENCE_MODE_SETTING_ID = UUID("7fe11091-10a0-52d4-971b-713fb3e963bd")


class SequenceMode(StrEnum):
    SEPARATE = "separate"
    UNIFIED = "unified"


@dataclass(frozen=True, slots=True)
class PreparedSequenceModeRevision:
    mode: SequenceMode
    record: RevisionRecord


def sequence_mode_from_state(
    state: Mapping[str, JsonValue] | None,
) -> SequenceMode:
    """Return the stored mode, defaulting older databases to separate editing."""

    if state is None:
        return SequenceMode.SEPARATE
    if not isinstance(state, Mapping):
        raise TypeError("배송순서 입력 방식 설정은 mapping이어야 합니다.")
    value = state.get("mode")
    if not isinstance(value, str):
        raise TypeError("배송순서 입력 방식은 문자열이어야 합니다.")
    try:
        return SequenceMode(value)
    except ValueError as error:
        raise ValueError("지원하지 않는 배송순서 입력 방식입니다.") from error


def sequence_mode_from_snapshot(snapshot: HistoricalSnapshot) -> SequenceMode:
    if not isinstance(snapshot, HistoricalSnapshot):
        raise TypeError("snapshot은 HistoricalSnapshot이어야 합니다.")
    return sequence_mode_from_state(
        snapshot.entity(SEQUENCE_MODE_SETTING_KIND, SEQUENCE_MODE_SETTING_ID)
    )


def build_sequence_mode_revision(
    current_revision_id: UUID,
    stored_state: Mapping[str, JsonValue] | None,
    mode: SequenceMode,
    *,
    actor: RevisionActor,
    recorded_at: datetime,
    revision_id: UUID,
) -> PreparedSequenceModeRevision:
    if not isinstance(mode, SequenceMode):
        raise TypeError("mode는 SequenceMode여야 합니다.")
    if not isinstance(actor, RevisionActor):
        raise TypeError("actor는 RevisionActor여야 합니다.")
    if not isinstance(recorded_at, datetime) or recorded_at.tzinfo is None or recorded_at.utcoffset() is None:
        raise ValueError("recorded_at에는 시간대가 있어야 합니다.")
    if not isinstance(revision_id, UUID) or revision_id == current_revision_id:
        raise ValueError("새 revision ID가 올바르지 않습니다.")
    current = sequence_mode_from_state(stored_state)
    if current is mode:
        raise ValueError("현재와 같은 배송순서 입력 방식입니다.")
    after = {"mode": mode.value}
    change = AuditChange(
        SEQUENCE_MODE_SETTING_KIND,
        SEQUENCE_MODE_SETTING_ID,
        ChangeOperation.INSERT if stored_state is None else ChangeOperation.UPDATE,
        stored_state,
        after,
    )
    metadata = RevisionMetadata(
        revision_id,
        current_revision_id,
        recorded_at,
        None,
        actor,
        "배송순서 입력 방식 변경",
        RevisionSource.UI,
        decisions={"sequence_mode_setting": {"mode": mode.value}},
        rule_versions={"sequence_mode_policy_version": 1},
    )
    return PreparedSequenceModeRevision(mode, RevisionRecord(metadata, (change,)))
