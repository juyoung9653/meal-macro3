"""DB-scoped template profile used by generated invoice/confirmation files."""

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
    freeze_json_object,
)


DOCUMENT_TEMPLATE_POLICY_VERSION = 1
DOCUMENT_TEMPLATE_SETTING_KIND = "document_template_profile_setting"
DOCUMENT_TEMPLATE_SETTING_ID = UUID("a74c60cc-a365-526f-a067-ce938c8fead1")


class DocumentTemplateProfile(StrEnum):
    SCHOOL_MEAL = "급식"
    HAPPY_LUNCHBOX = "행복도시락"

    @property
    def label(self) -> str:
        return {
            DocumentTemplateProfile.SCHOOL_MEAL: "급식 양식",
            DocumentTemplateProfile.HAPPY_LUNCHBOX: "행복도시락 양식",
        }[self]


class DocumentTemplateNotConfiguredError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class DocumentTemplateSetting:
    version: int
    profile: DocumentTemplateProfile
    changed_in_revision_id: UUID

    def __post_init__(self) -> None:
        if isinstance(self.version, bool) or not isinstance(self.version, int):
            raise TypeError("문서 양식 version은 정수여야 합니다.")
        if self.version < 1:
            raise ValueError("문서 양식 version은 1부터 시작해야 합니다.")
        if not isinstance(self.profile, DocumentTemplateProfile):
            raise TypeError("profile은 DocumentTemplateProfile이어야 합니다.")
        if not isinstance(self.changed_in_revision_id, UUID):
            raise TypeError("문서 양식 변경 revision ID는 UUID여야 합니다.")

    def public_manifest(self) -> Mapping[str, JsonValue]:
        return freeze_json_object(
            {
                "policy_version": DOCUMENT_TEMPLATE_POLICY_VERSION,
                "version": self.version,
                "profile": self.profile.value,
                "setting_revision_id": str(self.changed_in_revision_id),
            }
        )


@dataclass(frozen=True, slots=True)
class PreparedDocumentTemplateRevision:
    setting: DocumentTemplateSetting
    record: RevisionRecord


def build_document_template_revision(
    current_revision_id: UUID,
    stored_state: Mapping[str, JsonValue] | None,
    profile: DocumentTemplateProfile,
    *,
    actor: RevisionActor,
    recorded_at: datetime,
    revision_id: UUID,
    reason: str = "문서 양식 변경",
) -> PreparedDocumentTemplateRevision:
    if not isinstance(current_revision_id, UUID):
        raise TypeError("현재 revision ID는 UUID여야 합니다.")
    if not isinstance(revision_id, UUID):
        raise TypeError("새 revision ID는 UUID여야 합니다.")
    if revision_id == current_revision_id:
        raise ValueError("새 revision ID는 현재 revision과 달라야 합니다.")
    if not isinstance(profile, DocumentTemplateProfile):
        raise TypeError("profile은 DocumentTemplateProfile이어야 합니다.")
    if not isinstance(actor, RevisionActor):
        raise TypeError("actor는 RevisionActor여야 합니다.")
    _validate_datetime(recorded_at)
    current = document_template_setting_from_state(stored_state)
    if current is not None and current.profile is profile:
        raise ValueError("현재 사용 중인 문서 양식과 같습니다.")
    setting = DocumentTemplateSetting(
        1 if current is None else current.version + 1,
        profile,
        revision_id,
    )
    after = document_template_setting_state(setting)
    change = AuditChange(
        DOCUMENT_TEMPLATE_SETTING_KIND,
        DOCUMENT_TEMPLATE_SETTING_ID,
        ChangeOperation.INSERT if stored_state is None else ChangeOperation.UPDATE,
        before=stored_state,
        after=after,
    )
    metadata = RevisionMetadata(
        revision_id=revision_id,
        parent_revision_id=current_revision_id,
        recorded_at=recorded_at,
        effective_on=None,
        actor=actor,
        reason=reason,
        source=RevisionSource.UI,
        decisions={
            "document_template_change": {
                "profile": profile.value,
                "version": setting.version,
            }
        },
        rule_versions={
            "document_template_policy_version": DOCUMENT_TEMPLATE_POLICY_VERSION
        },
    )
    return PreparedDocumentTemplateRevision(
        setting,
        RevisionRecord(metadata, (change,)),
    )


def document_template_setting_state(
    setting: DocumentTemplateSetting,
) -> Mapping[str, JsonValue]:
    if not isinstance(setting, DocumentTemplateSetting):
        raise TypeError("setting은 DocumentTemplateSetting이어야 합니다.")
    return {
        "policy_version": DOCUMENT_TEMPLATE_POLICY_VERSION,
        "version": setting.version,
        "profile": setting.profile.value,
        "changed_in_revision_id": str(setting.changed_in_revision_id),
    }


def document_template_setting_from_state(
    state: Mapping[str, JsonValue] | None,
) -> DocumentTemplateSetting | None:
    if state is None:
        return None
    if not isinstance(state, Mapping):
        raise TypeError("문서 양식 state는 mapping이어야 합니다.")
    if state.get("policy_version") != DOCUMENT_TEMPLATE_POLICY_VERSION:
        raise ValueError("지원하지 않는 문서 양식 policy version입니다.")
    version = state.get("version")
    profile = state.get("profile")
    revision_id = state.get("changed_in_revision_id")
    if not isinstance(profile, str):
        raise TypeError("문서 양식 profile은 문자열이어야 합니다.")
    if not isinstance(revision_id, str):
        raise TypeError("문서 양식 revision ID는 문자열이어야 합니다.")
    return DocumentTemplateSetting(
        version=version,  # type: ignore[arg-type]
        profile=DocumentTemplateProfile(profile),
        changed_in_revision_id=UUID(revision_id),
    )


def document_template_setting_from_snapshot(
    snapshot: HistoricalSnapshot,
) -> DocumentTemplateSetting | None:
    if not isinstance(snapshot, HistoricalSnapshot):
        raise TypeError("snapshot은 HistoricalSnapshot이어야 합니다.")
    return document_template_setting_from_state(
        snapshot.entity(
            DOCUMENT_TEMPLATE_SETTING_KIND,
            DOCUMENT_TEMPLATE_SETTING_ID,
        )
    )


def require_document_template_setting(
    snapshot: HistoricalSnapshot,
) -> DocumentTemplateSetting:
    setting = document_template_setting_from_snapshot(snapshot)
    if setting is None:
        raise DocumentTemplateNotConfiguredError(
            "이 DB에서 사용할 청구서·확인서 양식을 먼저 선택해주세요."
        )
    return setting


def _validate_datetime(value: object) -> None:
    if not isinstance(value, datetime):
        raise TypeError("문서 양식 기록 시각은 datetime이어야 합니다.")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("문서 양식 기록 시각에는 시간대가 있어야 합니다.")
