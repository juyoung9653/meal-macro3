"""Prepare substitute sheets from sealed SQLite facts, never from an Excel input."""
from __future__ import annotations

from datetime import date
import re

from src.domain.delivery_substitute import (
    PreparedSubstituteData, SubstituteColumn, SubstituteRow, STANDARD_COLUMNS,
)
from src.domain.document_templates import document_template_setting_from_snapshot, DocumentTemplateProfile
from src.domain.document_column_mappings import mapping_setting_from_snapshot
from src.domain.delivery_routing import routing_state_from_snapshot
from src.domain.projections import roster_from_historical_snapshot
from src.domain.revisions import HistoricalSnapshot
from src.domain.roster import Meal
from src.services.sqlite_delivery_report_service import prepare_physical_delivery_data
from src.services.delivery_households import require_delivery_household
from src.services.document_column_mappings import substitute_default_mapping, effective_mapping
from src.services.sqlite_document_service import roster_mapping_values
from src.generators.mapping_values import resolve_mapping_value
from src.domain.pause_conversion import pause_conversion_from_snapshot
from src.domain.projections import status_events_from_historical_snapshot
from src.domain.status import resolve_status_history


ROSTER_COLUMN_KEYS = {"school": "school_name", "mobile": "mobile_phone",
                      "address": "road_address", "delivery_settings": "form"}


def substitute_columns_from_roster(columns, *, source_snapshot=None):
    custom_ids = (None if source_snapshot is None else {
        f"custom:{field.definition_id}" for field in roster_from_historical_snapshot(source_snapshot).custom_fields
    })
    return tuple(SubstituteColumn(ROSTER_COLUMN_KEYS.get(column.key, column.key), column.label)
                 for column in columns
                 if custom_ids is None or not column.key.startswith("custom:") or column.key in custom_ids)


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def sequence_sort_key(value: str):
    """Natural ordering without converting or losing the displayed sequence."""
    return (not bool(value), tuple((0, int(part)) if part.isdigit() else (1, part.casefold())
                                   for part in re.split(r"(\d+)", value) if part), value)


def prepare_delivery_substitute_data(
    snapshot: HistoricalSnapshot, day: date, meal: Meal,
    *, mapping_snapshot: HistoricalSnapshot | None = None,
    columns: tuple[SubstituteColumn, ...] | None = None,
    fallbacks: dict[str, str] | None = None,
) -> PreparedSubstituteData:
    if type(day) is not date or not isinstance(meal, Meal):
        raise TypeError("배송 날짜와 식종이 올바르지 않습니다.")
    physical = prepare_physical_delivery_data(snapshot, day, day, (meal,))
    roster = roster_from_historical_snapshot(snapshot)
    routing = routing_state_from_snapshot(snapshot)
    mapping_snapshot = snapshot if mapping_snapshot is None else mapping_snapshot
    template = document_template_setting_from_snapshot(mapping_snapshot)
    template_profile = template.profile if template else DocumentTemplateProfile.SCHOOL_MEAL
    setting = (mapping_setting_from_snapshot(mapping_snapshot, template_profile.value, "substitute")
               if fallbacks is None else None)
    requested_columns, requested_fallbacks = columns, fallbacks
    custom = tuple(sorted(roster.custom_fields, key=lambda item: (item.display_order, str(item.definition_id))))
    columns = STANDARD_COLUMNS + tuple(SubstituteColumn(f"custom:{item.definition_id}", item.name) for item in custom)
    if requested_columns is not None:
        columns = tuple(requested_columns)
        valid = {column.key for column in STANDARD_COLUMNS} | {
            "school_type", "class_name", "household_head_name", "relationship_to_child",
            *(f"status:{meal.name.lower()}" for meal in Meal),
            *(f"custom:{item.definition_id}" for item in custom),
        }
        if len({column.key for column in columns}) != len(columns) or any(column.key not in valid for column in columns):
            raise ValueError("선택한 원본에 없는 배송대타 칼럼입니다. 칼럼이나 원본 변경번호를 확인해주세요.")
    defaults = tuple(column.key for column in STANDARD_COLUMNS[:6])
    note2 = next((item for item in custom if item.name.strip() == "비고2"), None)
    defaults += (f"custom:{note2.definition_id}" if note2 else "note",)
    defaults = tuple(key for key in defaults if key in {column.key for column in columns})
    mapping, fallbacks = {}, {}
    manifest = {"profile": template_profile.value, "setting": setting.public_manifest() if setting else None}
    if setting is not None:
        current_roster = roster_from_historical_snapshot(mapping_snapshot)
        all_defaults = substitute_default_mapping(current_roster)
        all_mapping = effective_mapping(all_defaults, setting.overrides)
        prefix = f"{meal.value}:"
        output_keys = {column.key for column in columns}
        mapping = {key[len(prefix):]: value for key, value in all_mapping.items()
                   if key.startswith(prefix) and key[len(prefix):] in output_keys}
        fallbacks = {key[len(prefix):]: value for key, value in setting.fallbacks.items()
                     if key.startswith(prefix) and key[len(prefix):] in output_keys}
        source_ids = {f"custom:{field.definition_id}" for field in roster.custom_fields}
        current_ids = {f"custom:{field.definition_id}" for field in current_roster.custom_fields}
        for target, source in (*mapping.items(), *fallbacks.items()):
            if source.startswith("custom:") and (source not in source_ids or source not in current_ids):
                label = next(column.label for column in columns if column.key == target)
                raise ValueError(f"배송대타 {meal.value} · {label}에 연결한 사용자 칼럼이 현재 명단 또는 선택한 원본에 없습니다. 출력 칼럼 연결이나 원본 변경번호를 확인해주세요.")
        columns = tuple(SubstituteColumn(column.key, {"road_address": "주소", "note": "비고"}.get(column.key, column.label)) for column in columns)
    if requested_fallbacks is not None:
        keys = {column.key for column in columns}
        if any(target not in keys or source not in keys or target == source
               for target, source in requested_fallbacks.items()):
            raise ValueError("대체 칼럼이 선택한 원본에 없습니다. 배송대타 칼럼을 다시 선택해주세요.")
        mapping = {key: key for key in keys}
        fallbacks = dict(requested_fallbacks)
    manifest.update(mapping=mapping, fallbacks=fallbacks)
    rows = []
    sort_keys = {}
    for shipment in physical.shipments:
        person = roster.person(shipment.person_id)
        profile = person.profile
        # An indivisible shipment may cover several service dates. Reject an
        # ambiguous household transition instead of merging unrelated homes.
        household_id = require_delivery_household(roster, person.person_id, shipment.service_dates)
        view = routing.resolve(roster, person.person_id, day)
        driver = view.drivers[meal]
        driver_id = view.driver_ids[meal]
        driver_key = f"driver:{driver_id}" if driver_id else f"name:{driver}" if driver else "unassigned"
        forms = {setting.form or "" for service_on in shipment.service_dates
                 for setting in roster.delivery_settings_for(person.person_id, on=service_on) if setting.meal is meal}
        form = " / ".join(sorted(forms - {""}))
        values = {column.key: _text(getattr(profile, column.key, None)) for column in (*STANDARD_COLUMNS, *columns)}
        values.update(sequence=roster.resolve_assignment(person.person_id, meal).sequence or "",
                      jurisdiction=view.jurisdiction or "", road_address=view.road_address or "",
                      detail_address=view.detail_address or "", form=form)
        values.update({f"custom:{item.definition_id}": _text(profile.custom_values.get(item.definition_id)) for item in custom})
        for column in columns:
            if column.key.startswith("status:"):
                status_meal = Meal[column.key.split(":", 1)[1].upper()]
                values[column.key] = (
                    resolve_status_history(person.person_id, status_meal,
                        status_events_from_historical_snapshot(snapshot, person_id=person.person_id, meal=status_meal),
                        same_month_pause=pause_conversion_from_snapshot(snapshot).same_month).status_on(day).value
                    if status_meal in roster.meals_for(person.person_id, on=day) else "-"
                )
        sort_keys[person.person_id] = (driver or "미지정", sequence_sort_key(values["sequence"]), values["name"], str(person.person_id))
        if setting is not None:
            # Resolve all outputs against the unmodified source, never against
            # another mapped output. Mapping a phone must not change grouping.
            sources = roster_mapping_values(person, roster, delivery_view=view)
            sources.update({"배송순서": values["sequence"], "배송 형태": form})
            for target, source in mapping.items():
                values[target] = _text(resolve_mapping_value(sources, source, fallbacks.get(target, "")))
        if requested_fallbacks is not None:
            original = dict(values)
            for target, fallback in fallbacks.items():
                values[target] = _text(resolve_mapping_value(original, target, fallback))
        rows.append(SubstituteRow(person.person_id, household_id, driver_key, driver or "미지정",
                                  values, shipment.entitlement_count, any(value.upper().endswith("BOX") for value in forms),
                                  _text(profile.mobile_phone), view.jurisdiction or ""))
    rows.sort(key=lambda row: sort_keys[row.person_id])
    return PreparedSubstituteData(day, meal, tuple(rows), columns, defaults, {
        "substitute_calculation_contract_version": 3,
        "column_mapping": manifest,
        "source_revision_id": str(snapshot.revision_id),
        "delivery_day": day.isoformat(), "meal": meal.value,
        "physical_delivery": physical.calculation_manifest,
        "household_grouping": "consecutive_normalized_db_mobile_phone_within_selected_driver",
        "routing_basis": "physical_delivery_day", "meal_count_basis": "service_date_entitlements",
    })
