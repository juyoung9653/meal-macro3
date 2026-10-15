"""Presentation rows for every change in one immutable revision."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from uuid import UUID

from src.domain.complaints import COMPLAINT_KIND
from src.domain.delivery_defaults import DELIVERY_DEFAULTS_KIND
from src.domain.delivery_routing import DRIVER_KIND, ROUTING_EVENT_KIND
from src.domain.document_column_mappings import MAPPING_ENTITY_KIND
from src.domain.document_templates import DOCUMENT_TEMPLATE_SETTING_KIND
from src.domain.export_security import EXPORT_PASSWORD_SETTING_KIND
from src.domain.projections import (
    ASSIGNMENT_KIND,
    CUSTOM_FIELD_KIND,
    ENROLLMENT_KIND,
    HOUSEHOLD_KIND,
    MEMBERSHIP_KIND,
    PERSON_KIND,
    STATUS_EVENT_KIND,
)
from src.domain.provider_settings import PROVIDER_SETTING_KIND
from src.domain.sequence_settings import SEQUENCE_MODE_SETTING_KIND
from src.domain.revisions import (
    AuditChange,
    ChangeOperation,
    HistoricalSnapshot,
    RevisionRecord,
)
from src.domain.school_calendar import SCHOOL_LUNCH_SCHEDULE_KIND
from src.domain.service_adjustments import SERVICE_ADJUSTMENT_SCHEDULE_KIND
from src.domain.unit_pricing import UNIT_PRICE_SCHEDULE_KIND
from src.importing.households import HOUSEHOLD_MATCHING_RULE_KIND
from src.importing.profile_history import IMPORT_PROFILE_KIND

_KIND_LABELS = {
    PERSON_KIND: "개인정보",
    HOUSEHOLD_KIND: "가구",
    MEMBERSHIP_KIND: "가구원",
    ENROLLMENT_KIND: "가입기간",
    ASSIGNMENT_KIND: "배송배정",
    CUSTOM_FIELD_KIND: "사용자 정의 항목",
    STATUS_EVENT_KIND: "상태 변경",
    DRIVER_KIND: "배송담당",
    ROUTING_EVENT_KIND: "배송 변경",
    COMPLAINT_KIND: "민원",
    DELIVERY_DEFAULTS_KIND: "식종별 배송 기본값",
    PROVIDER_SETTING_KIND: "공통 제공기관",
    SEQUENCE_MODE_SETTING_KIND: "배송순서 입력 방식",
    EXPORT_PASSWORD_SETTING_KIND: "Excel 비밀번호",
    MAPPING_ENTITY_KIND: "출력 칼럼 연결",
    DOCUMENT_TEMPLATE_SETTING_KIND: "문서 양식",
    UNIT_PRICE_SCHEDULE_KIND: "급식 단가",
    SERVICE_ADJUSTMENT_SCHEDULE_KIND: "휴무·대체식",
    SCHOOL_LUNCH_SCHEDULE_KIND: "학교 급식 일정",
    IMPORT_PROFILE_KIND: "Excel 가져오기 설정",
    HOUSEHOLD_MATCHING_RULE_KIND: "가구 묶기 기준",
}

_OPERATION_LABELS = {
    ChangeOperation.INSERT: "추가",
    ChangeOperation.UPDATE: "수정",
    ChangeOperation.DELETE: "삭제",
}

_FIELD_LABELS = {
    "name": "성명",
    "birth_date": "생년월일",
    "school_type": "취학유형",
    "school_name": "학교명",
    "grade": "학년",
    "class_name": "반",
    "household_head_name": "세대주 성명",
    "relationship_to_child": "아동과의 관계",
    "jurisdiction": "관할동",
    "provider": "제공기관",
    "telephone": "전화번호",
    "mobile_phone": "핸드폰",
    "note": "비고",
    "road_address": "도로명주소",
    "detail_address": "상세주소",
    "custom_values": "사용자 정의 값",
    "periods": "기간",
    "starts_on": "시작일",
    "ends_on": "종료일",
    "status": "상태",
    "source_text": "원문",
    "driver": "배송담당",
    "driver_ids": "배송담당",
    "sequence": "배송순서",
    "content": "내용",
    "channel": "채널",
    "received_at": "접수 시각",
    "profile": "양식",
    "document_kind": "문서 종류",
    "overrides": "연결",
    "default_unit_price": "기본 단가",
    "changes": "변경 항목",
    "meals": "식종",
    "weekdays": "제공 요일",
    "delivery_weekdays": "배송 요일",
    "rule": "가구 묶기 규칙",
    "criteria": "묶기 기준",
    "address": "주소",
    "match_mode": "기준 적용 방식",
    "blank_values_match": "빈 값 일치 허용",
    "source_kind": "원본 종류",
    "profile_columns": "인적사항 칼럼 연결",
    "combined_meals_column": "통합 식종 칼럼",
    "default_meals": "기본 가입 식종",
    "meal_flag_columns": "식종 칼럼 연결",
    "driver_column": "배송담당 칼럼",
    "sequence_column": "배송순서 칼럼",
    "source_reference_column": "원본 참조 칼럼",
    "custom_field_columns": "추가 항목 칼럼 연결",
    "custom_field_names": "추가 항목 이름",
    "header_row": "머리글 행",
    "data_start_row": "데이터 시작 행",
    "fallback_profile_columns": "보조 인적사항 칼럼",
    "meal_true_values": "가입 표시 값",
    "meal_false_values": "미가입 표시 값",
    "date_normalization": "날짜 변환 방식",
    "phone_normalization": "전화번호 변환 방식",
    "ignored_columns": "제외 칼럼",
    "provider_value": "제공기관",
    "mode": "입력 방식",
}

_INTERNAL_FIELDS = {
    "person_id",
    "revision_id",
    "recorded_at",
    "changed_in_revision_id",
    "policy_version",
    "version",
    "profile_id",
    "rule_id",
    "header_fingerprint",
    "normalization_version",
}

_KNOWN_KINDS = frozenset(_KIND_LABELS)


def build_revision_change_rows(
    record: RevisionRecord,
    snapshot: HistoricalSnapshot,
    *,
    previous_snapshot: HistoricalSnapshot | None = None,
) -> tuple[tuple[str, str, str, str], ...]:
    """Return 대상/식종/구분/변경 내용 rows for one revision.

    The function deliberately does not apply the person-history import filter: a
    revision browser is an audit view and must expose every recorded change.
    """
    if not isinstance(record, RevisionRecord):
        raise TypeError("record는 RevisionRecord여야 합니다.")
    if not isinstance(snapshot, HistoricalSnapshot):
        raise TypeError("snapshot은 HistoricalSnapshot이어야 합니다.")
    if previous_snapshot is not None and not isinstance(previous_snapshot, HistoricalSnapshot):
        raise TypeError("previous_snapshot은 HistoricalSnapshot 또는 None이어야 합니다.")

    return tuple(
        (
            _target_text(change, snapshot, previous_snapshot),
            _meal_text(change),
            _KIND_LABELS.get(change.entity_kind, change.entity_kind),
            _change_text(change),
        )
        for change in record.changes
    )


def _target_text(
    change: AuditChange,
    snapshot: HistoricalSnapshot,
    previous_snapshot: HistoricalSnapshot | None,
) -> str:
    if change.entity_kind not in _KNOWN_KINDS:
        return change.entity_kind
    state = change.after or change.before or {}
    if change.entity_kind == PERSON_KIND:
        name = _person_name(change.entity_id, snapshot, previous_snapshot, state)
        return name or str(change.entity_id)

    person_id = _uuid(state.get("person_id"))
    if person_id is not None:
        name = _person_name(person_id, snapshot, previous_snapshot)
        return name or str(person_id)

    for key in ("name", "school_name"):
        value = state.get(key)
        if isinstance(value, str) and value.strip():
            return value

    for source in (snapshot, previous_snapshot):
        if source is None:
            continue
        stored = source.entity(change.entity_kind, change.entity_id)
        if stored is None:
            continue
        for key in ("name", "school_name"):
            value = stored.get(key)
            if isinstance(value, str) and value.strip():
                return value

    return _KIND_LABELS.get(change.entity_kind, change.entity_kind)


def _person_name(
    person_id: UUID,
    snapshot: HistoricalSnapshot,
    previous_snapshot: HistoricalSnapshot | None,
    fallback: Mapping | None = None,
) -> str:
    for source in (snapshot, previous_snapshot):
        if source is None:
            continue
        state = source.entity(PERSON_KIND, person_id)
        if state is not None:
            name = state.get("name")
            if isinstance(name, str) and name.strip():
                return name
    if fallback is not None:
        name = fallback.get("name")
        if isinstance(name, str) and name.strip():
            return name
    return ""


def _meal_text(change: AuditChange) -> str:
    if change.entity_kind not in _KNOWN_KINDS:
        return ""
    state = change.after or change.before or {}
    meal = state.get("meal")
    if isinstance(meal, str):
        return meal
    meals = state.get("meals")
    if isinstance(meals, Sequence) and not isinstance(meals, (str, bytes)):
        return ", ".join(str(item) for item in meals if not isinstance(item, Mapping))
    driver_ids = state.get("driver_ids")
    if isinstance(driver_ids, Mapping):
        return ", ".join(str(item) for item in driver_ids)
    return ""


def _change_text(change: AuditChange) -> str:
    if change.entity_kind == EXPORT_PASSWORD_SETTING_KIND:
        return "비밀번호 설정 변경"
    if change.entity_kind not in _KNOWN_KINDS:
        return f"{change.entity_kind} · {_OPERATION_LABELS[change.operation]}"
    if change.entity_kind == STATUS_EVENT_KIND:
        return _status_change_text(change)

    before = change.before or {}
    after = change.after or {}
    fields = sorted(set(before) | set(after))
    details = []
    for field in fields:
        if field in _INTERNAL_FIELDS or _is_sensitive(field):
            continue
        old = before.get(field)
        new = after.get(field)
        if old == new:
            continue
        label = _FIELD_LABELS.get(field, field)
        if change.operation is ChangeOperation.INSERT:
            details.append(f"{label}: {_value_text(field, new)}")
        elif change.operation is ChangeOperation.DELETE:
            details.append(f"{label}: {_value_text(field, old)}")
        else:
            details.append(
                f"{label}: {_value_text(field, old)} → {_value_text(field, new)}"
            )
    operation = _OPERATION_LABELS[change.operation]
    return operation if not details else f"{operation} · {' · '.join(details)}"


def _status_change_text(change: AuditChange) -> str:
    before = "" if change.before is None else _status_state_text(change.before)
    after = "" if change.after is None else _status_state_text(change.after)
    if before and after:
        return f"변경 전\n{before}\n변경 후\n{after}"
    operation = _OPERATION_LABELS[change.operation]
    return f"{operation} · {after or before}" if after or before else operation


def _status_state_text(state: Mapping) -> str:
    parts = []
    if "status" in state:
        parts.append(f"상태: {_value_text('status', state.get('status'))}")
    if "starts_on" in state or "ends_on" in state:
        parts.append(f"기간: {_period_text(state.get('starts_on'), state.get('ends_on'))}")
    source = state.get("source_text")
    if "source_text" in state and not _is_generated_status_source(state, source):
        parts.append(f"원문: {_value_text('source_text', source)}")
    return "\n".join(parts) or "상태 기록"


def _period_text(start: object, end: object) -> str:
    if start and end:
        return f"{start} ~ {end}"
    if start:
        return f"{start}부터 · 종료일 미지정"
    if end:
        return f"시작일 미지정 · {end}까지"
    return "시작일·종료일 미지정"


def _is_generated_status_source(state: Mapping, source: object) -> bool:
    if not isinstance(source, str):
        return False
    meal = state.get("meal")
    status = state.get("status")
    if not isinstance(meal, str) or not isinstance(status, str):
        return False
    start = state.get("starts_on") or "이전부터"
    end = state.get("ends_on") or "계속"
    return source == f"{meal} {status} · {start} ~ {end}"


def _value_text(field: str, value: object, *, depth: int = 0) -> str:
    if _is_sensitive(field):
        return "설정됨"
    if value is None or value == "":
        return "미지정"
    if isinstance(value, bool):
        return "예" if value else "아니요"
    if field == "criteria" and isinstance(value, str):
        return _FIELD_LABELS.get(value, value)
    if field == "match_mode" and value == "all":
        return "모든 기준 일치"
    if field == "mode" and value in {"separate", "unified"}:
        return "식종별로 따로 입력" if value == "separate" else "한 번 입력해 모든 식종에 적용"
    if field == "periods" and isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return " / ".join(
            _period_text(item.get("starts_on"), item.get("ends_on"))
            if isinstance(item, Mapping)
            else str(item)
            for item in value
        ) or "없음"
    if isinstance(value, Mapping):
        if depth >= 3:
            return "설정 값"
        items = []
        for key, item in value.items():
            key_text = str(key)
            if key_text in _INTERNAL_FIELDS or _is_sensitive(key_text):
                continue
            items.append(
                f"{_FIELD_LABELS.get(key_text, key_text)}: "
                f"{_value_text(key_text, item, depth=depth + 1)}"
            )
        return ", ".join(items) or "설정 값"
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        rendered = [_value_text(field, item, depth=depth + 1) for item in value]
        return " / ".join(rendered) if rendered else "없음"
    return str(value)


def _uuid(value: object) -> UUID | None:
    if not isinstance(value, str):
        return None
    try:
        return UUID(value)
    except ValueError:
        return None


def _is_sensitive(field: str) -> bool:
    lowered = field.casefold()
    return any(token in lowered for token in ("password", "secret", "token"))
