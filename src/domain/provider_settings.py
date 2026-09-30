"""Revision-backed common provider setting for one operating database."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
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

PROVIDER_SETTING_KIND = "provider_setting"
PROVIDER_SETTING_ID = UUID("3ece78ed-3d11-51ef-a896-aa0a36ed1c32")


@dataclass(frozen=True, slots=True)
class PreparedProviderRevision:
    provider: str
    record: RevisionRecord


def provider_from_snapshot(snapshot: HistoricalSnapshot) -> str:
    state = snapshot.entity(PROVIDER_SETTING_KIND, PROVIDER_SETTING_ID)
    if state is not None:
        value = state.get("provider")
        if not isinstance(value, str):
            raise TypeError("공통 제공기관 설정은 문자열이어야 합니다.")
        return value
    from .projections import roster_from_historical_snapshot

    providers = {
        person.profile.provider
        for person in roster_from_historical_snapshot(snapshot).persons
        if person.profile.provider
    }
    return next(iter(providers)) if len(providers) == 1 else ""


def build_provider_revision(
    current_revision_id: UUID,
    stored_state: Mapping[str, JsonValue] | None,
    provider: str,
    *,
    actor: RevisionActor,
    recorded_at: datetime,
    revision_id: UUID,
    reason: str = "공통 제공기관 변경",
) -> PreparedProviderRevision:
    if not isinstance(provider, str):
        raise TypeError("제공기관은 문자열이어야 합니다.")
    if not isinstance(actor, RevisionActor):
        raise TypeError("actor는 RevisionActor이어야 합니다.")
    if (
        not isinstance(recorded_at, datetime)
        or recorded_at.tzinfo is None
        or recorded_at.utcoffset() is None
    ):
        raise ValueError("recorded_at에는 시간대가 있어야 합니다.")
    if revision_id == current_revision_id:
        raise ValueError("새 revision ID는 현재 revision과 달라야 합니다.")
    before = stored_state
    if before is not None and before.get("provider") == provider:
        raise ValueError("현재와 같은 공통 제공기관입니다.")
    after = {"provider": provider}
    change = AuditChange(
        PROVIDER_SETTING_KIND,
        PROVIDER_SETTING_ID,
        ChangeOperation.INSERT if before is None else ChangeOperation.UPDATE,
        before,
        after,
    )
    metadata = RevisionMetadata(
        revision_id,
        current_revision_id,
        recorded_at,
        None,
        actor,
        reason,
        RevisionSource.UI,
        decisions={"provider_setting": {"provider": provider}},
        rule_versions={"provider_setting_policy_version": 1},
    )
    return PreparedProviderRevision(provider, RevisionRecord(metadata, (change,)))
