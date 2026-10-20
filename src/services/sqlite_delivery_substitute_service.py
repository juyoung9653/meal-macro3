"""Prepare substitute sheets from sealed SQLite facts, never from an Excel input."""
from __future__ import annotations

from datetime import date
import re

from src.domain.delivery_substitute import (
    PreparedSubstituteData, SubstituteColumn, SubstituteRow,
)
from src.domain.delivery_routing import routing_state_from_snapshot
from src.domain.projections import roster_from_historical_snapshot
from src.domain.revisions import HistoricalSnapshot
from src.domain.roster import Meal
from src.services.sqlite_delivery_report_service import prepare_physical_delivery_data


STANDARD_COLUMNS = (
    SubstituteColumn("sequence", "배송순서"),
    SubstituteColumn("jurisdiction", "관할동"),
    SubstituteColumn("name", "성명"),
    SubstituteColumn("detail_address", "상세주소"),
    SubstituteColumn("road_address", "도로명주소"),
    SubstituteColumn("mobile_phone", "핸드폰"),
    SubstituteColumn("note", "비고"),
    SubstituteColumn("telephone", "전화번호"),
    SubstituteColumn("school_name", "학교명"),
    SubstituteColumn("grade", "학년"),
    SubstituteColumn("birth_date", "생년월일"),
    SubstituteColumn("form", "배송 형태"),
)


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
) -> PreparedSubstituteData:
    if type(day) is not date or not isinstance(meal, Meal):
        raise TypeError("배송 날짜와 식종이 올바르지 않습니다.")
    physical = prepare_physical_delivery_data(snapshot, day, day, (meal,))
    roster = roster_from_historical_snapshot(snapshot)
    routing = routing_state_from_snapshot(snapshot)
    custom = tuple(sorted(roster.custom_fields, key=lambda item: (item.display_order, str(item.definition_id))))
    columns = STANDARD_COLUMNS + tuple(SubstituteColumn(f"custom:{item.definition_id}", item.name) for item in custom)
    defaults = tuple(column.key for column in STANDARD_COLUMNS[:6])
    note2 = next((item for item in custom if item.name.strip() == "비고2"), None)
    defaults += (f"custom:{note2.definition_id}" if note2 else "note",)
    rows = []
    for shipment in physical.shipments:
        person = roster.person(shipment.person_id)
        profile = person.profile
        # An indivisible shipment may cover several service dates. Reject an
        # ambiguous household transition instead of merging unrelated homes.
        households = {roster.household_for(person.person_id, on=service_on).household_id
                      if roster.household_for(person.person_id, on=service_on) else None
                      for service_on in shipment.service_dates}
        if None in households or len(households) != 1:
            raise ValueError(f"{profile.name}님의 배송에 유효한 가구가 하나로 정해지지 않습니다. 가구 적용일을 확인해주세요.")
        view = routing.resolve(roster, person.person_id, day)
        driver = view.drivers[meal]
        driver_id = view.driver_ids[meal]
        driver_key = f"driver:{driver_id}" if driver_id else f"name:{driver}" if driver else "unassigned"
        forms = {setting.form or "" for service_on in shipment.service_dates
                 for setting in roster.delivery_settings_for(person.person_id, on=service_on) if setting.meal is meal}
        form = " / ".join(sorted(forms - {""}))
        values = {column.key: _text(getattr(profile, column.key, None)) for column in STANDARD_COLUMNS}
        values.update(sequence=roster.resolve_assignment(person.person_id, meal).sequence or "",
                      jurisdiction=view.jurisdiction or "", road_address=view.road_address or "",
                      detail_address=view.detail_address or "", form=form)
        values.update({f"custom:{item.definition_id}": _text(profile.custom_values.get(item.definition_id)) for item in custom})
        rows.append(SubstituteRow(person.person_id, next(iter(households)), driver_key, driver or "미지정",
                                  values, shipment.entitlement_count, any(value.upper().endswith("BOX") for value in forms)))
    rows.sort(key=lambda row: (row.driver_label, sequence_sort_key(row.values["sequence"]), row.values["name"], str(row.person_id)))
    return PreparedSubstituteData(day, meal, tuple(rows), columns, defaults, {
        "substitute_calculation_contract_version": 2,
        "source_revision_id": str(snapshot.revision_id),
        "delivery_day": day.isoformat(), "meal": meal.value,
        "physical_delivery": physical.calculation_manifest,
        "household_grouping": "consecutive_normalized_db_mobile_phone_within_selected_driver",
        "routing_basis": "physical_delivery_day", "meal_count_basis": "service_date_entitlements",
    })
