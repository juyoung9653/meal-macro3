"""Revision-backed password used for generated personal-data workbooks."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
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
    freeze_json_object,
)


EXPORT_PASSWORD_POLICY_VERSION = 1
EXPORT_PASSWORD_SETTING_KIND = "export_password_setting"
EXPORT_PASSWORD_SETTING_ID = UUID("ea86c3c9-38eb-558a-89a9-ae07d588ef6c")
_MAX_PASSWORD_CHARACTERS = 255


class ExportPasswordNotConfiguredError(ValueError):
    """Generated personal-data workbook output is blocked until configured."""


@dataclass(frozen=True, slots=True)
class ExportPasswordSetting:
    """One active shared Excel password, held only inside the encrypted DB."""

    version: int
    password: str = field(repr=False)
    changed_in_revision_id: UUID

    def __post_init__(self) -> None:
        if isinstance(self.version, bool) or not isinstance(self.version, int):
            raise TypeError("Excel 비밀번호 version은 정수여야 합니다.")
        if self.version < 1:
            raise ValueError("Excel 비밀번호 version은 1부터 시작해야 합니다.")
        object.__setattr__(self, "password", _password(self.password))
        _validate_uuid(self.changed_in_revision_id, "Excel 비밀번호 변경 revision ID")

    def public_manifest(self) -> Mapping[str, JsonValue]:
        """Return regeneration metadata that never contains the password."""

        return freeze_json_object(
            {
                "protected": True,
                "policy_version": EXPORT_PASSWORD_POLICY_VERSION,
                "version": self.version,
                "setting_revision_id": str(self.changed_in_revision_id),
            }
        )


@dataclass(frozen=True, slots=True)
class PreparedExportPasswordRevision:
    setting: ExportPasswordSetting
    record: RevisionRecord


def build_export_password_revision(
    current_revision_id: UUID,
    stored_state: Mapping[str, JsonValue] | None,
    password: str,
    *,
    actor: RevisionActor,
    recorded_at: datetime,
    revision_id: UUID,
    reason: str = "생성 Excel 비밀번호 변경",
) -> PreparedExportPasswordRevision:
    """Insert or replace the shared export password as one UI revision."""

    _validate_uuid(current_revision_id, "현재 revision ID")
    _validate_uuid(revision_id, "새 revision ID")
    if revision_id == current_revision_id:
        raise ValueError("새 revision ID는 현재 revision과 달라야 합니다.")
    if not isinstance(actor, RevisionActor):
        raise TypeError("actor는 RevisionActor여야 합니다.")
    _validate_datetime(recorded_at, "Excel 비밀번호 기록 시각")
    normalized_password = _password(password)
    current = export_password_setting_from_state(stored_state)
    if current is not None and current.password == normalized_password:
        raise ValueError("현재와 같은 Excel 비밀번호입니다.")

    setting = ExportPasswordSetting(
        version=1 if current is None else current.version + 1,
        password=normalized_password,
        changed_in_revision_id=revision_id,
    )
    after = export_password_setting_state(setting)
    operation = (
        ChangeOperation.INSERT
        if stored_state is None
        else ChangeOperation.UPDATE
    )
    change = AuditChange(
        EXPORT_PASSWORD_SETTING_KIND,
        EXPORT_PASSWORD_SETTING_ID,
        operation,
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
            "export_password_change": {
                "configured": True,
                "version": setting.version,
            }
        },
        rule_versions={
            "export_password_policy_version": EXPORT_PASSWORD_POLICY_VERSION
        },
    )
    return PreparedExportPasswordRevision(setting, RevisionRecord(metadata, (change,)))


def export_password_setting_state(
    setting: ExportPasswordSetting,
) -> Mapping[str, JsonValue]:
    if not isinstance(setting, ExportPasswordSetting):
        raise TypeError("setting은 ExportPasswordSetting이어야 합니다.")
    return {
        "policy_version": EXPORT_PASSWORD_POLICY_VERSION,
        "version": setting.version,
        "password": setting.password,
        "changed_in_revision_id": str(setting.changed_in_revision_id),
    }


def export_password_setting_from_state(
    state: Mapping[str, JsonValue] | None,
) -> ExportPasswordSetting | None:
    if state is None:
        return None
    if not isinstance(state, Mapping):
        raise TypeError("Excel 비밀번호 state는 mapping이어야 합니다.")
    if state.get("policy_version") != EXPORT_PASSWORD_POLICY_VERSION:
        raise ValueError("지원하지 않는 Excel 비밀번호 정책 version입니다.")
    version = state.get("version")
    password = state.get("password")
    revision_id = state.get("changed_in_revision_id")
    if not isinstance(revision_id, str):
        raise TypeError("Excel 비밀번호 변경 revision ID는 문자열이어야 합니다.")
    return ExportPasswordSetting(
        version=version,  # type: ignore[arg-type]
        password=password,  # type: ignore[arg-type]
        changed_in_revision_id=UUID(revision_id),
    )


def export_password_setting_from_snapshot(
    snapshot: HistoricalSnapshot,
) -> ExportPasswordSetting | None:
    if not isinstance(snapshot, HistoricalSnapshot):
        raise TypeError("snapshot은 HistoricalSnapshot이어야 합니다.")
    return export_password_setting_from_state(
        snapshot.entity(EXPORT_PASSWORD_SETTING_KIND, EXPORT_PASSWORD_SETTING_ID)
    )


def require_export_password_setting(
    snapshot: HistoricalSnapshot,
) -> ExportPasswordSetting:
    """Return the configured setting or block unprotected v2 output."""

    setting = export_password_setting_from_snapshot(snapshot)
    if setting is None:
        raise ExportPasswordNotConfiguredError(
            "설정에서 생성 Excel 공통 비밀번호를 먼저 지정해주세요."
        )
    return setting


def _password(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("Excel 비밀번호는 문자열이어야 합니다.")
    if not value:
        raise ValueError("Excel 비밀번호를 입력해주세요.")
    if not value.strip():
        raise ValueError("Excel 비밀번호는 공백만으로 만들 수 없습니다.")
    if value != value.strip():
        raise ValueError("Excel 비밀번호 앞뒤에는 공백을 사용할 수 없습니다.")
    if len(value) > _MAX_PASSWORD_CHARACTERS:
        raise ValueError(
            f"Excel 비밀번호는 {_MAX_PASSWORD_CHARACTERS}자 이하여야 합니다."
        )
    return value


def _validate_uuid(value: object, label: str) -> None:
    if not isinstance(value, UUID):
        raise TypeError(f"{label}는 UUID여야 합니다.")


def _validate_datetime(value: object, label: str) -> None:
    if not isinstance(value, datetime):
        raise TypeError(f"{label}은 datetime이어야 합니다.")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label}에는 시간대가 있어야 합니다.")
