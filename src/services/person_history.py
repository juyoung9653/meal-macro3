"""Read-only, user-facing history tables shared by the desktop and exports.

Rows are oldest first.  Timestamps are converted to Korea Standard Time and
made naive because the presentation surfaces label them as KST.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from uuid import UUID
from zoneinfo import ZoneInfo

from src.domain import HistoricalSnapshot, RevisionRecord, RosterSnapshot
from src.domain.delivery_routing import ROUTING_EVENT_KIND, routing_state_from_snapshot
from src.domain.revisions import format_revision_timestamp
from src.domain.projections import (
    ASSIGNMENT_KIND,
    CUSTOM_FIELD_KIND,
    ENROLLMENT_KIND,
    PERSON_KIND,
    STATUS_EVENT_KIND,
)

_KST = ZoneInfo("Asia/Seoul")
_INFO_HEADINGS = ("기록 시각(KST)", "적용일", "변경번호", "사람 UUID", "성명", "대상", "항목", "이전 값", "변경 값", "사유", "변경자")
_MEAL_HEADINGS = ("기록 시각(KST)", "적용일", "변경번호", "사람 UUID", "성명", "식종", "구분", "이전 값", "변경 값", "접수 채널", "사유", "변경자")
_COMPLAINT_HEADINGS = ("접수 시각(KST)", "기록 시각(KST)", "변경자", "민원 ID", "사람 UUID", "성명", "식종", "채널", "내용")


def build_history_tables(
    roster: RosterSnapshot,
    snapshot: HistoricalSnapshot,
    revisions: tuple[RevisionRecord, ...],
    *,
    person_id: UUID | None = None,
) -> dict[str, tuple[tuple[str, ...], tuple[tuple[object, ...], ...]]]:
    """Build stable tables for the passed immutable revision ancestry only."""
    if person_id is not None and not isinstance(person_id, UUID):
        raise TypeError("person_id는 UUID 또는 None이어야 합니다.")
    info: list[tuple[object, ...]] = []
    meals: list[tuple[object, ...]] = []
    historical_names = {}
    custom_labels = {}
    for record in revisions:
        for change in record.changes:
            state = change.after or change.before or {}
            if change.entity_kind == PERSON_KIND and state.get("name"):
                historical_names[change.entity_id] = state["name"]
            elif change.entity_kind == CUSTOM_FIELD_KIND and state.get("name"):
                custom_labels[str(change.entity_id)] = state["name"]
    custom_labels.update({str(item.definition_id): item.name for item in roster.custom_fields})
    drivers = {str(item.driver_id): item.name for item in routing_state_from_snapshot(snapshot).drivers}
    for record in sorted(revisions, key=lambda item: item.metadata.recorded_at):
        metadata = record.metadata
        stamp = _kst(metadata.recorded_at)
        code = format_revision_timestamp(metadata.recorded_at)
        for change in record.changes:
            before, after = change.before or {}, change.after or {}
            target = _person_id(change.entity_kind, change.entity_id, before, after)
            if person_id is not None and target != person_id:
                continue
            if target is None and person_id is not None:
                continue
            applied = metadata.effective_on
            intake = _intake_channel(metadata.decisions)
            name = _person_name(roster, target, before, after) or historical_names.get(target, "")
            if change.entity_kind == PERSON_KIND:
                for field, old, new in _differences(before, after):
                    if field == "custom_values":
                        for custom_field, custom_old, custom_new in _differences(
                            _mapping(old), _mapping(new)
                        ):
                            label = custom_labels.get(custom_field, custom_field)
                            info.append((stamp, applied, code, str(target), name, "개인정보", f"사용자 정의: {label}", _text(custom_old), _text(custom_new), metadata.reason, metadata.actor.username))
                    else:
                        info.append((stamp, applied, code, str(target), name, "개인정보", _label(field), _text(old), _text(new), metadata.reason, metadata.actor.username))
            elif change.entity_kind == ROUTING_EVENT_KIND and target is not None:
                for field, old, new in _differences(before, after):
                    if field in {"person_id", "position", "effective_on"}:
                        continue
                    target_rows = meals if field == "driver_ids" else info
                    if target_rows is meals:
                        target_rows.append((stamp, applied, code, str(target), name, "식종별", "배송담당", _driver_text(old, drivers), _driver_text(new, drivers), intake, metadata.reason, metadata.actor.username))
                    else:
                        target_rows.append((stamp, applied, code, str(target), name, "배송·주소", f"{_label(field)} 등록" if not before else _label(field), _text(old), _text(new), metadata.reason, metadata.actor.username))
            elif change.entity_kind in {ENROLLMENT_KIND, STATUS_EVENT_KIND, ASSIGNMENT_KIND}:
                meal = after.get("meal", before.get("meal", "공통"))
                category = {ENROLLMENT_KIND: "가입기간", STATUS_EVENT_KIND: "상태", ASSIGNMENT_KIND: "배송배정"}[change.entity_kind]
                diffs = _differences(before, after)
                if not diffs:
                    diffs = (("기록", before, after),)
                for field, old, new in diffs:
                    if field in {"person_id", "revision_id", "recorded_at", "meal"}:
                        continue
                    old_text = _meal_text(field, old) if field == "기록" or field in before else ""
                    new_text = _meal_text(field, new) if field == "기록" or field in after else ""
                    meals.append((stamp, applied, code, str(target), name, _text(meal), f"{category}: {_label(field)}", old_text, new_text, intake, metadata.reason, metadata.actor.username))

    complaints = _complaint_rows(roster, snapshot, revisions, person_id, historical_names)
    return {
        "정보 변경": (_INFO_HEADINGS, tuple(info)),
        "식종 변경": (_MEAL_HEADINGS, tuple(meals)),
        "민원": (_COMPLAINT_HEADINGS, tuple(complaints)),
    }


def _complaint_rows(roster: RosterSnapshot, snapshot: HistoricalSnapshot, revisions: tuple[RevisionRecord, ...], person_id: UUID | None, historical_names: Mapping) -> list[tuple[object, ...]]:
    from src.domain.complaints import complaints_from_snapshot
    records = {change.entity_id: record.metadata for record in revisions for change in record.changes if change.entity_kind == "complaint"}
    rows = []
    for item in complaints_from_snapshot(snapshot):
        if person_id is not None and item.person_id != person_id:
            continue
        record = records.get(item.complaint_id)
        name = _person_name(roster, item.person_id, {}, {}) or historical_names.get(item.person_id, "")
        rows.append((_kst(item.received_at), "" if record is None else _kst(record.recorded_at), "" if record is None else record.actor.username, str(item.complaint_id), str(item.person_id), name, ", ".join(meal.value for meal in item.meals), item.channel.value, item.content))
    return sorted(rows, key=lambda row: row[0])


def _person_id(kind: str, entity_id: UUID, before: Mapping, after: Mapping) -> UUID | None:
    if kind == PERSON_KIND:
        return entity_id
    value = after.get("person_id", before.get("person_id"))
    try:
        return UUID(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _differences(before: Mapping, after: Mapping):
    keys = sorted(set(before) | set(after))
    return tuple((key, before.get(key), after.get(key)) for key in keys if before.get(key) != after.get(key))


def _mapping(value: object) -> Mapping:
    return value if isinstance(value, Mapping) else {}


def _kst(value: datetime) -> datetime:
    return value.astimezone(_KST).replace(tzinfo=None) if value.tzinfo else value.replace(tzinfo=None)


def _text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, Mapping):
        return "; ".join(f"{_label(str(key))}={_text(item)}" for key, item in value.items())
    if isinstance(value, (tuple, list)):
        return "; ".join(_text(item) for item in value)
    return str(value)


def _meal_text(field: str, value: object) -> str:
    """Present projected meal state without leaking its serialized field syntax."""
    if field == "ends_on" and value is None:
        return "종료일 미지정"
    if field == "starts_on" and value is None:
        return "시작일 미지정"
    if field == "periods" and isinstance(value, (tuple, list)):
        return " · ".join(_period_text(item) for item in value)
    if isinstance(value, Mapping):
        return " · ".join(
            f"{_label(str(key))}: {('미지정' if item is None else _meal_text(str(key), item)) or '미지정'}"
            for key, item in value.items()
            if key not in {"person_id", "revision_id", "recorded_at", "meal"}
        )
    if isinstance(value, (tuple, list)):
        return " · ".join(_meal_text(field, item) for item in value)
    return _text(value)


def _period_text(value: object) -> str:
    if not isinstance(value, Mapping) or not {"starts_on", "ends_on"} <= value.keys():
        return _meal_text("", value)
    start, end = value["starts_on"], value["ends_on"]
    if start and end:
        return f"{start} ~ {end}"
    if start:
        return f"{start}부터 · 종료일 미지정"
    if end:
        return f"시작일 미지정 · {end}까지"
    return "시작일·종료일 미지정"


def _driver_text(value: object, names: Mapping[str, str]) -> str:
    if isinstance(value, Mapping):
        return "; ".join(f"{key}={names.get(str(item), '미지정') if item else '미지정'}" for key, item in value.items())
    return _text(value)


def _label(value: str) -> str:
    return {
        "name": "성명", "birth_date": "생년월일", "school_type": "취학유형",
        "school_name": "학교명", "grade": "학년", "class_name": "반",
        "household_head_name": "세대주 성명", "relationship_to_child": "아동과의 관계",
        "jurisdiction": "관할동", "provider": "제공기관", "telephone": "전화번호",
        "mobile_phone": "핸드폰", "note": "비고",
        "road_address": "도로명주소", "detail_address": "상세주소", "driver_ids": "배송담당",
        "periods": "기간", "starts_on": "시작일", "ends_on": "종료일", "source_text": "사유",
        "status": "상태", "sequence": "배송순서", "driver": "배송담당",
    }.get(value, value)


def _person_name(roster: RosterSnapshot, person_id: UUID | None, before: Mapping, after: Mapping) -> str:
    if person_id is not None:
        try:
            return roster.person(person_id).profile.name
        except KeyError:
            pass
    for state in (after, before):
        name = state.get("name") if isinstance(state, Mapping) else None
        if isinstance(name, str):
            return name
    return ""


def _intake_channel(decisions: Mapping) -> str:
    for key in ("meal_status_change", "batch_meal_status_change"):
        choice = decisions.get(key)
        if isinstance(choice, Mapping):
            value = choice.get("intake_channel")
            if isinstance(value, str):
                return value
    return ""
