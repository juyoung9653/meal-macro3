"""Versioned, DB-scoped overrides for output template columns."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID, uuid5

from .revisions import (
    AuditChange, ChangeOperation, HistoricalSnapshot, JsonValue, RevisionActor,
    RevisionMetadata, RevisionRecord, RevisionSource,
)

MAPPING_POLICY_VERSION = 2
MAPPING_ENTITY_KIND = "document_column_mapping_setting"
_NAMESPACE = UUID("5df58302-12ce-4a34-a07d-98c24765904e")
PROFILES = ("급식", "행복도시락")
DOCUMENT_KINDS = ("invoice", "confirmation", "substitute", "daily_report")


def mapping_entity_id(profile: str, document_kind: str) -> UUID:
    _check_scope(profile, document_kind)
    return uuid5(_NAMESPACE, f"{profile}:{document_kind}")


def _check_scope(profile: str, document_kind: str) -> None:
    if profile not in PROFILES or document_kind not in DOCUMENT_KINDS:
        raise ValueError("지원하지 않는 문서 양식 또는 문서 종류입니다.")


@dataclass(frozen=True, slots=True)
class DocumentColumnMappingSetting:
    profile: str
    document_kind: str
    version: int
    overrides: Mapping[str, str]
    changed_in_revision_id: UUID
    fallbacks: Mapping[str, str] = field(default_factory=dict)

    def public_manifest(self) -> dict[str, JsonValue]:
        return {
            "policy_version": MAPPING_POLICY_VERSION,
            "version": self.version,
            "profile": self.profile,
            "document_kind": self.document_kind,
            "setting_revision_id": str(self.changed_in_revision_id),
            "overrides": dict(sorted(self.overrides.items())),
            "fallbacks": dict(sorted(self.fallbacks.items())),
        }


@dataclass(frozen=True, slots=True)
class PreparedMappingRevision:
    setting: DocumentColumnMappingSetting
    record: RevisionRecord


def mapping_setting_from_state(state: Mapping[str, JsonValue] | None) -> DocumentColumnMappingSetting | None:
    if state is None:
        return None
    if state.get("policy_version") not in (1, MAPPING_POLICY_VERSION):
        raise ValueError("지원하지 않는 출력 칼럼 연결 버전입니다.")
    profile, kind, version = state.get("profile"), state.get("document_kind"), state.get("version")
    if not isinstance(profile, str) or not isinstance(kind, str):
        raise ValueError("출력 칼럼 연결 범위가 올바르지 않습니다.")
    _check_scope(profile, kind)
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ValueError("출력 칼럼 연결 변경 번호가 올바르지 않습니다.")
    overrides = state.get("overrides")
    if not isinstance(overrides, Mapping) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in overrides.items()):
        raise ValueError("출력 칼럼 연결 값이 올바르지 않습니다.")
    fallbacks = state.get("fallbacks", {})
    if not isinstance(fallbacks, Mapping) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in fallbacks.items()):
        raise ValueError("출력 칼럼 2순위 연결 값이 올바르지 않습니다.")
    revision = state.get("changed_in_revision_id")
    if not isinstance(revision, str):
        raise ValueError("출력 칼럼 연결 revision ID가 올바르지 않습니다.")
    return DocumentColumnMappingSetting(profile, kind, version, dict(overrides), UUID(revision), dict(fallbacks))


def mapping_setting_from_snapshot(snapshot: HistoricalSnapshot, profile: str, document_kind: str) -> DocumentColumnMappingSetting | None:
    state = snapshot.entity(MAPPING_ENTITY_KIND, mapping_entity_id(profile, document_kind))
    setting = mapping_setting_from_state(state)
    if setting is not None and (setting.profile, setting.document_kind) != (profile, document_kind):
        raise ValueError("출력 칼럼 연결 범위가 저장 위치와 다릅니다.")
    return setting


def load_mapping_overrides(snapshot: HistoricalSnapshot, profile: str, document_kind: str) -> dict[str, str]:
    setting = mapping_setting_from_snapshot(snapshot, profile, document_kind)
    return {} if setting is None else dict(setting.overrides)


def load_mapping_fallbacks(snapshot: HistoricalSnapshot, profile: str, document_kind: str) -> dict[str, str]:
    setting = mapping_setting_from_snapshot(snapshot, profile, document_kind)
    return {} if setting is None else dict(setting.fallbacks)


def build_mapping_revision(
    current_revision_id: UUID,
    snapshot: HistoricalSnapshot,
    profile: str,
    document_kind: str,
    desired_mapping: Mapping[str, str],
    *,
    desired_fallbacks: Mapping[str, str] | None = None,
    defaults: Mapping[str, str],
    allowed_sources: tuple[str, ...],
    locked_targets: tuple[str, ...] = (),
    actor: RevisionActor,
    recorded_at: datetime,
    revision_id: UUID,
) -> PreparedMappingRevision | None:
    """Store only deviations; no-op saves do not append a revision."""
    _check_scope(profile, document_kind)
    if snapshot.revision_id != current_revision_id:
        raise ValueError("현재 revision과 명단 snapshot이 다릅니다.")
    if revision_id == current_revision_id:
        raise ValueError("새 revision ID는 현재 revision과 달라야 합니다.")
    if not isinstance(actor, RevisionActor):
        raise TypeError("actor는 RevisionActor여야 합니다.")
    if recorded_at.tzinfo is None or recorded_at.utcoffset() is None:
        raise ValueError("기록 시각에는 시간대가 있어야 합니다.")
    if not isinstance(desired_mapping, Mapping):
        raise TypeError("출력 칼럼 연결은 mapping이어야 합니다.")
    state = snapshot.entity(MAPPING_ENTITY_KIND, mapping_entity_id(profile, document_kind))
    current = mapping_setting_from_state(state)
    allowed = set(allowed_sources)
    locked = set(locked_targets)
    overrides: dict[str, str] = {}
    for target, source in desired_mapping.items():
        if target not in defaults or target in locked:
            raise ValueError(f"편집할 수 없는 출력 칼럼입니다: {target}")
        if not isinstance(source, str) or (source not in allowed and source != (current.overrides.get(target) if current is not None else None)):
            raise ValueError(f"{target} 칼럼에 연결할 명단 항목을 다시 선택해주세요.")
        if source != defaults[target]:
            overrides[target] = source
    desired_fallbacks = (current.fallbacks if current else {}) if desired_fallbacks is None else desired_fallbacks
    if not isinstance(desired_fallbacks, Mapping):
        raise TypeError("출력 칼럼 2순위 연결은 mapping이어야 합니다.")
    fallbacks = {}
    for target, source in desired_fallbacks.items():
        if target not in defaults or target in locked:
            raise ValueError(f"편집할 수 없는 출력 칼럼입니다: {target}")
        if not isinstance(source, str) or (source not in allowed and source != (current.fallbacks.get(target) if current is not None else None)):
            raise ValueError(f"{target} 칼럼의 2순위 명단 항목을 다시 선택해주세요.")
        if source:
            fallbacks[target] = source
    if current is not None and dict(current.overrides) == overrides and dict(current.fallbacks) == fallbacks:
        return None
    if current is None and not overrides and not fallbacks:
        return None
    setting = DocumentColumnMappingSetting(profile, document_kind, 1 if current is None else current.version + 1, overrides, revision_id, fallbacks)
    after: dict[str, JsonValue] = {
        "policy_version": MAPPING_POLICY_VERSION,
        "profile": profile,
        "document_kind": document_kind,
        "version": setting.version,
        "overrides": overrides,
        "fallbacks": fallbacks,
        "changed_in_revision_id": str(revision_id),
    }
    change = AuditChange(MAPPING_ENTITY_KIND, mapping_entity_id(profile, document_kind), ChangeOperation.INSERT if state is None else ChangeOperation.UPDATE, before=state, after=after)
    metadata = RevisionMetadata(
        revision_id=revision_id, parent_revision_id=current_revision_id,
        recorded_at=recorded_at, effective_on=None, actor=actor,
        reason="출력 칼럼 연결 변경", source=RevisionSource.UI,
        decisions={"document_column_mapping_change": {"profile": profile, "document_kind": document_kind, "version": setting.version}},
        rule_versions={"document_column_mapping_policy_version": MAPPING_POLICY_VERSION},
    )
    return PreparedMappingRevision(setting, RevisionRecord(metadata, (change,)))
