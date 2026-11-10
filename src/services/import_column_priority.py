"""Import destinations used by all four document types."""
from zipfile import BadZipFile
from openpyxl.utils.exceptions import InvalidFileException

from src.templates.confirmation_fields import placeholder_mapping
from src.core.models import FULL_ADDRESS_FIELD
from src.domain.document_templates import document_template_setting_from_snapshot
from src.services.document_column_mappings import (
    effective_mapping, load_mapping_overrides, load_mapping_fallbacks, mapping_editor_spec,
)

_SOURCE_FIELDS = {
    "성명": "name", "생년월일": "birth_date", "학교명": "school_name",
    "학년": "grade", "취학유형": "school_type", "반": "class_name",
    "세대주 성명": "household_head_name", "아동과의 관계": "relationship_to_child",
    "전화번호": "telephone", "핸드폰": "mobile_phone", "관할동": "jurisdiction",
    "도로명주소": "road_address", "상세주소": "detail_address", "비고": "note",
    "제공기관": "provider",
}


def source_import_targets(sources):
    targets = set()
    for source in sources:
        if source.startswith("custom:"):
            targets.add(source)
        elif source in ("배송담당", "배송순서"):
            targets.add("driver" if source == "배송담당" else "sequence")
        elif source == FULL_ADDRESS_FIELD:
            targets.update(("profile:road_address", "profile:detail_address"))
        elif source in _SOURCE_FIELDS:
            targets.add("profile:" + _SOURCE_FIELDS[source])
    return frozenset(targets)


def document_import_targets(snapshot, roster=None, *, settings=None, substitute_sources=None):
    """Return stable destination IDs and nonblocking template-read notices."""
    setting = document_template_setting_from_snapshot(snapshot)
    if setting is None:
        return frozenset(), ()
    profile = setting.profile.value
    sources, unavailable = set(), []
    for kind, label in (("invoice", "청구서"), ("confirmation", "확인서"),
                        ("daily_report", "일일식수"), ("substitute", "배송목록")):
        if kind == "substitute" and substitute_sources is not None:
            continue
        overrides = load_mapping_overrides(snapshot, profile, kind)
        valid_targets = None
        try:
            options = {}
            if kind == "invoice" and hasattr(snapshot, "entity"):
                from src.domain.invoice_settings import invoice_settings_from_snapshot
                options["history_column"] = invoice_settings_from_snapshot(snapshot).history_column(profile)
            spec = mapping_editor_spec(profile, kind, settings=settings, roster=roster, **options)
        except (OSError, ValueError, KeyError, BadZipFile, InvalidFileException):
            # An unavailable output template must not prevent roster imports.
            unavailable.append(label)
            sources.update(overrides.values())
        else:
            valid_targets = set(spec.targets)
            mapping = (placeholder_mapping(spec.defaults, overrides) if kind == "confirmation"
                       else effective_mapping(spec.defaults, overrides))
            sources.update(mapping[target] for target in spec.targets if target in mapping)
        fallbacks = load_mapping_fallbacks(snapshot, profile, kind)
        if kind == "confirmation" and valid_targets is not None:
            fallbacks = placeholder_mapping({target: "" for target in spec.targets}, fallbacks)
        sources.update(source for target, source in fallbacks.items()
                       if not target.startswith("__calc_") and (valid_targets is None or target in valid_targets))
    targets = set(source_import_targets(sources))
    if substitute_sources is not None:
        targets.update(key_import_targets(substitute_sources))
    if hasattr(snapshot, "entity"):
        from src.domain.daily_report_choices import daily_report_choices_from_snapshot
        targets.update(key_import_targets(daily_report_choices_from_snapshot(snapshot).remarks.columns))
    return frozenset(targets), tuple(unavailable)


def key_import_targets(keys):
    """Resolve stable roster keys used by delivery placeholders and remarks."""
    aliases = {"school": "school_name", "mobile": "mobile_phone", "address": "road_address"}
    targets = set()
    for key in keys:
        key = aliases.get(key, key)
        if key.startswith("custom:"):
            targets.add(key)
        elif key in ("sequence", "driver_label", "driver"):
            targets.add("sequence" if key == "sequence" else "driver")
        elif key == "full_address":
            targets.update(("profile:road_address", "profile:detail_address"))
        elif key == "jurisdiction_join":
            targets.add("profile:jurisdiction")
        elif key in _SOURCE_FIELDS.values():
            targets.add("profile:" + key)
    return frozenset(targets)
