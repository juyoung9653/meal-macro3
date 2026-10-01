"""Canonical audit projections for roster and status entities.

The functions in this Module are the single translation boundary between typed
domain objects and the full JSON states stored in the revision ledger.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from datetime import date, datetime
from uuid import UUID, uuid5

from .revisions import (
    AuditChange,
    ChangeOperation,
    EntityRef,
    HistoricalSnapshot,
    JsonValue,
    plain_json,
)
from .roster import (
    CustomFieldDefinition,
    CustomFieldType,
    DeliveryAssignment,
    Household,
    HouseholdDeliverySetting,
    HouseholdMembership,
    InclusiveDateRange,
    Meal,
    MealEnrollment,
    Person,
    PersonProfile,
    RosterSnapshot,
)
from .status import MealStatus, StatusEvent


PERSON_KIND = "roster.person"
HOUSEHOLD_KIND = "roster.household"
MEMBERSHIP_KIND = "roster.household_memberships"
ENROLLMENT_KIND = "roster.meal_enrollment"
ASSIGNMENT_KIND = "roster.delivery_assignment"
CUSTOM_FIELD_KIND = "roster.custom_field_definition"
STATUS_EVENT_KIND = "status.event"

ROSTER_ENTITY_KINDS = frozenset(
    {
        PERSON_KIND,
        HOUSEHOLD_KIND,
        MEMBERSHIP_KIND,
        ENROLLMENT_KIND,
        ASSIGNMENT_KIND,
        CUSTOM_FIELD_KIND,
    }
)
TYPED_ENTITY_KINDS = ROSTER_ENTITY_KINDS | {STATUS_EVENT_KIND}

_ENTITY_NAMESPACE = UUID("741f6df2-22c9-46ef-a270-8210db871975")
_ASSIGNMENT_MEALS: tuple[Meal | None, ...] = (None, *tuple(Meal))
_KIND_ORDER = {
    HOUSEHOLD_KIND: 0,
    CUSTOM_FIELD_KIND: 1,
    PERSON_KIND: 2,
    MEMBERSHIP_KIND: 3,
    ENROLLMENT_KIND: 4,
    ASSIGNMENT_KIND: 5,
}


def roster_audit_changes(
    before: RosterSnapshot | None,
    after: RosterSnapshot,
) -> tuple[AuditChange, ...]:
    """Return deterministic full-state changes between two roster snapshots."""

    if before is not None and not isinstance(before, RosterSnapshot):
        raise TypeError("before는 RosterSnapshot 또는 None이어야 합니다.")
    if not isinstance(after, RosterSnapshot):
        raise TypeError("after는 RosterSnapshot이어야 합니다.")

    before_states = {} if before is None else _roster_states(before)
    after_states = _roster_states(after)
    references = sorted(
        before_states.keys() | after_states.keys(),
        key=lambda reference: (
            _KIND_ORDER[reference.entity_kind],
            str(reference.entity_id),
        ),
    )
    changes: list[AuditChange] = []
    for reference in references:
        old = before_states.get(reference)
        new = after_states.get(reference)
        if old is None:
            changes.append(
                AuditChange(
                    reference.entity_kind,
                    reference.entity_id,
                    ChangeOperation.INSERT,
                    before=None,
                    after=new,
                )
            )
        elif new is None:
            changes.append(
                AuditChange(
                    reference.entity_kind,
                    reference.entity_id,
                    ChangeOperation.DELETE,
                    before=old,
                    after=None,
                )
            )
        elif plain_json(old) != plain_json(new):
            changes.append(
                AuditChange(
                    reference.entity_kind,
                    reference.entity_id,
                    ChangeOperation.UPDATE,
                    before=old,
                    after=new,
                )
            )
    return tuple(changes)


def roster_entity_states(
    snapshot: RosterSnapshot,
) -> Mapping[EntityRef, Mapping[str, JsonValue]]:
    """Return all canonical ledger entities represented by a roster snapshot."""

    if not isinstance(snapshot, RosterSnapshot):
        raise TypeError("snapshot은 RosterSnapshot이어야 합니다.")
    return _roster_states(snapshot)


def roster_from_historical_snapshot(snapshot: HistoricalSnapshot) -> RosterSnapshot:
    """Rehydrate a roster from the selected immutable ledger snapshot."""

    if not isinstance(snapshot, HistoricalSnapshot):
        raise TypeError("snapshot은 HistoricalSnapshot이어야 합니다.")
    return roster_from_entity_states(snapshot.revision_id, snapshot.entities)


def roster_from_entity_states(
    revision_id: UUID,
    entities: Mapping[EntityRef, Mapping[str, JsonValue]],
) -> RosterSnapshot:
    """Rehydrate a roster from canonical entity states."""

    if not isinstance(revision_id, UUID):
        raise TypeError("revision ID는 UUID여야 합니다.")
    if not isinstance(entities, Mapping):
        raise TypeError("entities는 mapping이어야 합니다.")

    persons: list[Person] = []
    households: list[Household] = []
    memberships: list[HouseholdMembership] = []
    enrollments: list[MealEnrollment] = []
    assignments: list[DeliveryAssignment] = []
    custom_fields: list[CustomFieldDefinition] = []
    for reference, state in entities.items():
        if reference.entity_kind == PERSON_KIND:
            persons.append(person_from_state(reference.entity_id, state))
        elif reference.entity_kind == HOUSEHOLD_KIND:
            households.append(household_from_state(reference.entity_id, state))
        elif reference.entity_kind == MEMBERSHIP_KIND:
            memberships.extend(memberships_from_state(reference.entity_id, state))
        elif reference.entity_kind == ENROLLMENT_KIND:
            enrollments.extend(enrollments_from_state(reference.entity_id, state))
        elif reference.entity_kind == ASSIGNMENT_KIND:
            assignment = assignment_from_state(reference.entity_id, state)
            if assignment is not None:
                assignments.append(assignment)
        elif reference.entity_kind == CUSTOM_FIELD_KIND:
            custom_fields.append(custom_field_from_state(reference.entity_id, state))

    return RosterSnapshot(
        revision_id=revision_id,
        persons=tuple(sorted(persons, key=lambda person: str(person.person_id))),
        households=tuple(
            sorted(households, key=lambda household: str(household.household_id))
        ),
        memberships=tuple(memberships),
        enrollments=tuple(enrollments),
        assignments=tuple(assignments),
        custom_fields=tuple(
            sorted(custom_fields, key=lambda definition: definition.display_order)
        ),
    )


def status_event_audit_change(event: StatusEvent) -> AuditChange:
    """Create the append-only audit change for one raw status event."""

    if not isinstance(event, StatusEvent):
        raise TypeError("event는 StatusEvent여야 합니다.")
    return AuditChange(
        STATUS_EVENT_KIND,
        event.event_id,
        ChangeOperation.INSERT,
        before=None,
        after=status_event_state(event),
    )


def status_event_state(event: StatusEvent) -> Mapping[str, JsonValue]:
    if not isinstance(event, StatusEvent):
        raise TypeError("event는 StatusEvent여야 합니다.")
    return {
        "revision_id": str(event.revision_id),
        "person_id": str(event.person_id),
        "meal": event.meal.value,
        "status": event.status.value,
        "starts_on": _date_text(event.period.starts_on),
        "ends_on": _date_text(event.period.ends_on),
        "recorded_at": event.recorded_at.isoformat(),
        "source_text": event.source_text,
    }


def status_event_from_state(event_id: UUID, state: Mapping[str, JsonValue]) -> StatusEvent:
    _require_exact_keys(
        state,
        {
            "revision_id",
            "person_id",
            "meal",
            "status",
            "starts_on",
            "ends_on",
            "recorded_at",
            "source_text",
        },
        STATUS_EVENT_KIND,
    )
    event = StatusEvent(
        event_id=event_id,
        revision_id=_uuid_value(state["revision_id"], "revision ID"),
        person_id=_uuid_value(state["person_id"], "사람 ID"),
        meal=_meal_value(state["meal"]),
        status=_status_value(state["status"]),
        period=InclusiveDateRange(
            _date_value(state["starts_on"], "상태 시작일"),
            _date_value(state["ends_on"], "상태 종료일"),
        ),
        recorded_at=_datetime_value(state["recorded_at"], "기록 시각"),
        source_text=_optional_text_value(state["source_text"], "원문 사건"),
    )
    _require_canonical(state, status_event_state(event), STATUS_EVENT_KIND)
    return event


def status_events_from_historical_snapshot(
    snapshot: HistoricalSnapshot,
    *,
    person_id: UUID | None = None,
    meal: Meal | None = None,
) -> tuple[StatusEvent, ...]:
    """Rehydrate raw status events in their original ledger insertion order."""

    if not isinstance(snapshot, HistoricalSnapshot):
        raise TypeError("snapshot은 HistoricalSnapshot이어야 합니다.")
    if person_id is not None and not isinstance(person_id, UUID):
        raise TypeError("사람 ID는 UUID 또는 None이어야 합니다.")
    if meal is not None and not isinstance(meal, Meal):
        raise TypeError("meal은 Meal 또는 None이어야 합니다.")
    events: list[StatusEvent] = []
    for reference, state in snapshot.entities.items():
        if reference.entity_kind != STATUS_EVENT_KIND:
            continue
        event = status_event_from_state(reference.entity_id, state)
        if person_id is not None and event.person_id != person_id:
            continue
        if meal is not None and event.meal is not meal:
            continue
        events.append(event)
    return tuple(events)


def person_from_state(person_id: UUID, state: Mapping[str, JsonValue]) -> Person:
    profile_keys = {
        "name",
        "birth_date",
        "school_type",
        "school_name",
        "grade",
        "class_name",
        "household_head_name",
        "relationship_to_child",
        "jurisdiction",
        "provider",
        "road_address",
        "detail_address",
        "telephone",
        "mobile_phone",
        "note",
        "custom_values",
    }
    _require_exact_keys(state, profile_keys | ({"delivery_settings"} if "delivery_settings" in state else set()), PERSON_KIND)
    custom_state = state["custom_values"]
    if not isinstance(custom_state, Mapping):
        raise TypeError("custom_values는 JSON object여야 합니다.")
    custom_values = {
        _uuid_value(key, "사용자 정의 필드 ID"): _custom_value_from_state(value)
        for key, value in custom_state.items()
    }
    person = Person(
        person_id,
        PersonProfile(
            name=_text_value(state["name"], "성명"),
            birth_date=_date_value(state["birth_date"], "생년월일"),
            school_type=_optional_text_value(state["school_type"], "취학유형"),
            school_name=_optional_text_value(state["school_name"], "학교명"),
            grade=_optional_text_value(state["grade"], "학년"),
            class_name=_optional_text_value(state["class_name"], "반"),
            household_head_name=_optional_text_value(
                state["household_head_name"],
                "세대주 성명",
            ),
            relationship_to_child=_optional_text_value(
                state["relationship_to_child"],
                "아동과의 관계",
            ),
            jurisdiction=_optional_text_value(state["jurisdiction"], "관할동"),
            provider=_optional_text_value(state["provider"], "제공기관"),
            road_address=_optional_text_value(state["road_address"], "도로명주소"),
            detail_address=_optional_text_value(state["detail_address"], "상세주소"),
            telephone=_optional_text_value(state["telephone"], "전화번호"),
            mobile_phone=_optional_text_value(state["mobile_phone"], "핸드폰"),
            note=_optional_text_value(state["note"], "비고"),
            custom_values=custom_values,
        ),
        None if "delivery_settings" not in state else household_from_state(
            person_id, {"delivery_settings": state["delivery_settings"]}
        ).delivery_settings,
    )
    _require_canonical(state, _person_state(person), PERSON_KIND)
    return person


def household_from_state(
    household_id: UUID,
    state: Mapping[str, JsonValue],
) -> Household:
    _require_exact_keys(state, {"delivery_settings"} if state else set(), HOUSEHOLD_KIND)
    settings = state.get("delivery_settings", [])
    if not isinstance(settings, (list, tuple)):
        raise ValueError("가구 배송 설정은 목록이어야 합니다.")
    parsed = []
    for item in settings:
        if not isinstance(item, Mapping) or set(item) not in ({"meal", "weekdays", "form"}, {"meal", "weekdays", "form", "delivery_weekdays"}):
            raise ValueError("가구 배송 설정 항목 형식이 올바르지 않습니다.")
        weekdays = item["weekdays"]
        if weekdays is not None and not isinstance(weekdays, (list, tuple)):
            raise ValueError("제공 요일은 목록이어야 합니다.")
        if not isinstance(item["meal"], str):
            raise ValueError("식종은 문자열이어야 합니다.")
        if item["form"] is not None and not isinstance(item["form"], str):
            raise ValueError("배송 형태는 문자열이어야 합니다.")
        delivery_weekdays = item.get("delivery_weekdays")
        if delivery_weekdays is not None and not isinstance(delivery_weekdays, (list, tuple)):
            raise ValueError("실제 배송 요일은 목록이어야 합니다.")
        parsed.append(HouseholdDeliverySetting(
            meal=Meal(item["meal"]),
            weekdays=None if weekdays is None else tuple(weekdays),
            form=item["form"],
            delivery_weekdays=None if delivery_weekdays is None else tuple(delivery_weekdays),
        ))
    household = Household(household_id, tuple(parsed))
    _require_canonical(state, _household_state(household), HOUSEHOLD_KIND)
    return household


def custom_field_from_state(
    definition_id: UUID,
    state: Mapping[str, JsonValue],
) -> CustomFieldDefinition:
    _require_exact_keys(
        state,
        {"name", "field_type", "display_order", "choices"},
        CUSTOM_FIELD_KIND,
    )
    field_type_text = _text_value(state["field_type"], "사용자 정의 필드 종류")
    try:
        field_type = CustomFieldType(field_type_text)
    except ValueError as error:
        raise ValueError(
            f"알 수 없는 사용자 정의 필드 종류입니다: {field_type_text!r}"
        ) from error
    display_order = state["display_order"]
    if isinstance(display_order, bool) or not isinstance(display_order, int):
        raise TypeError("사용자 정의 필드 표시 순서는 정수여야 합니다.")
    definition = CustomFieldDefinition(
        definition_id,
        _text_value(state["name"], "사용자 정의 필드명"),
        field_type,
        display_order,
        tuple(
            _text_value(choice, "선택값")
            for choice in _sequence_value(state["choices"], "선택값")
        ),
    )
    _require_canonical(state, _custom_field_state(definition), CUSTOM_FIELD_KIND)
    return definition


def memberships_from_state(
    entity_id: UUID,
    state: Mapping[str, JsonValue],
) -> tuple[HouseholdMembership, ...]:
    _require_exact_keys(state, {"person_id", "periods"}, MEMBERSHIP_KIND)
    person_id = _uuid_value(state["person_id"], "사람 ID")
    if entity_id != person_id:
        raise ValueError("가구 소속 entity ID는 사람 ID와 같아야 합니다.")
    periods = _sequence_value(state["periods"], "가구 소속 기간")
    result = tuple(
        HouseholdMembership(
            person_id,
            _uuid_value(_mapping_item(item, "household_id"), "가구 ID"),
            InclusiveDateRange(
                _date_value(_mapping_item(item, "starts_on"), "시작일"),
                _date_value(_mapping_item(item, "ends_on"), "종료일"),
            ),
        )
        for item in periods
    )
    canonical = _membership_state(person_id, result)
    _require_canonical(state, canonical, MEMBERSHIP_KIND)
    return result


def enrollments_from_state(
    entity_id: UUID,
    state: Mapping[str, JsonValue],
) -> tuple[MealEnrollment, ...]:
    _require_exact_keys(state, {"person_id", "meal", "periods"}, ENROLLMENT_KIND)
    person_id = _uuid_value(state["person_id"], "사람 ID")
    meal = _meal_value(state["meal"])
    if entity_id != enrollment_entity_id(person_id, meal):
        raise ValueError("식종 가입 entity ID가 사람·식종과 일치하지 않습니다.")
    periods = _sequence_value(state["periods"], "식종 가입 기간")
    result = tuple(
        MealEnrollment(
            person_id,
            meal,
            InclusiveDateRange(
                _date_value(_mapping_item(item, "starts_on"), "시작일"),
                _date_value(_mapping_item(item, "ends_on"), "종료일"),
            ),
        )
        for item in periods
    )
    canonical = _enrollment_state(person_id, meal, result)
    _require_canonical(state, canonical, ENROLLMENT_KIND)
    return result


def assignment_from_state(
    entity_id: UUID,
    state: Mapping[str, JsonValue],
) -> DeliveryAssignment | None:
    _require_exact_keys(
        state,
        {"person_id", "meal", "driver", "sequence"},
        ASSIGNMENT_KIND,
    )
    person_id = _uuid_value(state["person_id"], "사람 ID")
    meal_value = state["meal"]
    meal = None if meal_value is None else _meal_value(meal_value)
    if entity_id != assignment_entity_id(person_id, meal):
        raise ValueError("배송 배정 entity ID가 사람·식종과 일치하지 않습니다.")
    driver = _optional_text_value(state["driver"], "배송담당")
    sequence = _optional_text_value(state["sequence"], "배송순서")
    if driver is None and sequence is None:
        canonical = _assignment_state(person_id, meal, None)
        _require_canonical(state, canonical, ASSIGNMENT_KIND)
        return None
    assignment = DeliveryAssignment(person_id, meal, driver, sequence)
    _require_canonical(
        state,
        _assignment_state(person_id, meal, assignment),
        ASSIGNMENT_KIND,
    )
    return assignment


def enrollment_entity_id(person_id: UUID, meal: Meal) -> UUID:
    if not isinstance(person_id, UUID):
        raise TypeError("사람 ID는 UUID여야 합니다.")
    if not isinstance(meal, Meal):
        raise TypeError("meal은 Meal이어야 합니다.")
    return uuid5(_ENTITY_NAMESPACE, f"enrollment/{person_id}/{meal.value}")


def assignment_entity_id(person_id: UUID, meal: Meal | None) -> UUID:
    if not isinstance(person_id, UUID):
        raise TypeError("사람 ID는 UUID여야 합니다.")
    if meal is not None and not isinstance(meal, Meal):
        raise TypeError("meal은 Meal 또는 None이어야 합니다.")
    meal_key = "common" if meal is None else meal.value
    return uuid5(_ENTITY_NAMESPACE, f"assignment/{person_id}/{meal_key}")


def _roster_states(
    snapshot: RosterSnapshot,
) -> dict[EntityRef, Mapping[str, JsonValue]]:
    states: dict[EntityRef, Mapping[str, JsonValue]] = {}
    memberships_by_person: defaultdict[UUID, list[HouseholdMembership]] = defaultdict(list)
    enrollments_by_key: defaultdict[tuple[UUID, Meal], list[MealEnrollment]] = defaultdict(list)
    assignments_by_key = {
        (assignment.person_id, assignment.meal): assignment
        for assignment in snapshot.assignments
    }
    for membership in snapshot.memberships:
        memberships_by_person[membership.person_id].append(membership)
    for enrollment in snapshot.enrollments:
        enrollments_by_key[(enrollment.person_id, enrollment.meal)].append(enrollment)

    for household in snapshot.households:
        states[EntityRef(HOUSEHOLD_KIND, household.household_id)] = _household_state(household)
    for definition in snapshot.custom_fields:
        states[
            EntityRef(CUSTOM_FIELD_KIND, definition.definition_id)
        ] = _custom_field_state(definition)
    for person in snapshot.persons:
        person_id = person.person_id
        states[EntityRef(PERSON_KIND, person_id)] = _person_state(person)
        states[EntityRef(MEMBERSHIP_KIND, person_id)] = _membership_state(
            person_id,
            memberships_by_person[person_id],
        )
        for meal in Meal:
            states[
                EntityRef(ENROLLMENT_KIND, enrollment_entity_id(person_id, meal))
            ] = _enrollment_state(
                person_id,
                meal,
                enrollments_by_key[(person_id, meal)],
            )
        for meal in _ASSIGNMENT_MEALS:
            states[
                EntityRef(ASSIGNMENT_KIND, assignment_entity_id(person_id, meal))
            ] = _assignment_state(
                person_id,
                meal,
                assignments_by_key.get((person_id, meal)),
            )
    return states


def _household_state(household: Household) -> Mapping[str, JsonValue]:
    if not household.delivery_settings:
        return {}
    return {"delivery_settings": [
        {"meal": item.meal.value,
         "weekdays": None if item.weekdays is None else list(item.weekdays),
         "form": item.form,
         **({"delivery_weekdays": list(item.delivery_weekdays)} if item.delivery_weekdays is not None else {})}
        for item in household.delivery_settings
    ]}


def _custom_field_state(
    definition: CustomFieldDefinition,
) -> Mapping[str, JsonValue]:
    return {
        "name": definition.name,
        "field_type": definition.field_type.value,
        "display_order": definition.display_order,
        "choices": list(definition.choices),
    }


def _person_state(person: Person) -> Mapping[str, JsonValue]:
    profile = person.profile
    return {
        "name": profile.name,
        "birth_date": _date_text(profile.birth_date),
        "school_type": profile.school_type,
        "school_name": profile.school_name,
        "grade": profile.grade,
        "class_name": profile.class_name,
        "household_head_name": profile.household_head_name,
        "relationship_to_child": profile.relationship_to_child,
        "jurisdiction": profile.jurisdiction,
        "provider": profile.provider,
        "road_address": profile.road_address,
        "detail_address": profile.detail_address,
        "telephone": profile.telephone,
        "mobile_phone": profile.mobile_phone,
        "note": profile.note,
        "custom_values": {
            str(definition_id): _custom_value_state(value)
            for definition_id, value in sorted(
                profile.custom_values.items(),
                key=lambda item: str(item[0]),
            )
        },
        **({"delivery_settings": _household_state(Household(person.person_id, person.delivery_settings))["delivery_settings"]
            if person.delivery_settings else {"delivery_settings": []}}
           if person.delivery_settings is not None else {}),
    }


def _membership_state(
    person_id: UUID,
    memberships: tuple[HouseholdMembership, ...] | list[HouseholdMembership],
) -> Mapping[str, JsonValue]:
    ordered = sorted(
        memberships,
        key=lambda item: (
            item.period.starts_on or date.min,
            item.period.ends_on or date.max,
            str(item.household_id),
        ),
    )
    return {
        "person_id": str(person_id),
        "periods": [
            {
                "household_id": str(item.household_id),
                "starts_on": _date_text(item.period.starts_on),
                "ends_on": _date_text(item.period.ends_on),
            }
            for item in ordered
        ],
    }


def _enrollment_state(
    person_id: UUID,
    meal: Meal,
    enrollments: tuple[MealEnrollment, ...] | list[MealEnrollment],
) -> Mapping[str, JsonValue]:
    ordered = sorted(
        enrollments,
        key=lambda item: (
            item.period.starts_on or date.min,
            item.period.ends_on or date.max,
        ),
    )
    return {
        "person_id": str(person_id),
        "meal": meal.value,
        "periods": [
            {
                "starts_on": _date_text(item.period.starts_on),
                "ends_on": _date_text(item.period.ends_on),
            }
            for item in ordered
        ],
    }


def _assignment_state(
    person_id: UUID,
    meal: Meal | None,
    assignment: DeliveryAssignment | None,
) -> Mapping[str, JsonValue]:
    return {
        "person_id": str(person_id),
        "meal": None if meal is None else meal.value,
        "driver": None if assignment is None else assignment.driver,
        "sequence": None if assignment is None else assignment.sequence,
    }


def _custom_value_state(value) -> Mapping[str, JsonValue]:
    if isinstance(value, datetime):
        raise TypeError("사용자 정의 필드 날짜는 datetime이 아닌 date여야 합니다.")
    if isinstance(value, date):
        return {"kind": "date", "value": value.isoformat()}
    return {"kind": "scalar", "value": value}


def _custom_value_from_state(value: object):
    if not isinstance(value, Mapping):
        raise TypeError("사용자 정의 필드 상태는 JSON object여야 합니다.")
    _require_exact_keys(value, {"kind", "value"}, "custom value")
    kind = value["kind"]
    if kind == "date":
        parsed = _date_value(value["value"], "사용자 정의 날짜")
        if parsed is None:
            raise ValueError("사용자 정의 날짜는 null일 수 없습니다.")
        return parsed
    if kind == "scalar":
        scalar = value["value"]
        if scalar is not None and not isinstance(scalar, (str, int, float, bool)):
            raise TypeError("사용자 정의 scalar 값이 올바르지 않습니다.")
        return scalar
    raise ValueError(f"알 수 없는 사용자 정의 값 종류입니다: {kind!r}")


def _date_text(value: date | None) -> str | None:
    return None if value is None else value.isoformat()


def _date_value(value: object, label: str) -> date | None:
    if value is None:
        return None
    text = _text_value(value, label)
    try:
        return date.fromisoformat(text)
    except ValueError as error:
        raise ValueError(f"{label} 형식이 올바르지 않습니다: {text!r}") from error


def _datetime_value(value: object, label: str) -> datetime:
    text = _text_value(value, label)
    try:
        return datetime.fromisoformat(text)
    except ValueError as error:
        raise ValueError(f"{label} 형식이 올바르지 않습니다: {text!r}") from error


def _uuid_value(value: object, label: str) -> UUID:
    text = _text_value(value, label)
    try:
        return UUID(text)
    except ValueError as error:
        raise ValueError(f"{label} 형식이 올바르지 않습니다: {text!r}") from error


def _meal_value(value: object) -> Meal:
    text = _text_value(value, "식종")
    try:
        return Meal(text)
    except ValueError as error:
        raise ValueError(f"알 수 없는 식종입니다: {text!r}") from error


def _status_value(value: object) -> MealStatus:
    text = _text_value(value, "상태")
    try:
        return MealStatus(text)
    except ValueError as error:
        raise ValueError(f"알 수 없는 상태입니다: {text!r}") from error


def _text_value(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label}은 문자열이어야 합니다.")
    return value


def _optional_text_value(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _text_value(value, label)


def _sequence_value(value: object, label: str) -> tuple[object, ...]:
    if not isinstance(value, (tuple, list)):
        raise TypeError(f"{label}은 JSON array여야 합니다.")
    return tuple(value)


def _mapping_item(value: object, key: str) -> object:
    if not isinstance(value, Mapping):
        raise TypeError("기간 항목은 JSON object여야 합니다.")
    expected = (
        {"household_id", "starts_on", "ends_on"}
        if "household_id" in value
        else {"starts_on", "ends_on"}
    )
    _require_exact_keys(value, expected, "기간")
    return value[key]


def _require_exact_keys(
    value: Mapping,
    expected: set[str],
    label: str,
) -> None:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} 상태는 JSON object여야 합니다.")
    actual = set(value)
    if actual != expected:
        raise ValueError(
            f"{label} 상태 필드가 올바르지 않습니다: "
            f"expected={sorted(expected)}, actual={sorted(actual)}"
        )


def _require_canonical(
    actual: Mapping,
    canonical: Mapping[str, JsonValue],
    label: str,
) -> None:
    if plain_json(actual) != plain_json(canonical):
        raise ValueError(f"{label} 상태가 canonical 형식이 아닙니다.")
