"""Prepare substitute sheets from sealed SQLite facts, never from an Excel input."""
from __future__ import annotations

from datetime import date
from dataclasses import replace
import re

from src.domain.delivery_substitute import (
    PreparedSubstituteData, SubstituteColumn, SubstituteRow, STANDARD_COLUMNS, DELIVERY_SOURCES,
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
from src.services.happy_delivery_presence import happy_recipient_presences


ROSTER_COLUMN_KEYS = {"school": "school_name", "mobile": "mobile_phone",
                      "address": "road_address", "delivery_settings": "form"}


def substitute_columns_from_roster(columns, *, source_snapshot=None):
    custom_ids = (None if source_snapshot is None else {
        f"custom:{field.definition_id}" for field in roster_from_historical_snapshot(source_snapshot).custom_fields
    })
    return tuple(SubstituteColumn(ROSTER_COLUMN_KEYS.get(column.key, column.key), column.label)
                 for column in columns
                 if custom_ids is None or not column.key.startswith("custom:") or column.key in custom_ids)



def all_substitute_template_columns(roster, view_columns=None):
    """The complete current catalog, independent of visibility or source date."""
    columns = {column.key: column for column in STANDARD_COLUMNS}
    for key, label in (("school_type", "학교급"), ("class_name", "반"),
                       ("household_head_name", "세대주명"), ("relationship_to_child", "아동과의 관계"),
                       ("lunch_starts_on", "중식 시작일"), ("lunch_ends_on", "중식 종료일")):
        columns[key] = SubstituteColumn(key, label)
    for meal in Meal:
        key = f"status:{meal.name.lower()}"
        columns[key] = SubstituteColumn(key, meal.value)
    for field in sorted(roster.custom_fields, key=lambda field: (field.display_order, str(field.definition_id))):
        key = f"custom:{field.definition_id}"
        columns[key] = SubstituteColumn(key, field.name)
    if view_columns is not None:
        for column in substitute_columns_from_roster(view_columns):
            if column.key in columns:
                columns[column.key] = column
    return tuple(columns.values())


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
    include_breakfast: bool = False,
) -> PreparedSubstituteData:
    if type(day) is not date or not isinstance(meal, Meal):
        raise TypeError("배송 날짜와 식종이 올바르지 않습니다.")
    roster = roster_from_historical_snapshot(snapshot)
    routing = routing_state_from_snapshot(snapshot)
    mapping_snapshot = snapshot if mapping_snapshot is None else mapping_snapshot
    template = document_template_setting_from_snapshot(mapping_snapshot)
    template_profile = template.profile if template else DocumentTemplateProfile.SCHOOL_MEAL
    status_list = template_profile is DocumentTemplateProfile.HAPPY_LUNCHBOX
    meals = ((meal, Meal.BREAKFAST) if status_list and include_breakfast and meal is not Meal.BREAKFAST
             else (meal,))
    physical = None if status_list else prepare_physical_delivery_data(snapshot, day, day, (meal,))
    shipments = (happy_recipient_presences(snapshot, roster, day, day, meals) if status_list else physical.shipments)
    setting = (mapping_setting_from_snapshot(mapping_snapshot, template_profile.value, "substitute")
               if fallbacks is None else None)
    requested_columns, requested_fallbacks = columns, fallbacks
    custom = tuple(sorted(roster.custom_fields, key=lambda item: (item.display_order, str(item.definition_id))))
    columns = STANDARD_COLUMNS + tuple(SubstituteColumn(f"custom:{item.definition_id}", item.name) for item in custom)
    if requested_columns is not None:
        columns = tuple(requested_columns)
        valid = {column.key for column in STANDARD_COLUMNS} | {
            "school_type", "class_name", "household_head_name", "relationship_to_child",
            "lunch_starts_on", "lunch_ends_on", *(column.key for column in DELIVERY_SOURCES),
            *(f"status:{meal.name.lower()}" for meal in Meal),
            *(f"custom:{item.definition_id}" for item in custom),
        }
        if len({column.key for column in columns}) != len(columns) or any(column.key not in valid for column in columns):
            raise ValueError("선택한 원본에 없는 배송대타 칼럼입니다. 칼럼이나 원본 변경번호를 확인해주세요.")
    columns = tuple({column.key: column for column in (*columns, *DELIVERY_SOURCES)}.values())
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
            raise ValueError("1순위 칼럼이 선택한 원본에 없습니다. 배송대타 칼럼을 다시 선택해주세요.")
        mapping = {key: key for key in keys}
        fallbacks = dict(requested_fallbacks)
    manifest.update(mapping=mapping, fallbacks=fallbacks)
    rows = []
    sort_keys = {}
    for shipment in sorted(shipments, key=lambda item: item.meal is not meal):
        person = roster.person(shipment.person_id)
        profile = person.profile
        # Group using this source revision's household, independently of
        # recorded membership dates and mapped output column values.
        household_id = require_delivery_household(roster, person.person_id, shipment.service_dates)
        view = routing.resolve(roster, person.person_id, shipment.delivery_on)
        driver = view.drivers[shipment.meal]
        driver_id = view.driver_ids[shipment.meal]
        driver_key = f"driver:{driver_id}" if driver_id else f"name:{driver}" if driver else "unassigned"
        forms = {setting.form or "" for service_on in shipment.service_dates
                 for setting in roster.delivery_settings_for(person.person_id, on=service_on) if setting.meal is shipment.meal}
        form = " / ".join(sorted(forms - {""}))
        values = {column.key: _text(getattr(profile, column.key, None)) for column in (*STANDARD_COLUMNS, *columns)}
        values.update(sequence=roster.resolve_assignment(person.person_id, shipment.meal).sequence or "",
                      jurisdiction=view.jurisdiction or "", road_address=view.road_address or "",
                      detail_address=view.detail_address or "", form=form)
        from src.domain.lunch_periods import lunch_date
        values.update({key: _text(lunch_date(person, key)) for key in ("lunch_starts_on", "lunch_ends_on")})
        values.update(delivery_day=day.isoformat(), driver_label=driver or "미지정",
                      full_address=" ".join(part for part in (view.road_address, view.detail_address) if part),
                      meal_title=meal.value + "명단", meal_label=meal.value,
                      meal_receives="1" if shipment.meal is meal else "")
        values.update({f"receives:{item.name.lower()}": "1" if shipment.meal is item else "" for item in Meal})
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
        sort_keys.setdefault((person.person_id, driver_key), (driver or "미지정", sequence_sort_key(values["sequence"]), values["name"], str(person.person_id)))
        if setting is not None:
            # Resolve all outputs against the unmodified source, never against
            # another mapped output. Mapping a phone must not change grouping.
            sources = roster_mapping_values(person, roster, delivery_view=view)
            sources.update({"배송순서": values["sequence"], "배송 형태": form})
            for target, source in mapping.items():
                values[target] = _text(resolve_mapping_value(sources, source, fallbacks.get(target, "")))
        if requested_fallbacks is not None:
            # Keep the saved option keys; the side selection now takes priority.
            # Always read original values so mappings cannot cascade.
            original = dict(values)
            for target, fallback in fallbacks.items():
                values[target] = _text(resolve_mapping_value(original, fallback, target))
        rows.append(SubstituteRow(person.person_id, household_id, driver_key, driver or "미지정",
                                  values, shipment.entitlement_count, any(value.upper().endswith("BOX") for value in forms),
                                  _text(profile.mobile_phone), view.jurisdiction or ""))
    combined = {}
    for row in rows:
        key = (row.person_id, row.driver_key)
        if key not in combined:
            combined[key] = row
        else:
            previous = combined[key]
            values = dict(previous.values)
            for item in Meal:
                field = f"receives:{item.name.lower()}"
                values[field] = values.get(field) or row.values.get(field, "")
            values["meal_receives"] = values.get("meal_receives") or row.values.get("meal_receives", "")
            combined[key] = replace(previous, values=values, box=previous.box or row.box)
    rows = sorted(combined.values(), key=lambda row: sort_keys[row.person_id, row.driver_key])
    return PreparedSubstituteData(day, meal, tuple(rows), columns, defaults, {
        "substitute_calculation_contract_version": 6,
        "recipient_basis": "enrolled_delivery_or_paused" if status_list else "physical_delivery",
        "included_meals": [item.value for item in meals],
        "breakfast_reference_day": None,
        "breakfast_reference_calculation": None,
        "column_mapping": manifest,
        "source_revision_id": str(snapshot.revision_id),
        "delivery_day": day.isoformat(), "meal": meal.value,
        **({"physical_delivery": physical.calculation_manifest} if physical else {}),
        "household_grouping": "source_revision_current_household_within_selected_driver",
        "routing_basis": "selected_day" if status_list else "physical_delivery_day",
        "meal_count_basis": "recipient_presence" if status_list else "service_date_entitlements",
    })
