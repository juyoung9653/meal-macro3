"""Presentation rows for every change in one immutable revision."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from zoneinfo import ZoneInfo
import re
from uuid import UUID

from src.domain.complaints import COMPLAINT_KIND
from src.domain.delivery_defaults import DELIVERY_DEFAULTS_KIND
from src.domain.delivery_routing import DRIVER_KIND, DRIVER_ORDER_KIND, ROUTING_EVENT_KIND
from src.domain.daily_driver_order import DAILY_DRIVER_ORDER_KIND
from src.domain.document_column_mappings import MAPPING_ENTITY_KIND
from src.domain.document_templates import DOCUMENT_TEMPLATE_SETTING_KIND
from src.domain.invoice_settings import INVOICE_SETTINGS_KIND
from src.domain.pause_conversion import PAUSE_CONVERSION_KIND
from src.domain.daily_report_choices import CHOICES_KIND
from src.domain.export_security import EXPORT_PASSWORD_SETTING_KIND
from src.domain.meal_history_cancellation import CANCELLATION_KIND, CANCELLATION_DECISION
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
from src.domain.household_grouping import GROUPING_KIND
from src.domain.household_shared import HOUSEHOLD_SHARED_KIND, shared_columns_from_state, shared_exceptions_from_state
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
    CANCELLATION_KIND: "식종 변경 등록 취소",
    DRIVER_KIND: "배송담당",
    DRIVER_ORDER_KIND: "기사 표시 순서",
    DAILY_DRIVER_ORDER_KIND: "일일식수 기사 순서",
    ROUTING_EVENT_KIND: "배송 변경",
    COMPLAINT_KIND: "민원",
    DELIVERY_DEFAULTS_KIND: "식종별 배송 기본값",
    PROVIDER_SETTING_KIND: "공통 제공기관",
    SEQUENCE_MODE_SETTING_KIND: "배송순서 입력 방식",
    HOUSEHOLD_SHARED_KIND: "가구 공용 칼럼",
    GROUPING_KIND: "가구 묶기 기준",
    EXPORT_PASSWORD_SETTING_KIND: "엑셀 비밀번호",
    MAPPING_ENTITY_KIND: "출력 칼럼 연결",
    DOCUMENT_TEMPLATE_SETTING_KIND: "문서 양식",
    INVOICE_SETTINGS_KIND: "식수·청구서 설정",
    PAUSE_CONVERSION_KIND: "일시중지 자동변환",
    CHOICES_KIND: "일일식수 식종·배송 형태 선택",
    UNIT_PRICE_SCHEDULE_KIND: "급식 단가",
    SERVICE_ADJUSTMENT_SCHEDULE_KIND: "휴무·대체식",
    SCHOOL_LUNCH_SCHEDULE_KIND: "학교 급식 일정",
    IMPORT_PROFILE_KIND: "엑셀 불러오기 설정",
    HOUSEHOLD_MATCHING_RULE_KIND: "가구 묶기 기준",
}

_OPERATION_LABELS = {
    ChangeOperation.INSERT: "추가",
    ChangeOperation.UPDATE: "수정",
    ChangeOperation.DELETE: "삭제",
}

_FIELD_LABELS = {
    "same_month": "같은 달 안에서 자동변환",
    "excluded_forms": "일일식수 제외 배송 형태",
    "include_next_month": "다음 달 식수 포함",
    "confirmation_single_meal_first": "확인서 1식 우선 배치",
    "confirmation_include_next_month_weekend": "확인서 다음 달 주말 표시",
    "school_history_column": "급식 청구서 이력 칼럼",
    "lunchbox_history_column": "행복도시락 청구서 이력 칼럼",
    "remarks": "비고 표시 설정",
    "columns": "비고에 표시할 항목",
    "position": "비고를 넣을 위치",
    "separate_forms": "따로 표시할 배송 형태",
    "include_excluded_deliveries": "제외한 배송도 표시",
    "fallbacks": "값이 없을 때 사용할 항목",
    "header_row_count": "머리글 줄 수",
    "unit_price": "한 끼 단가",
    "covered_periods": "확인한 기간",
    "days": "날짜별 급식 일정",
    "service_on": "식사일",
    "delivery_on": "배송일",
    "mark": "학교 급식 여부",
    "grades_without_school_meal": "학교 급식이 없는 학년",
    "period_start": "시작일",
    "period_end": "종료일",
    "treatment": "식사 제공 방법",
    "default_delivery_on": "대체 배송일",
    "delivery_overrides": "식사별 배송일",
    "origin": "설정 경로",
    "public_holiday": "공휴일",
    "day": "날짜",
    "mobile": "핸드폰",
    "school": "학교명",
    "breakfast": "조식",
    "lunch": "중식",
    "dinner": "석식",
    "source": "가져올 항목",
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
    "ordered_driver_ids": "기사 표시 순서",
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
    "row_filter": "가져올 행 조건",
    "column": "조건 칼럼",
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
    "meal": "식종",
    "effective_on": "적용일",
    "delivery_settings": "식종별 배송 설정",
    "field_type": "항목 종류",
    "display_order": "표시 순서",
    "choices": "선택 항목",
    "form": "제공 형태",
    "value": "값",
    "household_id": "가구 연결",
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
    "position", "change_id", "setting_revision_id", "normalized_school_name", "provider_id", "provider_version",
}

_KNOWN_KINDS = frozenset(_KIND_LABELS)
_WEEKDAYS = ("월", "화", "수", "목", "금", "토", "일")
_MEAL_ORDER = {"석식": 0, "조식": 1, "중식": 2}
_MEAL_COLUMN_KEYS = {"석식": "status:dinner", "조식": "status:breakfast", "중식": "status:lunch"}
_ROSTER_FIELD_KEYS = {"school_name": "school", "mobile_phone": "mobile", "road_address": "address"}


def build_revision_change_rows(
    record: RevisionRecord,
    snapshot: HistoricalSnapshot,
    *,
    previous_snapshot: HistoricalSnapshot | None = None,
    column_labels: Mapping[str, str] | None = None,
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
    if column_labels is not None and not isinstance(column_labels, Mapping):
        raise TypeError("column_labels는 Mapping 또는 None이어야 합니다.")

    current_households = _household_members(snapshot)
    previous_households = _household_members(previous_snapshot)
    current_assignments = _household_assignments(snapshot)
    previous_assignments = _household_assignments(previous_snapshot)
    grouped_meals, grouped_followers = _status_groups(record.changes)
    linked_enrollments = _delivery_enrollment_changes(record)
    enrollment_indices = {index for index, _ in linked_enrollments.values()}
    rows = []
    changed_people = {}
    for index, change in enumerate(record.changes):
        if index in grouped_followers or index in enrollment_indices:
            continue
        if change.entity_kind == CANCELLATION_KIND:
            rows.append(_cancellation_row(change, record, snapshot, previous_snapshot))
            continue
        target = _target_text(change, snapshot, previous_snapshot)
        if change.entity_kind == HOUSEHOLD_KIND:
            members = _active_household_names(
                change.entity_id, current_assignments, snapshot, record.metadata.effective_on,
            ) or _active_household_names(
                change.entity_id, previous_assignments, previous_snapshot, record.metadata.effective_on,
            )
            if members:
                target = "\n".join(members)
        meals = grouped_meals.get(index, (_meal_text(change),))
        meal_text = " · ".join(meal for meal in meals if meal)
        if change.entity_kind == GROUPING_KIND:
            def labels(state):
                return " / ".join(_shared_column_label(key, snapshot, previous_snapshot, column_labels)
                                  for key in (state or {}).get("columns", ("mobile",)))
            rows.append(("전체 명단", "", "가구 묶기 기준", f"이전: {labels(change.before)}\n이후: {labels(change.after)}\n선택한 칼럼 모두 일치"))
            continue
        if change.entity_kind == HOUSEHOLD_SHARED_KIND:
            shared_rows = _shared_change_rows(change, snapshot, previous_snapshot, column_labels)
            if shared_rows:
                rows.extend(shared_rows)
            continue
        if change.entity_kind == STATUS_EVENT_KIND:
            field_label = " · ".join(_status_column_label(meal, column_labels) for meal in meals)
            content = _status_change_text(change, grouped_meals=meal_text)
            for meal in meals:
                linked = linked_enrollments.get(((change.after or {}).get("person_id"), meal))
                if linked is not None:
                    for _key, _label, detail in _field_change_rows(
                        linked[1], snapshot, previous_snapshot,
                        current_households=current_households,
                        previous_households=previous_households,
                        column_labels=column_labels,
                    ):
                        content += f"\n{meal} 가입 기간 변경:\n{detail}"
            row_index = len(rows)
            rows.append((target, meal_text, field_label, content))
            changed_people[row_index] = (_uuid((change.after or {}).get("person_id")),
                                         (change.after or {}).get("starts_on") or record.metadata.effective_on,
                                         (STATUS_EVENT_KIND, meals))
            continue
        if change.entity_kind in _KNOWN_KINDS - {HOUSEHOLD_SHARED_KIND, EXPORT_PASSWORD_SETTING_KIND}:
            fields = _field_change_rows(
                change, snapshot, previous_snapshot,
                current_households=current_households,
                previous_households=previous_households,
                column_labels=column_labels,
            )
            for field_key, label, content in fields:
                row_index = len(rows)
                rows.append((target, meal_text, label, content))
                if change.entity_kind == PERSON_KIND:
                    changed_people[row_index] = (change.entity_id, record.metadata.effective_on,
                                                 (PERSON_KIND, field_key))
            if not fields and any(_is_sensitive(field) and (change.before or {}).get(field) != (change.after or {}).get(field)
                                  for field in set(change.before or {}) | set(change.after or {})):
                rows.append((target, meal_text, _KIND_LABELS[change.entity_kind], "민감한 설정 변경"))
            continue
        rows.append((
            target, meal_text, _KIND_LABELS.get(change.entity_kind, "기타 변경"),
            _change_text(change, snapshot, previous_snapshot),
        ))
    return _group_household_rows(
        rows, changed_people, current_assignments, previous_assignments,
    )


def _delivery_enrollment_changes(record):
    """Keep automatic enrollment details in their delivery row, not duplicate rows."""
    if not any(key in record.metadata.decisions for key in ("meal_status_change", "batch_meal_status_change")):
        return {}
    delivery_targets = {
        (change.after.get("person_id"), change.after.get("meal"))
        for change in record.changes
        if change.entity_kind == STATUS_EVENT_KIND
        and change.operation is ChangeOperation.INSERT
        and change.after is not None and change.after.get("status") == "배송"
    }
    return {
        (change.after.get("person_id"), change.after.get("meal")): (index, change)
        for index, change in enumerate(record.changes)
        if change.entity_kind == ENROLLMENT_KIND and change.after is not None
        and (change.after.get("person_id"), change.after.get("meal")) in delivery_targets
    }


def _cancellation_row(change, record, snapshot, previous_snapshot):
    marker = change.after or change.before or {}
    kind, entity_id = marker.get("entity_kind"), _uuid(marker.get("entity_id"))
    state = None
    for related in record.changes:
        if related.entity_kind == kind and related.entity_id == entity_id:
            state = related.before or related.after
            break
    if state is None and entity_id is not None:
        for source in (previous_snapshot, snapshot):
            if source is not None:
                state = source.entity(kind, entity_id)
                if state is not None:
                    break
    state = state or {}
    person_id = _uuid(state.get("person_id"))
    target = (_person_name(person_id, snapshot, previous_snapshot) if person_id else "") or "식종 변경 기록"
    meal = str(state.get("meal") or "")
    category = "가입 시작" if kind == ENROLLMENT_KIND else str(state.get("status") or "상태 변경")
    action = (
        "등록 취소를 되돌렸습니다." if change.after is None else
        "등록을 취소했습니다." if CANCELLATION_DECISION in record.metadata.decisions else
        "등록 취소 상태를 복구했습니다."
    )
    text = f"{meal} {category} {action}".strip()
    period = marker if kind == ENROLLMENT_KIND else state
    if period.get("starts_on") or period.get("ends_on"):
        text += f"\n적용 기간: {_period_text(period.get('starts_on'), period.get('ends_on'))}"
    return target, meal, "식종 변경 등록 취소", text


def _status_groups(changes: tuple[AuditChange, ...]) -> tuple[dict[int, tuple[str, ...]], set[int]]:
    """Combine one person's matching, newly recorded meal statuses only."""
    candidates: dict[tuple, dict[str, int]] = {}
    for index, change in enumerate(changes):
        if change.entity_kind != STATUS_EVENT_KIND or change.operation is not ChangeOperation.INSERT or change.before is not None:
            continue
        state = change.after or {}
        person_id = _uuid(state.get("person_id"))
        meal = state.get("meal")
        status = state.get("status")
        source = state.get("source_text")
        if person_id is None or meal not in _MEAL_ORDER or not isinstance(status, str) or not isinstance(source, (str, type(None))):
            continue
        if _is_generated_status_source(state, source):
            source = None
        key = (person_id, status, state.get("starts_on"), state.get("ends_on"), source)
        candidates.setdefault(key, {}).setdefault(meal, index)

    grouped_meals = {}
    grouped_followers = set()
    for meals_to_indices in candidates.values():
        if len(meals_to_indices) < 2:
            continue
        indices = tuple(meals_to_indices.values())
        grouped_meals[indices[0]] = tuple(sorted(meals_to_indices, key=_MEAL_ORDER.__getitem__))
        grouped_followers.update(indices[1:])
    return grouped_meals, grouped_followers


def _column_label(key: str, fallback: str, column_labels: Mapping[str, str] | None) -> str:
    alias = None if column_labels is None else column_labels.get(key)
    return alias if isinstance(alias, str) and alias.strip() else fallback


def _status_column_label(meal: str, column_labels: Mapping[str, str] | None) -> str:
    return _column_label(_MEAL_COLUMN_KEYS.get(meal, ""), meal, column_labels)


def _shared_column_label(
    key: str,
    snapshot: HistoricalSnapshot,
    previous_snapshot: HistoricalSnapshot | None,
    column_labels: Mapping[str, str] | None,
) -> str:
    if key.startswith("custom:"):
        fallback = _named_entity(CUSTOM_FIELD_KIND, key[7:], snapshot, previous_snapshot)
    else:
        fallback = {
            "address": "도로명주소", "detail_address": "상세주소",
            "mobile": "핸드폰", "school": "학교명",
            "delivery_settings": "배송 설정",
            "status:dinner": "석식", "status:breakfast": "조식", "status:lunch": "중식",
        }.get(key, _field_label(key))
    return _column_label(key, fallback, column_labels)


def _shared_change_rows(
    change: AuditChange,
    snapshot: HistoricalSnapshot,
    previous_snapshot: HistoricalSnapshot | None,
    column_labels: Mapping[str, str] | None,
) -> tuple[tuple[str, str, str, str], ...]:
    old_columns = shared_columns_from_state(change.before)
    new_columns = shared_columns_from_state(change.after)
    rows = []
    for key in sorted(new_columns - old_columns):
        rows.append(("전체 명단", "", _shared_column_label(key, snapshot, previous_snapshot, column_labels),
                     "가구 공용 지정"))
    for key in sorted(old_columns - new_columns):
        rows.append(("전체 명단", "", _shared_column_label(key, snapshot, previous_snapshot, column_labels),
                     "가구 공용 해제"))
    old_exceptions = shared_exceptions_from_state(change.before)
    new_exceptions = shared_exceptions_from_state(change.after)
    for content, pairs in (("공용 예외 지정", new_exceptions - old_exceptions),
                           ("공용 예외 해제", old_exceptions - new_exceptions)):
        for person_id, key in sorted(pairs, key=lambda pair: (str(pair[0]), pair[1])):
            name = _person_name(person_id, snapshot, previous_snapshot) or "이름을 확인할 수 없는 사람"
            rows.append((name, "", _shared_column_label(key, snapshot, previous_snapshot, column_labels),
                         content))
    return tuple(rows)


def _active_household_names(
    household_id: UUID,
    assignments: Mapping[UUID, tuple],
    snapshot: HistoricalSnapshot | None,
    day: date | None,
) -> tuple[str, ...]:
    if day is None or snapshot is None:
        return ()
    members = [
        (_person_name(person_id, snapshot, None) or "이름을 확인할 수 없는 사람", str(person_id))
        for person_id in assignments
        if _household_at(person_id, day, assignments) == str(household_id)
    ]
    return tuple(name for name, _ in sorted(members))


def _household_assignments(snapshot: HistoricalSnapshot | None) -> dict[UUID, tuple[tuple[str, str | None, str | None], ...]]:
    if snapshot is None:
        return {}
    result: dict[UUID, list[tuple[str, str | None, str | None]]] = {}
    for reference, state in snapshot.entities.items():
        if reference.entity_kind != MEMBERSHIP_KIND:
            continue
        person_id = _uuid(state.get("person_id")) or reference.entity_id
        for period in state.get("periods", ()):
            if isinstance(period, Mapping) and period.get("household_id"):
                result.setdefault(person_id, []).append((
                    str(period["household_id"]), period.get("starts_on"), period.get("ends_on"),
                ))
    return {person_id: tuple(periods) for person_id, periods in result.items()}


def _household_at(person_id: UUID, day: date | str | None,
                  assignments: Mapping[UUID, tuple]) -> str | None:
    day_text = day.isoformat() if isinstance(day, date) else day
    if not day_text:
        return None
    homes = {home for home, start, end in assignments.get(person_id, ())
             if (not start or start <= day_text) and (not end or day_text <= end)}
    return next(iter(homes)) if len(homes) == 1 else None


def _group_household_rows(
    rows: list[tuple[str, str, str, str]],
    changed_people: Mapping[int, tuple[UUID | None, date | str | None, tuple]],
    current_assignments: Mapping[UUID, tuple],
    previous_assignments: Mapping[UUID, tuple],
) -> tuple[tuple[str, str, str, str], ...]:
    groups: dict[tuple, dict[UUID, int]] = {}
    for index, (person_id, day, field_identity) in changed_people.items():
        if person_id is None:
            continue
        home = _household_at(person_id, day, current_assignments) or _household_at(
            person_id, day, previous_assignments,
        )
        if home is None:
            continue
        _, meal, column, content = rows[index]
        groups.setdefault((home, field_identity, meal, column, content), {}).setdefault(person_id, index)
    merged = {}
    followers = set()
    for people in groups.values():
        if len(people) < 2:
            continue
        indices = tuple(people.values())
        merged[indices[0]] = "\n".join(rows[index][0] for index in indices)
        followers.update(indices[1:])
    return tuple((merged.get(index, row[0]), *row[1:])
                 for index, row in enumerate(rows) if index not in followers)


def _field_change_rows(
    change: AuditChange,
    snapshot: HistoricalSnapshot | None,
    previous_snapshot: HistoricalSnapshot | None,
    *,
    current_households: Mapping | None,
    previous_households: Mapping | None,
    column_labels: Mapping[str, str] | None,
) -> tuple[tuple[str, str, str], ...]:
    before = change.before or {}
    after = change.after or {}
    if change.entity_kind == ENROLLMENT_KIND:
        old_periods, new_periods = before.get("periods"), after.get("periods")
        old_meal, new_meal = before.get("meal"), after.get("meal")
        if (not old_periods and not new_periods) or (old_periods == new_periods and old_meal == new_meal):
            return ()
        meal = new_meal or old_meal
        label = _status_column_label(meal, column_labels) if isinstance(meal, str) else "가입기간"
        def period_value(periods, meal_name):
            if not periods:
                return "미지정"
            prefix = f"{meal_name} · " if old_meal and new_meal and old_meal != new_meal and meal_name else ""
            return prefix + "적용 기간: " + _value_text("periods", periods, snapshot, previous_snapshot)
        return (("periods", label, _field_value_change(
            change.operation, period_value(old_periods, old_meal), period_value(new_periods, new_meal),
        )),)
    if change.entity_kind == ASSIGNMENT_KIND:
        old_meal, new_meal = before.get("meal"), after.get("meal")
        meal = new_meal or old_meal
        details = []
        for key in ("driver", "sequence"):
            old_value, new_value = before.get(key), after.get(key)
            if _same_optional_value(old_value, new_value) and (
                old_meal == new_meal or _same_optional_value(old_value, None)
            ):
                continue
            label = _column_label(key, _FIELD_LABELS[key], column_labels)
            context = f"{meal}\n" if isinstance(meal, str) else ""
            details.append((key, label, context + _field_value_change(
                change.operation, _value_text(key, old_value), _value_text(key, new_value),
            )))
        return tuple(details)
    details = []
    for field in sorted(set(before) | set(after)):
        if field in _INTERNAL_FIELDS or _is_sensitive(field):
            continue
        old, new = before.get(field), after.get(field)
        if _same_optional_value(old, new):
            continue
        if field == "custom_values" and isinstance(old or {}, Mapping) and isinstance(new or {}, Mapping):
            for field_id in sorted(set(old or {}) | set(new or {})):
                old_item = (old or {}).get(field_id)
                new_item = (new or {}).get(field_id)
                old_value = old_item.get("value") if isinstance(old_item, Mapping) and set(old_item) == {"kind", "value"} else old_item
                new_value = new_item.get("value") if isinstance(new_item, Mapping) and set(new_item) == {"kind", "value"} else new_item
                if old_item == new_item or (_same_optional_value(old_value, None) and _same_optional_value(new_value, None)):
                    continue
                name = _named_entity(CUSTOM_FIELD_KIND, field_id, snapshot, previous_snapshot)
                label = _column_label(f"custom:{field_id}", name, column_labels)
                if _is_sensitive(name) or _is_sensitive(label):
                    details.append((f"custom:{field_id}", label, "설정 변경"))
                    continue
                details.append((f"custom:{field_id}", label, _field_value_change(
                    change.operation,
                    _value_text("value", old_value, previous_snapshot, snapshot),
                    _value_text("value", new_value, snapshot, previous_snapshot),
                )))
            continue
        label = _field_label(field)
        label = _column_label(_ROSTER_FIELD_KEYS.get(field, field), label, column_labels)
        old_text = _value_text(field, old, previous_snapshot, snapshot,
                               households=previous_households)
        new_text = _value_text(field, new, snapshot, previous_snapshot,
                               households=current_households)
        if change.entity_kind == MEMBERSHIP_KIND and field == "periods":
            label = "같은 가구와 적용 기간"
            old_groups = [p.get("household_id") for p in (old or ()) if isinstance(p, Mapping)]
            new_groups = [p.get("household_id") for p in (new or ()) if isinstance(p, Mapping)]
            if old_groups != new_groups and old and new:
                label = "같은 가구가 바뀌었습니다 · 적용 기간"
        elif field == "household_id" and old and new:
            label = "같은 가구가 바뀌었습니다"
        details.append((field, label, _field_value_change(change.operation, old_text, new_text)))
    return tuple(details)


def _field_value_change(operation: ChangeOperation, old_text: str, new_text: str) -> str:
    return f"이전: {old_text}\n이후: {new_text}"


def _same_optional_value(old: object, new: object) -> bool:
    if old is None or old == "":
        return new is None or new == ""
    if type(old) is not type(new) and not isinstance(old, (Mapping, Sequence)):
        return False
    return old == new


def _target_text(
    change: AuditChange,
    snapshot: HistoricalSnapshot,
    previous_snapshot: HistoricalSnapshot | None,
) -> str:
    if change.entity_kind not in _KNOWN_KINDS:
        return "기타 변경"
    state = change.after or change.before or {}
    if change.entity_kind == PERSON_KIND:
        name = _person_name(change.entity_id, snapshot, previous_snapshot, state)
        return name or "이름을 확인할 수 없는 사람"

    person_id = _uuid(state.get("person_id"))
    if person_id is not None:
        name = _person_name(person_id, snapshot, previous_snapshot)
        return name or "이름을 확인할 수 없는 사람"

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

    return _KIND_LABELS[change.entity_kind]


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
    return ""


def _change_text(change: AuditChange, snapshot=None, previous_snapshot=None) -> str:
    if change.entity_kind == HOUSEHOLD_SHARED_KIND:
        before = shared_columns_from_state(change.before)
        after = shared_columns_from_state(change.after)
        def label(key):
            if key.startswith("custom:"):
                field_id = _uuid(key[7:])
                for source in (snapshot, previous_snapshot):
                    state = source.entity(CUSTOM_FIELD_KIND, field_id) if source is not None and field_id is not None else None
                    if state is not None:
                        return str(state.get("name", "사용자 정의 항목"))
                return "사용자 정의 항목"
            aliases = {"address": "도로명주소", "detail_address": "상세주소", "mobile": "핸드폰", "school": "학교명", "delivery_settings": "배송 설정", "status:dinner": "석식 상태", "status:breakfast": "조식 상태", "status:lunch": "중식 상태"}
            return aliases.get(key, _field_label(key))
        details = []
        if after - before:
            details.append("가구 공용 지정: " + ", ".join(label(key) for key in sorted(after - before)))
        if before - after:
            details.append("가구 공용 해제: " + ", ".join(label(key) for key in sorted(before - after)))
        old_exceptions = shared_exceptions_from_state(change.before)
        new_exceptions = shared_exceptions_from_state(change.after)
        for title, pairs in (("개별 관리 추가", new_exceptions - old_exceptions), ("개별 관리 해제", old_exceptions - new_exceptions)):
            if pairs:
                details.append(title + ": " + ", ".join(
                    f"{_person_name(pid, snapshot, previous_snapshot) or '이름을 확인할 수 없는 사람'} · {label(key)}" for pid, key in sorted(pairs, key=lambda pair: (str(pair[0]), pair[1]))))
        return " · ".join(details) or "가구 공용 설정 변경"

    if change.entity_kind == EXPORT_PASSWORD_SETTING_KIND:
        return "비밀번호 설정 변경"
    if change.entity_kind not in _KNOWN_KINDS:
        return {
            ChangeOperation.INSERT: "항목을 추가했습니다.",
            ChangeOperation.UPDATE: "항목을 수정했습니다.",
            ChangeOperation.DELETE: "항목을 삭제했습니다.",
        }[change.operation]
    if change.entity_kind == STATUS_EVENT_KIND:
        return _status_change_text(change)
    return _OPERATION_LABELS[change.operation]


def _status_change_text(change: AuditChange, *, grouped_meals: str | None = None) -> str:
    before = "" if change.before is None else _status_state_text(change.before)
    after = "" if change.after is None else _status_state_text(change.after)
    state = change.after or change.before or {}
    meal = grouped_meals or state.get("meal") or "식종"
    status = state.get("status") or "상태 변경"
    title = f"{meal} {status}"
    if change.operation is ChangeOperation.INSERT:
        lines = [title]
        if "starts_on" in state or "ends_on" in state:
            lines.append(f"적용 기간: {_period_text(state.get('starts_on'), state.get('ends_on'))}")
        source = state.get("source_text")
        if source and not _is_generated_status_source(state, source):
            lines.append(f"사유: {_value_text('source_text', source)}")
        return "\n".join(lines)
    if before and after:
        return f"{title} 수정\n이전:\n{before}\n이후:\n{after}"
    if before:
        return f"{title} 기록 삭제\n이전:\n{before}\n이후: 기록 삭제"
    return f"{title} 수정\n이전: 미지정\n이후:\n{after}" if after else title


def _status_state_text(state: Mapping) -> str:
    parts = []
    if "status" in state:
        parts.append(f"상태: {_value_text('status', state.get('status'))}")
    if "starts_on" in state or "ends_on" in state:
        parts.append(f"적용 기간: {_period_text(state.get('starts_on'), state.get('ends_on'))}")
    source = state.get("source_text")
    if "source_text" in state and not _is_generated_status_source(state, source):
        parts.append(f"사유: {_value_text('source_text', source)}")
    return "\n".join(parts) or "상태 기록"


def _period_text(start: object, end: object) -> str:
    return f"{start or ''} ~ {end or ''}"


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


_VALUE_LABELS = {
    "field_type": {"text": "문자", "number": "숫자", "date": "날짜", "choice": "선택 목록"},
    "document_kind": {"invoice": "청구서", "confirmation": "확인서", "substitute": "배송대타"},
    "date_normalization": {"auto": "자동 인식", "yyyymmdd": "연월일 숫자 8자리"},
    "phone_normalization": {"korean": "전화번호 형식 정리", "digits": "숫자만 사용", "preserve": "원문 유지"},
    "mode": {"separate": "식종별로 따로 입력", "unified": "한 번 입력해 모든 식종에 적용",
             "nonempty": "값이 있는 행", "equals": "특정 값인 행"},
    "match_mode": {"all": "모든 기준 일치", "any": "하나 이상의 기준 일치"},
    "position": {"above": "기존 비고 위", "below": "기존 비고 아래"},
    "treatment": {"normal": "정상 제공", "not_provided": "미제공 · 청구 제외", "replacement": "대체식 제공 · 청구 포함"},
    "origin": {"manual": "직접 입력", "public_holiday": "공휴일 검토"},
    "mark": {"O": "학교에서 급식 제공", "X": "도시락 배송 필요"},
    "source_kind": {"roster-xlsx": "엑셀 명단", "roster-combined-xlsx": "식종을 한 열에 적은 엑셀 명단", "xlsx": "엑셀 명단"},
}


def _field_label(key):
    if key in _FIELD_LABELS:
        return _FIELD_LABELS[key]
    # Future stored keys must not become English UI labels.
    return "기타 설정" if re.search(r"[A-Za-z]", str(key)) else str(key)


def _reference_label(value, snapshot=None, previous_snapshot=None, *, preserve_unknown=False):
    from src.domain.document_column_sources import INVOICE_SOURCE_LABELS
    if value in INVOICE_SOURCE_LABELS:
        return INVOICE_SOURCE_LABELS[value].replace("템플릿", "양식")
    if value.startswith("custom:"):
        return _named_entity(CUSTOM_FIELD_KIND, value[7:], snapshot, previous_snapshot)
    if value.startswith("status:"):
        return _field_label(value.split(":", 1)[1])
    if ":" in value:
        scope, key = value.split(":", 1)
        return _field_label(scope) + " · " + _reference_label(key, snapshot, previous_snapshot)
    return value if preserve_unknown and value not in _FIELD_LABELS else _field_label(value)


def _source_column_text(token):
    if isinstance(token, str) and re.fullmatch(r"[A-Z]{1,3}", token):
        number = 0
        for letter in token:
            number = number * 26 + ord(letter) - ord("A") + 1
        return f"{number}번째 열"
    return str(token)


def history_reason(reason):
    # Keep operator-entered prose intact; translate legacy generated titles only.
    return {"Excel 명단 가져오기": "엑셀 명단 불러오기", "Excel 명단 불러오기": "엑셀 명단 불러오기"}.get(reason, reason)


def _value_text(
    field: str,
    value: object,
    snapshot: HistoricalSnapshot | None = None,
    previous_snapshot: HistoricalSnapshot | None = None,
    *,
    depth: int = 0,
    households: Mapping | None = None,
) -> str:
    if _is_sensitive(field):
        return "설정됨"
    if value is None or value == "":
        return "미지정"
    if isinstance(value, bool):
        return "예" if value else "아니요"
    if isinstance(value, str) and field in _VALUE_LABELS:
        return _VALUE_LABELS[field].get(value, "기타 설정" if re.search(r"[A-Za-z]", value) else value)
    if field == "columns" and isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return " / ".join(_reference_label(str(item), snapshot, previous_snapshot) for item in value) or "없음"
    if field in {"overrides", "fallbacks"} and isinstance(value, Mapping):
        return ", ".join(
            f"{_reference_label(str(key), snapshot, previous_snapshot, preserve_unknown=True)}: "
            f"{_reference_label(str(item), snapshot, previous_snapshot, preserve_unknown=True) if item else '비움'}"
            for key, item in value.items()
        ) or "없음"
    if field in {"profile_columns", "fallback_profile_columns", "meal_flag_columns", "custom_field_columns"} and isinstance(value, Mapping):
        return ", ".join(
            f"{_named_entity(CUSTOM_FIELD_KIND, key, snapshot, previous_snapshot) if _uuid(str(key)) else _reference_label(str(key), snapshot, previous_snapshot)}: {_source_column_text(item)}"
            for key, item in value.items()
        ) or "없음"
    if field in {"combined_meals_column", "driver_column", "sequence_column", "source_reference_column", "ignored_columns"} and isinstance(value, str):
        return _source_column_text(value)
    if field == "received_at" and isinstance(value, str):
        try:
            stamp = datetime.fromisoformat(value)
            if stamp.tzinfo is not None:
                stamp = stamp.astimezone(ZoneInfo("Asia/Seoul"))
            return stamp.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            return "접수 시각 확인 필요"
    if field == "criteria" and isinstance(value, str):
        return _reference_label(value, snapshot, previous_snapshot)
    if field == "match_mode" and value == "all":
        return "모든 기준 일치"
    if field == "mode" and value in {"separate", "unified"}:
        return "식종별로 따로 입력" if value == "separate" else "한 번 입력해 모든 식종에 적용"
    if field in {"weekdays", "delivery_weekdays"} and isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return ", ".join(
            _WEEKDAYS[day] if type(day) is int and 0 <= day < 7 else str(day)
            for day in value
        ) or "없음"
    if field == "periods" and isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return " / ".join(
            ((_household_text(item["household_id"], households,
                              item.get("starts_on"), item.get("ends_on")) + " · ")
             if "household_id" in item else "")
            + _period_text(item.get("starts_on"), item.get("ends_on"))
            if isinstance(item, Mapping)
            else str(item)
            for item in value
        ) or "없음"
    if field == "driver_ids" and isinstance(value, Mapping):
        return ", ".join(
            f"{meal}: {_named_entity(DRIVER_KIND, driver_id, snapshot, previous_snapshot) if driver_id else '미지정'}"
            for meal, driver_id in value.items()
        ) or "없음"
    if field == "ordered_driver_ids" and isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return " → ".join(_named_entity(DRIVER_KIND, driver_id, snapshot, previous_snapshot) for driver_id in value) or "없음"
    if field == "household_id":
        return _household_text(value, households)
    if field == "custom_values" and isinstance(value, Mapping):
        parts = []
        for field_id, item in value.items():
            name = _named_entity(CUSTOM_FIELD_KIND, field_id, snapshot, previous_snapshot)
            if _is_sensitive(name):
                parts.append(f"{name}: 설정됨")
                continue
            if isinstance(item, Mapping) and set(item) == {"kind", "value"}:
                item = item["value"]
            parts.append(f"{name}: {_value_text('value', item, snapshot, previous_snapshot, depth=depth + 1, households=households)}")
        return ", ".join(parts) or "없음"
    if isinstance(value, Mapping):
        if depth >= 3:
            return "설정 값"
        items = []
        for key, item in value.items():
            key_text = str(key)
            if (key_text in _INTERNAL_FIELDS and not (field == "remarks" and key_text == "position")) or _is_sensitive(key_text):
                continue
            items.append(
                f"{_field_label(key_text)}: "
                f"{_value_text(key_text, item, snapshot, previous_snapshot, depth=depth + 1, households=households)}"
            )
        return ", ".join(items) or "설정 값"
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        rendered = [
            _value_text(field, item, snapshot, previous_snapshot, depth=depth + 1, households=households)
            for item in value
        ]
        return " / ".join(rendered) if rendered else "없음"
    return str(value)


def _household_members(snapshot):
    """Index member names once per snapshot, including their membership dates."""
    result = {}
    if snapshot is None:
        return result
    for reference, state in snapshot.entities.items():
        if reference.entity_kind != MEMBERSHIP_KIND:
            continue
        person_id = _uuid(state.get("person_id")) or reference.entity_id
        name = _person_name(person_id, snapshot, None) or "이름을 확인할 수 없는 사람"
        for period in state.get("periods", ()):
            if isinstance(period, Mapping) and period.get("household_id"):
                result.setdefault(str(period["household_id"]), []).append(
                    (name, period.get("starts_on"), period.get("ends_on")))
    return result


def _household_text(value, households, starts_on=None, ends_on=None):
    names = set()
    for name, member_start, member_end in (households or {}).get(str(value), ()):
        if starts_on and member_end and member_end < starts_on:
            continue
        if ends_on and member_start and member_start > ends_on:
            continue
        names.add(name)
    return (", ".join(sorted(names)) + " 가구") if names else "가구원 정보가 없는 가구"


def _named_entity(
    kind: str,
    value: object,
    snapshot: HistoricalSnapshot | None,
    previous_snapshot: HistoricalSnapshot | None,
) -> str:
    entity_id = _uuid(value)
    if entity_id is not None:
        for source in (snapshot, previous_snapshot):
            state = source.entity(kind, entity_id) if source is not None else None
            if state is not None:
                name = state.get("name")
                if isinstance(name, str) and name.strip():
                    return name
    return "이름을 확인할 수 없는 항목"


def _uuid(value: object) -> UUID | None:
    if not isinstance(value, str):
        return None
    try:
        return UUID(value)
    except ValueError:
        return None


def _is_sensitive(field: str) -> bool:
    lowered = field.casefold()
    return any(token in lowered for token in ("password", "secret", "token", "비밀번호"))
