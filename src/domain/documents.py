"""Immutable contracts for generated document history."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from pathlib import PurePosixPath, PureWindowsPath
from uuid import UUID

from .revisions import (
    JsonValue,
    RevisionActor,
    freeze_json_object,
    validate_revision_display_code,
)


class DocumentKind(StrEnum):
    INVOICE = "invoice"
    CONFIRMATION = "confirmation"
    DELIVERY_REPORT = "delivery_report"
    DELIVERY_SUBSTITUTE = "delivery_substitute"

    @property
    def label(self) -> str:
        return {
            DocumentKind.INVOICE: "청구서",
            DocumentKind.CONFIRMATION: "확인서",
            DocumentKind.DELIVERY_REPORT: "일일식수",
            DocumentKind.DELIVERY_SUBSTITUTE: "배송대타",
        }[self]


@dataclass(frozen=True, slots=True)
class DocumentRun:
    """One successfully generated file tied to an immutable source revision."""

    run_id: UUID
    kind: DocumentKind
    source_revision_id: UUID
    source_revision_display_code: str
    period_start: date
    period_end: date
    generated_at: datetime
    actor: RevisionActor
    calculation_manifest: Mapping[str, JsonValue]
    template_manifest: Mapping[str, JsonValue]
    output_filename: str
    output_sha256: str
    output_byte_size: int

    def __post_init__(self) -> None:
        _validate_uuid(self.run_id, "문서 run ID")
        if not isinstance(self.kind, DocumentKind):
            raise TypeError("문서 종류는 DocumentKind여야 합니다.")
        _validate_uuid(self.source_revision_id, "원본 revision ID")
        object.__setattr__(
            self,
            "source_revision_display_code",
            validate_revision_display_code(self.source_revision_display_code),
        )
        _validate_date(self.period_start, "문서 시작일")
        _validate_date(self.period_end, "문서 종료일")
        if self.period_start > self.period_end:
            raise ValueError("문서 시작일은 종료일보다 늦을 수 없습니다.")
        if not isinstance(self.generated_at, datetime):
            raise TypeError("문서 생성 시각은 datetime이어야 합니다.")
        if self.generated_at.tzinfo is None or self.generated_at.utcoffset() is None:
            raise ValueError("문서 생성 시각에는 시간대가 있어야 합니다.")
        if not isinstance(self.actor, RevisionActor):
            raise TypeError("actor는 RevisionActor여야 합니다.")
        calculation_manifest = freeze_json_object(self.calculation_manifest)
        if not calculation_manifest:
            raise ValueError("계산 version manifest는 비어 있을 수 없습니다.")
        object.__setattr__(self, "calculation_manifest", calculation_manifest)
        template_manifest = freeze_json_object(self.template_manifest)
        if not template_manifest:
            raise ValueError("템플릿 version manifest는 비어 있을 수 없습니다.")
        object.__setattr__(self, "template_manifest", template_manifest)
        object.__setattr__(
            self,
            "output_filename",
            _filename(self.output_filename),
        )
        object.__setattr__(self, "output_sha256", _sha256(self.output_sha256))
        if (
            isinstance(self.output_byte_size, bool)
            or not isinstance(self.output_byte_size, int)
        ):
            raise TypeError("문서 파일 크기는 정수여야 합니다.")
        if self.output_byte_size < 1:
            raise ValueError("문서 파일 크기는 1 byte 이상이어야 합니다.")


def _filename(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("문서 파일명은 문자열이어야 합니다.")
    filename = value.strip()
    if not filename:
        raise ValueError("문서 파일명은 비어 있을 수 없습니다.")
    if (
        PureWindowsPath(filename).name != filename
        or PurePosixPath(filename).name != filename
    ):
        raise ValueError("문서 이력에는 전체 경로를 저장할 수 없습니다.")
    return filename


def _sha256(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("문서 SHA-256은 문자열이어야 합니다.")
    normalized = value.lower()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ValueError("문서 SHA-256은 64자리 16진수여야 합니다.")
    return normalized


def _validate_uuid(value: object, label: str) -> None:
    if not isinstance(value, UUID):
        raise TypeError(f"{label}는 UUID여야 합니다.")


def _validate_date(value: object, label: str) -> None:
    if isinstance(value, datetime) or not isinstance(value, date):
        raise TypeError(f"{label}은 date여야 합니다.")
