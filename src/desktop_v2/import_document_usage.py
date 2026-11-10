"""Read current delivery-template preferences without modifying local settings."""
import json
from hashlib import sha256
from pathlib import Path
from zipfile import BadZipFile

from openpyxl.utils.exceptions import InvalidFileException

from src.core.settings import get_settings
from src.domain.document_templates import document_template_setting_from_snapshot
from src.domain.delivery_substitute import DELIVERY_SOURCES, STANDARD_COLUMNS
from src.excel.mapped_delivery_template import inspect_delivery_form
from src.services.sqlite_delivery_substitute_service import all_substitute_template_columns
from src.services.import_column_priority import document_import_targets
from .delivery_template import DeliveryTemplateStore
from .document_hub import DocumentAction
from .substitute_preferences import preferences


def import_document_targets(snapshot, roster, database_id, *, settings=None, preferences_store=None, column_labels=None):
    setting = document_template_setting_from_snapshot(snapshot)
    if setting is None or roster is None:
        return document_import_targets(snapshot, roster, settings=settings)
    settings = settings or get_settings()
    store, key = preferences(database_id, preferences_store)
    try:
        saved = json.loads(store.value(key, "{}"))
    except (TypeError, ValueError):
        saved = {}
    if not isinstance(saved, dict):
        saved = {}
    templates = DeliveryTemplateStore(database_id, profile=setting.profile, settings=store)
    path = templates.selected(DocumentAction.DELIVERY_SUBSTITUTE)
    path = path or Path(settings.get_substitute_template_path(setting.profile.value))
    scope = sha256(str(path.resolve()).encode("utf-8")).hexdigest()
    by_template = saved.get("placeholder_templates", {})
    selected = by_template.get(scope, {}) if isinstance(by_template, dict) else {}
    if not isinstance(selected, dict):
        selected = {}
    def mapping(container, name):
        value = container.get(name, {})
        return {key: source for key, source in value.items() if isinstance(source, str)} if isinstance(value, dict) else {}
    primary = mapping(selected, "template_mapping")
    fallback = mapping(selected, "template_fallbacks")
    try:
        form = inspect_delivery_form(path)
    except (OSError, ValueError, KeyError, BadZipFile, InvalidFileException):
        sources = ({*primary.values(), *fallback.values()} if selected
                   else _legacy_sources(roster, saved))
        targets, unavailable = document_import_targets(
            snapshot, roster, settings=settings, substitute_sources=sources)
        return targets, tuple(dict.fromkeys((*unavailable, "배송목록")))
    if form is None:
        # The output dialog uses checked roster columns and its per-meal choices.
        # Historical DB mappings do not drive this path (options supplies fallbacks).
        sources = _legacy_sources(roster, saved)
        return document_import_targets(snapshot, roster, settings=settings, substitute_sources=sources)
    columns = all_substitute_template_columns(roster)
    labels = {column.key: column.label for column in (*columns, *DELIVERY_SOURCES)}
    aliases = {"school": "school_name", "mobile": "mobile_phone", "address": "road_address"}
    for key, label in (column_labels or {}).items():
        if aliases.get(key, key) in labels:
            labels[aliases.get(key, key)] = label
    defaults = {**{label: key for key, label in labels.items()},
                **{key: source for key, source in form.defaults.items() if source}}
    sources = set()
    by_meal = saved.get("fallbacks_by_meal", {})
    legacy_maps = [value for value in by_meal.values() if isinstance(value, dict)] if isinstance(by_meal, dict) else []
    for token in form.targets:
        value = primary.get(token, defaults.get(token, ""))
        sources.add(value)
        sources.add(fallback.get(token, ""))
        legacy_key = "road_address" if value == "full_address" else value
        if token not in primary:
            sources.update(values[legacy_key] for values in legacy_maps
                           if isinstance(values.get(legacy_key), str))
    return document_import_targets(snapshot, roster, settings=settings, substitute_sources=sources)


def _legacy_sources(roster, saved):
    keys = {column.key for column in all_substitute_template_columns(roster)}
    selected, order = saved.get("columns"), saved.get("order")
    valid = (isinstance(selected, list) and isinstance(order, list)
             and all(isinstance(value, str) for value in (*selected, *order)))
    checked = set(selected) & keys if valid else set()
    if not checked:
        checked = {column.key for column in STANDARD_COLUMNS[:6]} & keys
    sources = set(checked)
    by_meal = saved.get("fallbacks_by_meal", {})
    for values in by_meal.values() if isinstance(by_meal, dict) else ():
        if isinstance(values, dict):
            sources.update(source for target, source in values.items()
                           if target in checked and isinstance(source, str))
    return sources
