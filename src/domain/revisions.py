"""Immutable revision-ledger contracts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from math import isfinite
from pathlib import PurePosixPath, PureWindowsPath
from types import MappingProxyType
from uuid import UUID
from zoneinfo import ZoneInfo

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | tuple[JsonValue, ...] | Mapping[str, JsonValue]

_KOREA_TIME_ZONE = ZoneInfo("Asia/Seoul")


class RevisionSource(StrEnum):
    UI = "ui"
    IMPORT = "import"
    SYSTEM = "system"


class ChangeOperation(StrEnum):
    INSERT = "insert"
    UPDATE = "update"
    DELETE = "delete"


@dataclass(frozen=True, slots=True, order=True)
class EntityRef:
    entity_kind: str
    entity_id: UUID

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "entity_kind",
            _required_text(self.entity_kind, "entity kind"),
        )
        _validate_uuid(self.entity_id, "entity ID")


@dataclass(frozen=True, slots=True)
class RevisionActor:
    username: str
    sid: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "username", _required_text(self.username, "사용자명"))
        object.__setattr__(self, "sid", _required_text(self.sid, "Windows SID"))


@dataclass(frozen=True, slots=True)
class RevisionMetadata:
    revision_id: UUID
    parent_revision_id: UUID | None
    recorded_at: datetime
    effective_on: date | None
    actor: RevisionActor
    reason: str
    source: RevisionSource
    decisions: Mapping[str, JsonValue] = field(default_factory=dict)
    rule_versions: Mapping[str, JsonValue] = field(default_factory=dict)
    display_code: str | None = None

    def __post_init__(self) -> None:
        _validate_uuid(self.revision_id, "revision ID")
        if self.parent_revision_id is not None:
            _validate_uuid(self.parent_revision_id, "parent revision ID")
        if not isinstance(self.recorded_at, datetime):
            raise TypeError("기록 시각은 datetime이어야 합니다.")
        if self.recorded_at.tzinfo is None or self.recorded_at.utcoffset() is None:
            raise ValueError("기록 시각에는 시간대가 있어야 합니다.")
        if self.effective_on is not None:
            _validate_date(self.effective_on, "적용일")
        if not isinstance(self.actor, RevisionActor):
            raise TypeError("actor는 RevisionActor여야 합니다.")
        object.__setattr__(self, "reason", _required_text(self.reason, "변경 사유"))
        if not isinstance(self.source, RevisionSource):
            raise TypeError("source는 RevisionSource여야 합니다.")
        object.__setattr__(self, "decisions", freeze_json_object(self.decisions))
        object.__setattr__(
            self,
            "rule_versions",
            freeze_json_object(self.rule_versions),
        )
        if self.display_code is not None:
            object.__setattr__(
                self,
                "display_code",
                validate_revision_display_code(self.display_code),
            )


@dataclass(frozen=True, slots=True)
class RevisionSummary:
    """Small immutable row used by history and document-source pickers."""

    sequence: int
    revision_id: UUID
    display_code: str
    recorded_at: datetime
    effective_on: date | None
    actor: RevisionActor
    reason: str
    source: RevisionSource

    def __post_init__(self) -> None:
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int):
            raise TypeError("revision 순번은 정수여야 합니다.")
        if self.sequence < 1:
            raise ValueError("revision 순번은 1부터 시작해야 합니다.")
        _validate_uuid(self.revision_id, "revision ID")
        object.__setattr__(
            self,
            "display_code",
            validate_revision_display_code(self.display_code),
        )
        if not isinstance(self.recorded_at, datetime):
            raise TypeError("기록 시각은 datetime이어야 합니다.")
        if self.recorded_at.tzinfo is None or self.recorded_at.utcoffset() is None:
            raise ValueError("기록 시각에는 시간대가 있어야 합니다.")
        if self.effective_on is not None:
            _validate_date(self.effective_on, "적용일")
        if not isinstance(self.actor, RevisionActor):
            raise TypeError("actor는 RevisionActor여야 합니다.")
        object.__setattr__(self, "reason", _required_text(self.reason, "변경 사유"))
        if not isinstance(self.source, RevisionSource):
            raise TypeError("source는 RevisionSource여야 합니다.")


@dataclass(frozen=True, slots=True)
class AuditChange:
    entity_kind: str
    entity_id: UUID
    operation: ChangeOperation
    before: Mapping[str, JsonValue] | None
    after: Mapping[str, JsonValue] | None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "entity_kind",
            _required_text(self.entity_kind, "entity kind"),
        )
        _validate_uuid(self.entity_id, "entity ID")
        if not isinstance(self.operation, ChangeOperation):
            raise TypeError("operation은 ChangeOperation이어야 합니다.")
        before = None if self.before is None else freeze_json_object(self.before)
        after = None if self.after is None else freeze_json_object(self.after)
        if self.operation is ChangeOperation.INSERT and (before is not None or after is None):
            raise ValueError("insert 변경은 after만 가져야 합니다.")
        if self.operation is ChangeOperation.UPDATE and (before is None or after is None):
            raise ValueError("update 변경은 before와 after가 모두 필요합니다.")
        if self.operation is ChangeOperation.DELETE and (before is None or after is not None):
            raise ValueError("delete 변경은 before만 가져야 합니다.")
        object.__setattr__(self, "before", before)
        object.__setattr__(self, "after", after)


@dataclass(frozen=True, slots=True)
class ImportBatch:
    batch_id: UUID
    revision_id: UUID
    source_filename: str
    sha256: str
    byte_size: int
    sheet_name: str | None
    header_fingerprint: str
    mapping_manifest: Mapping[str, JsonValue]
    effective_on: date
    decisions: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_uuid(self.batch_id, "import batch ID")
        _validate_uuid(self.revision_id, "revision ID")
        source_filename = _required_literal_text(self.source_filename, "원본 파일명")
        if (
            PureWindowsPath(source_filename).name != source_filename
            or PurePosixPath(source_filename).name != source_filename
        ):
            raise ValueError("원본 파일명에는 전체 경로를 저장할 수 없습니다.")
        object.__setattr__(self, "source_filename", source_filename)
        if not isinstance(self.sha256, str):
            raise TypeError("SHA-256은 문자열이어야 합니다.")
        sha256 = self.sha256.lower()
        if len(sha256) != 64 or any(character not in "0123456789abcdef" for character in sha256):
            raise ValueError("SHA-256은 64자리 16진수여야 합니다.")
        object.__setattr__(self, "sha256", sha256)
        if isinstance(self.byte_size, bool) or not isinstance(self.byte_size, int):
            raise TypeError("파일 크기는 정수여야 합니다.")
        if self.byte_size < 0:
            raise ValueError("파일 크기는 음수일 수 없습니다.")
        if self.sheet_name is not None:
            object.__setattr__(
                self,
                "sheet_name",
                _required_literal_text(self.sheet_name, "시트명"),
            )
        object.__setattr__(
            self,
            "header_fingerprint",
            _required_text(self.header_fingerprint, "헤더 fingerprint"),
        )
        object.__setattr__(
            self,
            "mapping_manifest",
            freeze_json_object(self.mapping_manifest),
        )
        _validate_date(self.effective_on, "적용일")
        object.__setattr__(self, "decisions", freeze_json_object(self.decisions))


@dataclass(frozen=True, slots=True)
class RevisionRecord:
    metadata: RevisionMetadata
    changes: tuple[AuditChange, ...]
    import_batch: ImportBatch | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.metadata, RevisionMetadata):
            raise TypeError("metadata는 RevisionMetadata여야 합니다.")
        changes = tuple(self.changes)
        if not changes or any(not isinstance(change, AuditChange) for change in changes):
            raise ValueError("revision에는 하나 이상의 AuditChange가 필요합니다.")
        references = tuple(
            EntityRef(change.entity_kind, change.entity_id) for change in changes
        )
        if len(set(references)) != len(references):
            raise ValueError("한 revision에서 같은 entity를 두 번 변경할 수 없습니다.")
        if self.import_batch is not None:
            if not isinstance(self.import_batch, ImportBatch):
                raise TypeError("import_batch는 ImportBatch 또는 None이어야 합니다.")
            if self.import_batch.revision_id != self.metadata.revision_id:
                raise ValueError("import batch의 revision ID가 metadata와 다릅니다.")
            if self.metadata.source is not RevisionSource.IMPORT:
                raise ValueError("import batch가 있는 revision의 source는 import여야 합니다.")
        object.__setattr__(self, "changes", changes)


@dataclass(frozen=True, slots=True)
class HistoricalSnapshot:
    """Full canonical entity states reconstructed at one revision."""

    revision_id: UUID
    entities: Mapping[EntityRef, Mapping[str, JsonValue]]

    def __post_init__(self) -> None:
        _validate_uuid(self.revision_id, "revision ID")
        if not isinstance(self.entities, Mapping):
            raise TypeError("entities는 mapping이어야 합니다.")
        frozen: dict[EntityRef, Mapping[str, JsonValue]] = {}
        for reference, state in self.entities.items():
            if not isinstance(reference, EntityRef):
                raise TypeError("snapshot key는 EntityRef여야 합니다.")
            frozen[reference] = freeze_json_object(state)
        object.__setattr__(self, "entities", MappingProxyType(frozen))

    def entity(
        self,
        entity_kind: str,
        entity_id: UUID,
    ) -> Mapping[str, JsonValue] | None:
        return self.entities.get(EntityRef(entity_kind, entity_id))


def freeze_json_object(value: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
    if not isinstance(value, Mapping):
        raise TypeError("JSON object는 mapping이어야 합니다.")
    return MappingProxyType(
        {
            _json_key(key): _freeze_json(item)
            for key, item in value.items()
        }
    )


def plain_json(value: JsonValue) -> JsonScalar | list | dict:
    if isinstance(value, Mapping):
        return {key: plain_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [plain_json(item) for item in value]
    return value


def _freeze_json(value: object) -> JsonValue:
    if isinstance(value, Mapping):
        return freeze_json_object(value)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(_freeze_json(item) for item in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, float) and not isfinite(value):
            raise ValueError("JSON 숫자는 유한해야 합니다.")
        return value
    raise TypeError("감사 JSON에는 scalar, list, object만 허용됩니다.")


def _json_key(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("JSON object key는 문자열이어야 합니다.")
    if not value:
        raise ValueError("JSON object key는 비어 있을 수 없습니다.")
    return value


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label}은 문자열이어야 합니다.")
    normalized = " ".join(value.split())
    if not normalized:
        raise ValueError(f"{label}은 비어 있을 수 없습니다.")
    return normalized


def _required_literal_text(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label}은 문자열이어야 합니다.")
    trimmed = value.strip()
    if not trimmed:
        raise ValueError(f"{label}은 비어 있을 수 없습니다.")
    return trimmed


def _validate_uuid(value: object, label: str) -> None:
    if not isinstance(value, UUID):
        raise TypeError(f"{label}는 UUID여야 합니다.")


def _validate_date(value: object, label: str) -> None:
    if isinstance(value, datetime) or not isinstance(value, date):
        raise TypeError(f"{label}은 date여야 합니다.")


def format_revision_timestamp(recorded_at: datetime) -> str:
    """Format one revision's recorded instant as a Korean save timestamp."""

    if not isinstance(recorded_at, datetime):
        raise TypeError("recorded_at must be a datetime")
    if recorded_at.tzinfo is None or recorded_at.utcoffset() is None:
        raise ValueError("recorded_at must include a time zone")
    return recorded_at.astimezone(_KOREA_TIME_ZONE).strftime("%Y%m%d-%H%M%S")


def validate_revision_display_code(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("표시용 변경번호는 문자열이어야 합니다.")
    if len(value) < 12 or value[8:9] != "-":
        raise ValueError("표시용 변경번호는 YYYYMMDD-001 형식이어야 합니다.")
    recorded_date_text = value[:8]
    ordinal_text = value[9:]
    if not recorded_date_text.isascii() or not recorded_date_text.isdigit():
        raise ValueError("표시용 변경번호는 YYYYMMDD-001 형식이어야 합니다.")
    if not ordinal_text.isascii() or not ordinal_text.isdigit():
        raise ValueError("표시용 변경번호는 YYYYMMDD-001 형식이어야 합니다.")
    try:
        date.fromisoformat(
            f"{recorded_date_text[:4]}-{recorded_date_text[4:6]}-"
            f"{recorded_date_text[6:]}"
        )
    except ValueError as error:
        raise ValueError("표시용 변경번호의 날짜가 올바르지 않습니다.") from error
    ordinal = int(ordinal_text)
    if ordinal < 1 or ordinal_text != f"{ordinal:03d}":
        raise ValueError("표시용 변경번호의 횟수는 001부터 시작해야 합니다.")
    return value
