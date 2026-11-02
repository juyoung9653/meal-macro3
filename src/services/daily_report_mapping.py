"""Resolve daily-report labels without changing delivery or household facts."""
from dataclasses import replace

from src.domain.delivery_routing import routing_state_from_snapshot
from src.domain.document_column_mappings import mapping_setting_from_snapshot
from src.domain.document_column_sources import AUTO_HISTORY
from src.domain.projections import roster_from_historical_snapshot
from src.domain.roster import Meal
from src.generators.mapping_values import resolve_mapping_value
from src.services.document_column_mappings import daily_report_defaults, effective_mapping
from src.services.sqlite_document_service import roster_mapping_values
from src.services.delivery_report_remarks import build_status_remarks, status_remark_actions


class DailyReportMapping:
    def __init__(self, snapshot, mapping_snapshot, profile):
        self.profile = profile
        self.roster = roster_from_historical_snapshot(snapshot)
        self.routing = routing_state_from_snapshot(snapshot)
        current = roster_from_historical_snapshot(mapping_snapshot)
        self.setting = mapping_setting_from_snapshot(mapping_snapshot, profile, "daily_report")
        self.mapping = effective_mapping(daily_report_defaults(profile), self.setting.overrides if self.setting else {})
        self.fallbacks = dict(self.setting.fallbacks) if self.setting else {}
        available = {f"custom:{item.definition_id}" for item in self.roster.custom_fields} & {
            f"custom:{item.definition_id}" for item in current.custom_fields}
        for source in (*self.mapping.values(), *self.fallbacks.values()):
            if source.startswith("custom:") and source not in available:
                raise ValueError("일일식수에 연결한 사용자 칼럼이 현재 명단 또는 선택한 원본에 없습니다. 문서 항목 설정을 확인해주세요.")
        self._values = {}
        self._sources = {}

    def values_for(self, person_id, day, meal):
        key = person_id, day, meal
        if key not in self._values:
            view = self.routing.resolve(self.roster, person_id, day)
            sources = roster_mapping_values(self.roster.person(person_id), self.roster, delivery_view=view)
            sources["배송담당"] = view.drivers[meal]
            self._sources[key] = sources
            values = {target: "" if value is None else str(value)
                      for target, source in self.mapping.items()
                      for value in (resolve_mapping_value(sources, source, self.fallbacks.get(target, "")),)}
            values["driver"] = values["driver"].strip()
            values["region"] = values["region"].strip()
            if self.profile == "행복도시락" and (not values["driver"] or not values["region"]):
                values["driver"] = values["region"] = "미분류"
            elif not values["driver"] and (self.mapping["driver"] != "배송담당" or self.fallbacks.get("driver")):
                values["driver"] = "미분류"
            self._values[key] = values
        return self._values[key]

    def for_meal(self, meal):
        return _MappedRouting(self, meal)

    def remarks_for(self, histories, day, meal, shipments, allowed_forms, presences):
        actions = status_remark_actions(self.roster, histories, day, meal, shipments, allowed_forms)
        delivered = {presence.person_id for presence in presences}
        history_actions = {}
        notes = {}
        for person in self.roster.persons:
            person_id = person.person_id
            if person_id not in delivered and person_id not in actions:
                continue
            values = self.values_for(person_id, day, meal)
            sources = {**self._sources[person_id, day, meal], AUTO_HISTORY: actions.get(person_id, "")}
            for source in (self.mapping["note"], self.fallbacks.get("note", "")):
                value = resolve_mapping_value(sources, source)
                if value == "":
                    continue
                if source == AUTO_HISTORY:
                    history_actions[person_id] = str(value)
                elif person_id in delivered:
                    text = f"{values['address']} · {value}" if values["address"] else str(value)
                    notes.setdefault(values["driver"], {}).setdefault(text, None)
                break
        history = build_status_remarks(self.roster, self.for_meal(meal), histories, day, meal,
                                       shipments, allowed_forms, actions=history_actions)
        return {driver: "\n".join(filter(None, (history.get(driver, ""), "\n".join(notes.get(driver, {})))))
                for driver in dict.fromkeys((*history, *notes))}

    def manifest(self):
        return {"profile": self.profile, "document_kind": "daily_report",
                "mapping": self.mapping, "fallbacks": self.fallbacks,
                "setting": self.setting.public_manifest() if self.setting else None}


class _MappedRouting:
    def __init__(self, mapping, meal):
        self.mapping, self.meal = mapping, meal

    def resolve(self, roster, person_id, day):
        original = self.mapping.routing.resolve(roster, person_id, day)
        values = self.mapping.values_for(person_id, day, self.meal)
        return replace(original,
                       drivers={meal: self.mapping.values_for(person_id, day, meal)["driver"] for meal in Meal},
                       jurisdiction=values["region"], road_address=values["address"])
