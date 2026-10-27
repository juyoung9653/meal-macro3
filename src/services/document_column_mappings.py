"""Template-aware editor choices and persistence boundary for column mappings."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import UUID

from src.core.models import FULL_ADDRESS_FIELD
from src.core.settings import SettingsManager, get_settings
from src.domain.document_column_mappings import (
    PreparedMappingRevision, build_mapping_revision as _build_mapping_revision,
    load_mapping_overrides, load_mapping_fallbacks, mapping_setting_from_snapshot,
)
from src.domain.revisions import HistoricalSnapshot, RevisionActor
from src.domain.roster import (
    CustomFieldType, RosterSnapshot, additional_mobile_phone_field_ordinal,
    additional_telephone_field_ordinal,
)
from src.domain.invoice_settings import InvoiceSettings, invoice_settings_from_snapshot
from src.domain.document_column_sources import INVOICE_SOURCE_LABELS, invoice_output_defaults, AUTO_HISTORY
from src.domain.delivery_substitute import STANDARD_COLUMNS
from src.templates.config import auto_detect_semantic_columns, get_template_sheet_name, read_template_columns

_STANDARD_SOURCES = (
    "성명", "생년월일", "학교명", "학년", "전화번호", "핸드폰", "관할동",
    FULL_ADDRESS_FIELD, "비고", "취학유형", "반", "세대주 성명", "아동과의 관계",
    "도로명주소", "상세주소", "제공기관",
)
_SOURCE_COLUMN_KEYS = {
    "성명": "name", "생년월일": "birth_date", "학교명": "school", "학년": "grade",
    "전화번호": "telephone", "핸드폰": "mobile", "관할동": "jurisdiction",
    "도로명주소": "address",
    "상세주소": "detail_address", "비고": "note",
    "취학유형": "school_type", "세대주 성명": "household_head_name",
    "아동과의 관계": "relationship_to_child",
}
_CONFIRM_DEFAULTS = {
    "도로명주소": "도로명주소", "성명": "성명",
    "세대주 성명": "세대주 성명", "핸드폰": "핸드폰", "관할동": "관할동",
}
_CONFIRM_LOCKED: tuple[str, ...] = ()
SUBSTITUTE_MEALS = ("석식", "조식", "중식")


def substitute_default_mapping(roster: RosterSnapshot | None) -> dict[str, str]:
    sources = {column.key: column.label for column in STANDARD_COLUMNS}
    custom = () if roster is None else sorted(
        roster.custom_fields, key=lambda field: (field.display_order, str(field.definition_id)))
    sources.update({f"custom:{field.definition_id}": f"custom:{field.definition_id}" for field in custom})
    return {f"{meal}:{target}": source for meal in SUBSTITUTE_MEALS for target, source in sources.items()}


@dataclass(frozen=True, slots=True)
class MappingEditorSpec:
    defaults: Mapping[str, str]
    targets: tuple[str, ...]
    source_options: tuple[str, ...]
    locked_targets: tuple[str, ...]
    target_labels: Mapping[str, str]
    source_labels: Mapping[str, str]
    selectable_source_options: tuple[str, ...] | None = None


def invoice_default_mapping(template_path: Path, profile: str, is_vacation_month: bool, *, history_column: str | None = None) -> dict[str, str]:
    columns = read_template_columns(str(template_path), sheet_name=get_template_sheet_name(is_vacation_month, profile))
    mapping = auto_detect_semantic_columns(columns)
    aliases = {
        "취학유형": "취학유형", "구분": "취학유형", "학교명": "학교명", "학년": "학년", "반": "반",
        "성명": "성명", "이름": "성명", "생년월일": "생년월일", "세대주성명": "세대주 성명",
        "아동과의관계": "아동과의 관계", "관할동": "관할동", "동": "관할동",
        "도로명주소": FULL_ADDRESS_FIELD, "주소": FULL_ADDRESS_FIELD, "전화번호": "전화번호",
        "핸드폰": "핸드폰", "제공기관": "제공기관", "비고": "비고", "평일중식중지=>초1-2,고3": "비고",
    }
    for column in columns:
        normalized = "".join(column.split()).replace("(급식수)", "").replace("(원)", "")
        source = aliases.get(normalized)
        if source is None and normalized.startswith("취학유형"):
            source = "취학유형"
        if source is not None:
            mapping[column] = source
    # Personal notes were not printed by default. They are now an explicit choice.
    mapping = {target: ("" if source == "비고" else source) for target, source in mapping.items()}
    history_column = InvoiceSettings().history_column(profile) if history_column is None else history_column
    if history_column in columns:
        mapping[history_column] = AUTO_HISTORY
    return invoice_output_defaults(mapping, columns)


def confirmation_default_mapping() -> dict[str, str]:
    return {
        "도로명주소": "도로명주소", "상세주소": "상세주소", "성명": "성명", "이름": "성명",
        "세대주 성명": "세대주 성명", "세대주\n성명": "세대주 성명", "세대주성명": "세대주 성명",
        "핸드폰": "핸드폰", "전화번호": "전화번호", "관할동": "관할동", "동": "관할동",
    }


def mapping_editor_spec(
    profile: str, document_kind: str, *, settings: SettingsManager | None = None,
    roster: RosterSnapshot | None = None,
    column_labels: Mapping[str, str] | None = None,
    history_column: str | None = None,
) -> MappingEditorSpec:
    if profile not in ("급식", "행복도시락") or document_kind not in ("invoice", "confirmation", "substitute"):
        raise ValueError("지원하지 않는 문서 양식 또는 문서 종류입니다.")
    settings = settings or get_settings()
    if document_kind == "invoice":
        path = Path(settings.get_invoice_template_path(profile)).resolve()
        defaults = invoice_default_mapping(path, profile, False, history_column=history_column)
        if profile == "행복도시락":
            defaults.update(invoice_default_mapping(path, profile, True, history_column=history_column))
        columns = _template_invoice_columns(path, profile)
        targets = tuple(columns)
        for target in targets:
            defaults.setdefault(target, "")
        locked = ()
    elif document_kind == "substitute":
        defaults = substitute_default_mapping(roster)
        targets = tuple(defaults)
        locked = ()
    else:
        defaults = confirmation_default_mapping()
        targets = tuple(_CONFIRM_DEFAULTS)
        locked = _CONFIRM_LOCKED
    source_options = list(_STANDARD_SOURCES)
    if document_kind == "substitute":
        source_options.extend(("배송순서", "배송 형태"))
    if document_kind == "invoice":
        source_options.extend(source for source in INVOICE_SOURCE_LABELS if source not in source_options)
    labels = {key: key for key in source_options}
    labels["학교명"] = "학교"
    labels[FULL_ADDRESS_FIELD] = "전체 주소 (도로명주소 + 상세주소)"
    if document_kind == "invoice":
        labels.update(INVOICE_SOURCE_LABELS)
    if roster is not None:
        text_fields = tuple(field for field in roster.custom_fields if field.field_type is CustomFieldType.TEXT)
        if any(additional_telephone_field_ordinal(field.name) is not None for field in text_fields):
            labels["전화번호"] = "전화번호1"
        if any(additional_mobile_phone_field_ordinal(field.name) is not None for field in text_fields):
            labels["핸드폰"] = "핸드폰1"
        for definition in roster.custom_fields:
            key = f"custom:{definition.definition_id}"
            source_options.append(key)
            labels[key] = definition.name
    for source, column_key in _SOURCE_COLUMN_KEYS.items():
        if source != FULL_ADDRESS_FIELD and column_labels and column_key in column_labels:
            labels[source] = column_labels[column_key]
    # Display names may collide; the saved field identity must stay unambiguous.
    label_counts = Counter(labels.values())
    for source in source_options:
        if label_counts[labels[source]] > 1:
            suffix = "추가 항목" if source.startswith("custom:") else f"기본 항목 · {source}"
            labels[source] = f"{labels[source]} ({suffix})"
    source_options.insert(0, "")
    labels[""] = "비움"
    selectable_sources = tuple(
        source for source in source_options
        if not source or (document_kind == "invoice" and source in INVOICE_SOURCE_LABELS) or (
            (source in _SOURCE_COLUMN_KEYS or source.startswith("custom:")
             or (document_kind == "substitute" and source in ("배송순서", "배송 형태")))
            and (
                column_labels is None
                or (document_kind == "substitute" and source in ("배송순서", "배송 형태"))
                or _SOURCE_COLUMN_KEYS.get(source, source) in column_labels
            )
        )
    )
    target_labels = {key: key for key in targets}
    if document_kind == "substitute":
        output_labels = {column.key: column.label for column in STANDARD_COLUMNS}
        if roster is not None:
            output_labels.update({f"custom:{field.definition_id}": field.name for field in roster.custom_fields})
        target_labels = {key: output_labels[key.split(":", 1)[1]] for key in targets}
    for key in locked:
        if key in target_labels:
            if document_kind == "confirmation":
                reason = "가구 묶음 기준"
            elif key == "제공기관":
                reason = "공통 설정 자동 적용"
            elif key == history_column:
                reason = "식종 변경 이력 자동 적용"
            else:
                reason = "자동 계산"
            target_labels[key] = f"{key} ({reason})"
    return MappingEditorSpec(defaults, targets, tuple(source_options), locked, target_labels, labels, selectable_sources)


def _template_invoice_columns(path: Path, profile: str) -> tuple[str, ...]:
    result = list(dict.fromkeys(read_template_columns(str(path), sheet_name=get_template_sheet_name(False, profile))))
    if profile == "행복도시락":
        for column in read_template_columns(str(path), sheet_name=get_template_sheet_name(True, profile)):
            if column not in result:
                result.append(column)
    return tuple(result)


def effective_mapping(defaults: Mapping[str, str], overrides: Mapping[str, str]) -> dict[str, str]:
    result = dict(defaults)
    for target, source in overrides.items():
        if not target.startswith("__calc_"):
            result[target] = source
    return result


def effective_fallbacks(defaults: Mapping[str, str], fallbacks: Mapping[str, str], *, document_kind: str) -> dict[str, str]:
    return {target: source for target, source in fallbacks.items()
            if source and not target.startswith("__calc_")}


def build_mapping_revision(
    current_revision_id: UUID, snapshot: HistoricalSnapshot, profile: str,
    document_kind: str, desired_mapping: Mapping[str, str], *, actor: RevisionActor,
    recorded_at: datetime, revision_id: UUID, settings: SettingsManager | None = None,
    roster: RosterSnapshot | None = None,
    desired_fallbacks: Mapping[str, str] | None = None,
) -> PreparedMappingRevision | None:
    spec = mapping_editor_spec(profile, document_kind, settings=settings, roster=roster,
                               history_column=invoice_settings_from_snapshot(snapshot).history_column(profile))
    editable = {key: spec.defaults[key] for key in spec.targets if key not in spec.locked_targets}
    return _build_mapping_revision(
        current_revision_id, snapshot, profile, document_kind, desired_mapping,
        desired_fallbacks=desired_fallbacks,
        defaults=editable, allowed_sources=spec.source_options, locked_targets=spec.locked_targets,
        actor=actor, recorded_at=recorded_at, revision_id=revision_id,
    )


__all__ = [
    "MappingEditorSpec", "mapping_editor_spec", "load_mapping_overrides", "load_mapping_fallbacks",
    "mapping_setting_from_snapshot", "effective_mapping", "effective_fallbacks", "build_mapping_revision",
    "invoice_default_mapping", "confirmation_default_mapping",
]
