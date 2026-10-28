"""Opt-in, revision-backed household columns and atomic propagation."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, timedelta
from functools import cached_property
from uuid import UUID, uuid4, uuid5
from zoneinfo import ZoneInfo

from .revisions import (
    AuditChange,
    ChangeOperation,
    EntityRef,
    HistoricalSnapshot,
    RevisionMetadata,
    RevisionRecord,
    RevisionSource,
)
from .roster import (
    CustomFieldType,
    DeliveryAssignment,
    HouseholdDeliverySetting,
    InclusiveDateRange,
    Meal,
)
from .projections import (
    ROSTER_ENTITY_KINDS,
    STATUS_EVENT_KIND,
    roster_audit_changes,
    roster_from_historical_snapshot,
    roster_from_entity_states,
    status_event_from_state,
    status_event_audit_change,
)
from .status import MealStatus, StatusEvent, resolve_status_history
from .pause_conversion import pause_conversion_from_snapshot
from .meal_status_changes import _cover_delivery_period
from .delivery_routing import (
    ROUTING_EVENT_KIND,
    DRIVER_KIND,
    RoutingEvent,
    routing_state_from_snapshot,
    _routing_event_state,
)

HOUSEHOLD_SHARED_KIND = "roster.household_shared_columns"
HOUSEHOLD_SHARED_ID = UUID("a64d3da3-90b7-57de-9974-134391526cc3")
_PROFILE = {
    "name": "name",
    "birth_date": "birth_date",
    "school": "school_name",
    "grade": "grade",
    "telephone": "telephone",
    "mobile": "mobile_phone",
    "school_type": "school_type",
    "household_head_name": "household_head_name",
    "relationship_to_child": "relationship_to_child",
    "jurisdiction": "jurisdiction",
    "note": "note",
}
_LABELS = {
    "name": "성명",
    "birth_date": "생년월일",
    "school": "학교",
    "grade": "학년",
    "telephone": "전화번호",
    "mobile": "핸드폰",
    "school_type": "취학유형",
    "household_head_name": "세대주 성명",
    "relationship_to_child": "아동과의 관계",
    "jurisdiction": "관할동",
    "note": "비고",
}
_DISPLAY_MEALS = (Meal.DINNER, Meal.BREAKFAST, Meal.LUNCH)
_MEALS = {"breakfast": Meal.BREAKFAST, "lunch": Meal.LUNCH, "dinner": Meal.DINNER}


class HouseholdSharedConflict(ValueError):
    """A shared column that can be resolved by an explicit household review."""

    def __init__(self, column_key, effective_on, message):
        super().__init__(message)
        self.column_key = column_key
        self.effective_on = effective_on


@dataclass(frozen=True)
class SharedComponent:
    key: str
    label: str
    choices: tuple[str, ...] = ()
    placeholder: str = ""


@dataclass(frozen=True)
class SharedHouseholdRow:
    household_id: UUID
    member_names: tuple[str, ...]
    values: tuple[str | None, ...]
    existing_values: tuple[tuple[str, ...], ...]


def shared_columns_from_state(state):
    if state is None:
        return frozenset()
    keys = state.get("columns")
    if not isinstance(keys, (tuple, list)) or any(
        not isinstance(key, str) for key in keys
    ):
        raise ValueError("가구 공용 칼럼 설정이 올바르지 않습니다.")
    keys = set(keys)
    if state.get("address_columns_version", 1) < 2 and "address" in keys:
        keys.add("detail_address")
    return frozenset(keys)


def shared_exceptions_from_state(state):
    pairs = set()
    entries = (state or {}).get("exceptions", ())
    if not isinstance(entries, (list, tuple)):
        raise ValueError("가구 공용 예외 설정이 올바르지 않습니다.")
    for entry in entries:
        try:
            pid, column = UUID(entry["person_id"]), entry["column"]
            if not isinstance(column, str):
                raise ValueError()
            pairs.add((pid, column))
            if (state or {}).get("address_columns_version", 1) < 2 and column == "address":
                pairs.add((pid, "detail_address"))
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            raise ValueError("가구 공용 예외 설정이 올바르지 않습니다.") from error
    return frozenset(pairs)


def _shared_state(columns, exceptions):
    state = {"columns": sorted(columns), "address_columns_version": 2}
    if exceptions:
        state["exceptions"] = [
            {"person_id": str(pid), "column": column}
            for pid, column in sorted(exceptions, key=lambda pair: (str(pair[0]), pair[1]))
        ]
    return state


def build_household_shared_exceptions_revision(roster, stored_state, exceptions, *,
        actor, recorded_at, effective_on, revision_id):
    exceptions = frozenset(exceptions)
    people = {person.person_id for person in roster.persons}
    columns = set(available_shared_column_keys(roster))
    if any(pid not in people or column not in columns for pid, column in exceptions):
        raise ValueError("현재 명단에 없는 대상자 또는 칼럼입니다.")
    if exceptions == shared_exceptions_from_state(stored_state):
        return None
    after = _shared_state(shared_columns_from_state(stored_state), exceptions)
    return RevisionRecord(
        RevisionMetadata(revision_id, roster.revision_id, recorded_at, effective_on,
                         actor, "가구 공용 예외 설정", RevisionSource.UI,
                         decisions={"household_shared_exceptions": True}),
        (AuditChange(HOUSEHOLD_SHARED_KIND, HOUSEHOLD_SHARED_ID,
                     ChangeOperation.INSERT if stored_state is None else ChangeOperation.UPDATE,
                     stored_state, after),),
    )


def available_shared_column_keys(roster):
    return (
        *_PROFILE,
        "address",
        "detail_address",
        "sequence",
        "delivery_settings",
        *(f"status:{key}" for key in _MEALS),
        *(f"custom:{field.definition_id}" for field in roster.custom_fields),
    )


def household_shared_components(roster, column_key, *, snapshot=None):
    if column_key not in available_shared_column_keys(roster):
        raise ValueError("현재 명단에 없는 칼럼입니다.")
    if column_key in ("address", "detail_address"):
        return (SharedComponent("road_address", "도로명주소"),) if column_key == "address" else (SharedComponent("detail_address", "상세주소"),)
    if column_key == "sequence":
        return tuple(
            SharedComponent(meal.value, f"{meal.value} 순서") for meal in _DISPLAY_MEALS
        )
    if column_key.startswith("status:"):
        return (
            SharedComponent("status", "상태", ("미가입", "배송", "일시중지", "중지")),
            SharedComponent(
                "ends_on", "종료일", placeholder="YYYY-MM-DD · 무기한은 빈칸"
            ),
            SharedComponent("driver", "배송담당"),
        )
    if column_key == "delivery_settings":
        return tuple(
            component
            for meal in _DISPLAY_MEALS
            for component in (
                SharedComponent(
                    f"{meal.value}:weekdays",
                    f"{meal.value} 제공요일",
                    placeholder="월,화,수 · 기본값은 빈칸",
                ),
                SharedComponent(f"{meal.value}:form", f"{meal.value} 배송형태"),
                SharedComponent(
                    f"{meal.value}:delivery_weekdays",
                    f"{meal.value} 배송요일",
                    placeholder="월,화,수,목,금,토,일 · 기본값은 빈칸",
                ),
            )
        )
    if column_key.startswith("custom:"):
        definition = next(
            field
            for field in roster.custom_fields
            if str(field.definition_id) == column_key[7:]
        )
        return (
            SharedComponent(
                column_key,
                definition.name,
                (("", *definition.choices) if definition.choices else ()),
                "YYYY-MM-DD" if definition.field_type is CustomFieldType.DATE else "",
            ),
        )
    return (
        SharedComponent(
            column_key,
            _LABELS[column_key],
            placeholder="YYYY-MM-DD" if column_key == "birth_date" else "",
        ),
    )


def _events(snapshot):
    return (
        ()
        if snapshot is None
        else tuple(
            status_event_from_state(ref.entity_id, state)
            for ref, state in snapshot.entities.items()
            if ref.entity_kind == STATUS_EVENT_KIND
        )
    )


class _ReadContext:
    """Decode routing and status history at most once for one operation."""

    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.histories = {}

    @cached_property
    def routing(self):
        return routing_state_from_snapshot(self.snapshot)

    @cached_property
    def events(self):
        return _events(self.snapshot)

    @cached_property
    def events_by_person_meal(self):
        grouped = {}
        for event in self.events:
            grouped.setdefault((event.person_id, event.meal), []).append(event)
        return grouped

    def history(self, person_id, meal):
        key = (person_id, meal)
        if key not in self.histories:
            self.histories[key] = resolve_status_history(
                person_id, meal, tuple(self.events_by_person_meal.get(key, ())),
                same_month_pause=pause_conversion_from_snapshot(self.snapshot).same_month,
            )
        return self.histories[key]


def _text(value):
    return (
        ""
        if value is None
        else value.isoformat()
        if isinstance(value, date)
        else str(value)
    )


def _values(roster, key, person_id, on, snapshot=None, *, context=None):
    person = roster.person(person_id)
    if context is None and snapshot is not None:
        context = _ReadContext(snapshot)
    needs_routing = key in ("address", "detail_address", "jurisdiction") or key.startswith("status:")
    view = (
        context.routing.resolve(roster, person_id, on)
        if context is not None and needs_routing
        else None
    )
    if key in _PROFILE:
        value = (
            view.jurisdiction
            if key == "jurisdiction" and view is not None
            else getattr(person.profile, _PROFILE[key])
        )
        return (_text(value),)
    if key.startswith("custom:"):
        return (_text(person.profile.custom_values.get(UUID(key[7:]))),)
    if key in ("address", "detail_address"):
        field = "road_address" if key == "address" else "detail_address"
        return (_text(getattr(view or person.profile, field)),)
    if key == "sequence":
        return tuple(
            roster.resolve_assignment(person_id, meal).sequence or ""
            for meal in _DISPLAY_MEALS
        )
    if key.startswith("status:"):
        meal = _MEALS[key[7:]]
        history = (
            context.history(person_id, meal)
            if context is not None
            else resolve_status_history(person_id, meal, ())
        )
        enrollment = next(
            (
                e
                for e in roster.enrollments
                if e.person_id == person_id and e.meal == meal and e.period.contains(on)
            ),
            None,
        )
        interval = next((i for i in history.intervals if i.period.contains(on)), None)
        ends = []
        if enrollment is not None and enrollment.period.ends_on is not None:
            ends.append(enrollment.period.ends_on)
        if (
            enrollment is not None
            and interval is not None
            and interval.period.ends_on is not None
        ):
            ends.append(interval.period.ends_on)
        return (
            history.status_on(on).value if enrollment is not None else "미가입",
            _text(min(ends)) if ends else "",
            view.drivers[meal]
            if view is not None
            else roster.resolve_assignment(person_id, meal).driver or "",
        )
    settings = {s.meal: s for s in roster.delivery_settings_for(person_id, on=on)}
    weekdays = "월화수목금토일"
    return tuple(
        value
        for meal in _DISPLAY_MEALS
        for value in (
            ",".join(weekdays[d] for d in settings[meal].weekdays)
            if meal in settings and settings[meal].weekdays is not None
            else "",
            settings[meal].form or "" if meal in settings else "",
            ",".join(weekdays[d] for d in settings[meal].delivery_weekdays)
            if meal in settings and settings[meal].delivery_weekdays is not None
            else "",
        )
    )


def household_shared_rows(roster, column_key, on, *, snapshot=None, exceptions=None):
    household_shared_components(roster, column_key, snapshot=snapshot)
    rows = []
    if exceptions is None:
        exceptions = shared_exceptions_from_state(snapshot.entity(HOUSEHOLD_SHARED_KIND, HOUSEHOLD_SHARED_ID) if snapshot else None)
    context = _ReadContext(snapshot) if snapshot is not None else None
    for household in roster.households:
        members = tuple(p for p in roster.household_members(household.household_id, on=on) if (p.person_id, column_key) not in exceptions)
        if not members:
            continue
        values = tuple(
            _values(roster, column_key, p.person_id, on, context=context)
            for p in members
        )
        common = tuple(
            items[0] if len(set(items)) == 1 else None for items in zip(*values)
        )
        rows.append(
            SharedHouseholdRow(
                household.household_id,
                tuple(p.profile.name for p in members),
                common,
                values,
            )
        )
    return tuple(rows)


def _parse_profile(roster, key, text):
    text = text.strip()
    if key == "birth_date":
        return date.fromisoformat(text) if text else None
    if key.startswith("custom:"):
        definition = next(
            field
            for field in roster.custom_fields
            if str(field.definition_id) == key[7:]
        )
        value = text or None
        if text and definition.field_type is CustomFieldType.DATE:
            value = date.fromisoformat(text)
        elif text and definition.field_type is CustomFieldType.NUMBER:
            value = float(text)
            if value.is_integer():
                value = int(value)
        definition.validate_value(value)
        return value
    return text if key == "name" else text or None


def _set_assignment(roster, person_id, meal, *, driver=None, sequence=None, field):
    # Materialize all meal fallbacks before changing one component; a cleared
    # override must not accidentally inherit a common assignment again.
    remaining = [a for a in roster.assignments if a.person_id != person_id]
    for target in Meal:
        old = roster.resolve_assignment(person_id, target)
        d = driver if target == meal and field == "driver" else old.driver
        s = sequence if target == meal and field == "sequence" else old.sequence
        if d or s:
            remaining.append(
                DeliveryAssignment(person_id, target, d or None, s or None)
            )
    return replace(roster, assignments=tuple(remaining))


def _set_values(roster, key, person_id, values, on):
    person = roster.person(person_id)
    if key in _PROFILE or key.startswith("custom:"):
        value = _parse_profile(roster, key, values[0])
        if key.startswith("custom:"):
            custom = dict(person.profile.custom_values)
            field_id = UUID(key[7:])
            if value is None:
                custom.pop(field_id, None)
            else:
                custom[field_id] = value
            profile = replace(person.profile, custom_values=custom)
        else:
            profile = replace(person.profile, **{_PROFILE[key]: value})
        updated = replace(person, profile=profile)
    elif key in ("address", "detail_address"):
        field = "road_address" if key == "address" else "detail_address"
        updated = replace(person, profile=replace(person.profile, **{field: values[0] or None}))
    elif key == "sequence":
        for meal, value in zip(_DISPLAY_MEALS, values):
            if "\n" in value or "\r" in value:
                raise ValueError("배송순서는 한 줄로 입력해주세요.")
            roster = _set_assignment(
                roster, person_id, meal, sequence=value.strip(), field="sequence"
            )
        return roster
    elif key == "delivery_settings":
        settings = []
        for index, meal in enumerate(_DISPLAY_MEALS):
            days, form, delivery = values[index * 3 : index * 3 + 3]

            def parse_days(text):
                return (
                    tuple(
                        "월화수목금토일".index(day.strip()) for day in text.split(",")
                    )
                    if text.strip()
                    else None
                )

            if days.strip() or form.strip() or delivery.strip():
                settings.append(
                    HouseholdDeliverySetting(
                        meal,
                        parse_days(days),
                        form.strip() or None,
                        parse_days(delivery),
                    )
                )
        updated = replace(person, delivery_settings=tuple(settings))
    else:
        raise ValueError("상태 값은 상태 이력과 함께 저장해야 합니다.")
    return replace(
        roster,
        persons=tuple(
            updated if p.person_id == person_id else p for p in roster.persons
        ),
    )


def _trim_enrollment(enrollments, pid, meal, period):
    result = []
    for item in enrollments:
        if (
            item.person_id != pid
            or item.meal != meal
            or not item.period.overlaps(period)
        ):
            result.append(item)
            continue
        if period.starts_on is not None and (
            item.period.starts_on is None or item.period.starts_on < period.starts_on
        ):
            result.append(
                replace(
                    item,
                    period=InclusiveDateRange(
                        item.period.starts_on, period.starts_on - timedelta(days=1)
                    ),
                )
            )
        if period.ends_on is not None and (
            item.period.ends_on is None or item.period.ends_on > period.ends_on
        ):
            result.append(
                replace(
                    item,
                    period=InclusiveDateRange(
                        period.ends_on + timedelta(days=1), item.period.ends_on
                    ),
                )
            )
    return tuple(result)


def _routing_change(
    pid, on, position, *, road=None, detail=None, jurisdiction=None, driver_ids=None
):
    event = RoutingEvent(
        uuid4(), pid, on, position, driver_ids or {}, road, detail, jurisdiction
    )
    return AuditChange(
        ROUTING_EVENT_KIND,
        event.event_id,
        ChangeOperation.INSERT,
        None,
        _routing_event_state(event),
    )


def _address_pair(roster, person_id, on, routing, extra=()):
    """Resolve a complete pair, including earlier changes in this revision."""
    profile = roster.person(person_id).profile
    road, detail = profile.road_address or "", profile.detail_address or ""
    events = {event.event_id: _routing_event_state(event) for event in routing.events} if routing else {}
    for change in extra:
        if change.entity_kind == ROUTING_EVENT_KIND and change.after is not None:
            events[change.entity_id] = change.after
    ordered = sorted(events.values(), key=lambda state: (state["effective_on"], state["position"]))
    for state in ordered:
        if state["person_id"] == str(person_id) and state["effective_on"] <= on.isoformat() and state["road_address"] is not None:
            road, detail = state["road_address"], state["detail_address"] or ""
    return {"road": road, "detail": detail}


def _shared_period(roster, source_id, target_id, household_id, on, requested):
    periods = [requested]
    for pid in (source_id, target_id):
        membership = next(
            (
                m
                for m in roster.memberships
                if m.person_id == pid
                and m.household_id == household_id
                and m.period.contains(on)
            ),
            None,
        )
        if membership is None:
            return None
        periods.append(membership.period)
    starts = max(p.starts_on or date.min for p in periods)
    ends = min(p.ends_on or date.max for p in periods)
    if starts > ends:
        return None
    return InclusiveDateRange(
        None if starts == date.min else starts, None if ends == date.max else ends
    )


def _subtract_periods(periods, removed):
    remaining = tuple(periods)
    for cut in removed:
        pieces = []
        for period in remaining:
            if not period.overlaps(cut):
                pieces.append(period)
                continue
            if cut.starts_on is not None and (
                period.starts_on is None or period.starts_on < cut.starts_on
            ):
                pieces.append(
                    InclusiveDateRange(
                        period.starts_on, cut.starts_on - timedelta(days=1)
                    )
                )
            if cut.ends_on is not None and (
                period.ends_on is None or period.ends_on > cut.ends_on
            ):
                pieces.append(
                    InclusiveDateRange(cut.ends_on + timedelta(days=1), period.ends_on)
                )
        remaining = tuple(pieces)
    return remaining


def _propagate_enrollments(before, after, working, selected, on, exceptions):
    claims = {}
    old_ids = {p.person_id for p in before.persons}
    for person in after.persons:
        if person.person_id not in old_ids:
            continue
        for name, meal in _MEALS.items():
            if "status:" + name not in selected or (person.person_id, "status:" + name) in exceptions:
                continue
            previous = tuple(
                e.period
                for e in before.enrollments
                if e.person_id == person.person_id and e.meal == meal
            )
            current = tuple(
                e.period
                for e in after.enrollments
                if e.person_id == person.person_id and e.meal == meal
            )
            for adding, periods in (
                (True, _subtract_periods(current, previous)),
                (False, _subtract_periods(previous, current)),
            ):
                for period in periods:
                    day = period.starts_on or on
                    household = after.household_for(person.person_id, on=day)
                    if household is None:
                        continue
                    for member in after.household_members(
                        household.household_id, on=day
                    ):
                        if (member.person_id, "status:" + name) in exceptions:
                            continue
                        clipped = _shared_period(
                            after,
                            person.person_id,
                            member.person_id,
                            household.household_id,
                            day,
                            period,
                        )
                        if clipped is None:
                            continue
                        target = (member.person_id, meal)
                        for old_period, old_adding in claims.get(target, ()):
                            if old_period.overlaps(clipped) and old_adding != adding:
                                raise HouseholdSharedConflict(
                                    "status:" + name, day,
                                    "같은 가구의 식종 가입 변경이 서로 충돌합니다."
                                )
                        claims.setdefault(target, []).append((clipped, adding))
                        enrollments = (
                            _cover_delivery_period(
                                working.enrollments, member.person_id, meal, clipped
                            )
                            if adding
                            else _trim_enrollment(
                                working.enrollments, member.person_id, meal, clipped
                            )
                        )
                        working = replace(working, enrollments=enrollments)
    return working


def _shared_driver_id(routing, name, changes, revision_id):
    name = name.strip()
    if not name:
        return None
    matches = [d for d in routing.drivers if d.name == name]
    if len(matches) > 1:
        raise ValueError(
            "같은 이름의 배송담당이 여러 명입니다. 기사 이름을 구분해주세요."
        )
    if matches:
        return matches[0].driver_id
    driver_id = uuid5(revision_id, "shared-driver:" + name)
    if not any(c.entity_id == driver_id for c in changes):
        changes.append(
            AuditChange(
                DRIVER_KIND,
                driver_id,
                ChangeOperation.INSERT,
                None,
                {"name": name, "telephone": ""},
            )
        )
    return driver_id


def build_household_shared_revision(
    roster,
    stored_state,
    column_key,
    enabled,
    household_values,
    *,
    actor,
    recorded_at,
    effective_on,
    revision_id,
    snapshot=None,
):
    components = household_shared_components(roster, column_key, snapshot=snapshot)
    keys = set(shared_columns_from_state(stored_state))
    exceptions = shared_exceptions_from_state(stored_state)
    rows = household_shared_rows(roster, column_key, effective_on, snapshot=snapshot, exceptions=exceptions)
    if enabled and set(household_values) != {row.household_id for row in rows}:
        raise ValueError("모든 가구의 공용 값을 직접 확인·입력해주세요.")
    working = replace(roster, revision_id=revision_id)
    extra = []
    context = _ReadContext(snapshot) if snapshot is not None else None
    routing = (
        context.routing
        if context is not None
        and (
            column_key in ("address", "detail_address", "jurisdiction")
            or column_key.startswith("status:")
        )
        else None
    )
    position = max((e.position for e in routing.events), default=0) if routing else 0
    if enabled:
        keys.add(column_key)
        for row in rows:
            values = tuple(household_values[row.household_id])
            if len(values) != len(components) or any(
                not isinstance(value, str) for value in values
            ):
                raise ValueError("충돌한 값을 직접 입력하거나 비우기로 확정해주세요.")
            # An explicit opt-in must not rewrite already equal values or their
            # dated history merely to turn on subsequent household propagation.
            if values == row.values:
                continue
            for person in roster.household_members(row.household_id, on=effective_on):
                pid = person.person_id
                if (pid, column_key) in exceptions:
                    continue
                position += 1
                if column_key.startswith("status:"):
                    meal = _MEALS[column_key[7:]]
                    status, end, driver = values
                    if status not in components[0].choices:
                        raise ValueError(
                            "상태는 미가입·배송·일시중지·중지 중 선택해주세요."
                        )
                    if status == "일시중지" and not end.strip():
                        raise ValueError("일시중지는 종료일을 입력해주세요.")
                    ends_on = date.fromisoformat(end) if end.strip() else None
                    if not end.strip():
                        future = [
                            event.period.starts_on
                            for event in (context.events if context is not None else ())
                            if event.person_id == pid
                            and event.meal == meal
                            and event.period.starts_on is not None
                            and event.period.starts_on > effective_on
                        ]
                        if future:
                            ends_on = min(future) - timedelta(days=1)
                    period = InclusiveDateRange(effective_on, ends_on)
                    membership = next(
                        m
                        for m in roster.memberships
                        if m.person_id == pid
                        and m.household_id == row.household_id
                        and m.period.contains(effective_on)
                    )
                    if membership.period.ends_on is not None and (
                        period.ends_on is None
                        or period.ends_on > membership.period.ends_on
                    ):
                        period = InclusiveDateRange(
                            effective_on, membership.period.ends_on
                        )
                    previous = _values(
                        roster, column_key, pid, effective_on, context=context
                    )
                    if (status, end.strip()) != previous[:2]:
                        if status == "미가입":
                            working = replace(
                                working,
                                enrollments=_trim_enrollment(
                                    working.enrollments, pid, meal, period
                                ),
                            )
                        else:
                            event = StatusEvent(
                                uuid4(),
                                revision_id,
                                pid,
                                meal,
                                MealStatus(status),
                                period,
                                recorded_at,
                                "가구 공용 설정",
                            )
                            working = replace(
                                working,
                                enrollments=_cover_delivery_period(
                                    working.enrollments, pid, meal, period
                                ),
                            )
                            extra.append(status_event_audit_change(event))
                    matches = (
                        [d for d in routing.drivers if d.name == driver.strip()]
                        if routing
                        else []
                    )
                    if driver.strip() and len(matches) != 1:
                        # Register an explicitly entered new driver rather than
                        # hiding its value behind an older routing event.
                        if len(matches) > 1:
                            raise ValueError(
                                "같은 이름의 배송담당이 여러 명입니다. 기사 이름을 구분해주세요."
                            )
                        driver_id = uuid5(
                            revision_id, "shared-driver:" + driver.strip()
                        )
                        if not any(c.entity_id == driver_id for c in extra):
                            extra.append(
                                AuditChange(
                                    DRIVER_KIND,
                                    driver_id,
                                    ChangeOperation.INSERT,
                                    None,
                                    {"name": driver.strip(), "telephone": ""},
                                )
                            )
                    else:
                        driver_id = matches[0].driver_id if matches else None
                    extra.append(
                        _routing_change(
                            pid, effective_on, position, driver_ids={meal: driver_id}
                        )
                    )
                else:
                    if column_key not in ("address", "detail_address", "jurisdiction"):
                        working = _set_values(
                            working, column_key, pid, values, effective_on
                        )
                    if column_key in ("address", "detail_address"):
                        pair = _address_pair(roster, pid, effective_on, routing, extra)
                        pair["road" if column_key == "address" else "detail"] = values[0].strip()
                        extra.append(
                            _routing_change(
                                pid,
                                effective_on,
                                position,
                                **pair,
                            )
                        )
                    elif column_key == "jurisdiction":
                        extra.append(
                            _routing_change(
                                pid,
                                effective_on,
                                position,
                                jurisdiction=values[0].strip(),
                            )
                        )
    else:
        keys.discard(column_key)
    after = _shared_state(keys, exceptions)
    changes = list(roster_audit_changes(roster, working)) + extra
    if keys != shared_columns_from_state(stored_state):
        changes.append(
            AuditChange(
                HOUSEHOLD_SHARED_KIND,
                HOUSEHOLD_SHARED_ID,
                ChangeOperation.INSERT
                if stored_state is None
                else ChangeOperation.UPDATE,
                stored_state,
                after,
            )
        )
    if not changes:
        raise ValueError("변경된 가구 공용 설정이 없습니다.")
    return RevisionRecord(
        RevisionMetadata(
            revision_id,
            roster.revision_id,
            recorded_at,
            effective_on,
            actor,
            "가구 공용 칼럼 설정",
            RevisionSource.UI,
            decisions={
                "household_shared_setting": {
                    "column": column_key,
                    "enabled": bool(enabled),
                }
            },
            rule_versions={"household_shared": 1},
        ),
        tuple(changes),
    )


def row_add_comparison_days(roster, person_id, effective_on):
    """Check the add date, delivery starts, and household membership boundaries."""
    days = {effective_on}
    households = {item.household_id for item in roster.memberships if item.person_id == person_id}
    for enrollment in roster.enrollments:
        if enrollment.person_id == person_id and enrollment.period.starts_on is not None:
            days.add(enrollment.period.starts_on)
    for membership in roster.memberships:
        if membership.household_id not in households:
            continue
        if membership.period.starts_on is not None:
            days.add(membership.period.starts_on)
        if membership.period.ends_on is not None:
            days.add(membership.period.ends_on)
            if membership.period.ends_on < date.max:
                days.add(membership.period.ends_on + timedelta(days=1))
    return tuple(sorted(days))


def expand_household_shared_revision(snapshot, record):
    """Expand explicit changed components before the ledger transaction commits."""
    from .import_shared import IMPORT_SHARED_VALUES, apply_import_shared_values

    record = apply_import_shared_values(snapshot, record)
    reviewed = set(record.metadata.decisions.get(IMPORT_SHARED_VALUES, {}))
    selected = shared_columns_from_state(
        snapshot.entity(HOUSEHOLD_SHARED_KIND, HOUSEHOLD_SHARED_ID)
    )
    entities = dict(snapshot.entities)
    for change in record.changes:
        ref = EntityRef(change.entity_kind, change.entity_id)
        if change.after is None:
            entities.pop(ref, None)
        else:
            entities[ref] = change.after
    row_add = record.metadata.decisions.get("roster_row_change", {}).get("operation") == "add"
    # A deliberate settings revision already supplies every household value.
    if not row_add and ("household_shared_setting" in record.metadata.decisions or any(
        c.entity_kind == HOUSEHOLD_SHARED_KIND for c in record.changes
    )):
        return record
    exceptions = shared_exceptions_from_state(snapshot.entity(HOUSEHOLD_SHARED_KIND, HOUSEHOLD_SHARED_ID))
    if row_add:
        pid = UUID(record.metadata.decisions["roster_row_change"]["person_id"])
        proposed_state = entities.get(EntityRef(HOUSEHOLD_SHARED_KIND, HOUSEHOLD_SHARED_ID))
        proposed_exceptions = shared_exceptions_from_state(proposed_state)
        if (shared_columns_from_state(proposed_state) != selected
                or not exceptions <= proposed_exceptions
                or any(person_id != pid or key not in selected
                       for person_id, key in proposed_exceptions - exceptions)):
            raise ValueError("행 추가에서는 새 대상자의 공용 항목 예외만 지정할 수 있습니다.")
        exceptions = proposed_exceptions
    if not selected and not exceptions:
        return record
    before = roster_from_historical_snapshot(snapshot)
    after = roster_from_entity_states(record.metadata.revision_id, entities)
    after_snapshot = HistoricalSnapshot(record.metadata.revision_id, entities)
    after_context = _ReadContext(after_snapshot)
    on = (
        record.metadata.effective_on
        or record.metadata.recorded_at.astimezone(ZoneInfo("Asia/Seoul")).date()
    )
    available = selected & set(available_shared_column_keys(after))
    active = available - reviewed
    correction = record.metadata.decisions.get("profile_routing_correction", {})
    corrected_components = {
        (UUID(item["person_id"]), item["column"])
        for item in correction.get("components", ())
    }
    corrected_events = set(correction.get("event_ids", ()))
    working = after
    proposals = {}
    old_ids = {p.person_id for p in before.persons}
    # New memberships are an explicit merge boundary. Reject differing values
    # before propagation can turn a newly imported member into an arbitrary winner.
    if row_add and (pid in old_ids or {p.person_id for p in after.persons} - old_ids != {pid}):
        raise ValueError("행 추가 대상자가 올바르지 않습니다.")
    comparison_days = row_add_comparison_days(after, pid, on) if row_add else (on,)
    for comparison_day in comparison_days:
        for person in after.persons:
            household = after.household_for(person.person_id, on=comparison_day)
            old_household = (
                before.household_for(person.person_id, on=comparison_day)
                if person.person_id in old_ids
                else None
            )
            if household is None or (
                old_household is not None
                and old_household.household_id == household.household_id
            ):
                continue
            for key in sorted(active):
                if (person.person_id, key) in exceptions:
                    continue
                values = {
                    _values(after, key, member.person_id, comparison_day, context=after_context)
                    for member in after.household_members(household.household_id, on=comparison_day)
                    if (member.person_id, key) not in exceptions
                }
                if len(values) > 1:
                    raise HouseholdSharedConflict(
                        key, comparison_day,
                        f"가구 공용 값 충돌: {key}. 가구를 합치기 전에 공용 값을 일치시켜주세요."
                    )
    # Registration must not rewrite existing members or create duplicate routing
    # events. The merge check above already requires matching non-exempt values.
    if row_add:
        return record
    for person in after.persons:
        household = after.household_for(person.person_id, on=on)
        if household is None:
            continue
        for key in sorted(active):
            if (person.person_id, key) in exceptions or (person.person_id, key) in corrected_components:
                continue
            if key.startswith("status:"):
                # Enrollment and status events are handled below; assignment
                # changes are the driver component of this visible column.
                meal = _MEALS[key[7:]]
                new = (after.resolve_assignment(person.person_id, meal).driver or "",)
                old = (
                    (before.resolve_assignment(person.person_id, meal).driver or "",)
                    if person.person_id in old_ids
                    else None
                )
            else:
                new = _values(after, key, person.person_id, on)
                old = (
                    _values(before, key, person.person_id, on)
                    if person.person_id in old_ids
                    else None
                )
            for index, value in enumerate(new):
                if old is not None and old[index] == value:
                    continue
                target = (household.household_id, key, index)
                if target in proposals and proposals[target] != value:
                    raise HouseholdSharedConflict(
                        key, on,
                        f"가구 공용 값 충돌: {key} · {person.profile.name}. 같은 가구의 입력값을 일치시킨 뒤 다시 저장해주세요."
                    )
                proposals[target] = value
    for (hid, key, index), value in proposals.items():
        for person in after.household_members(hid, on=on):
            if (person.person_id, key) in exceptions:
                continue
            if key.startswith("status:"):
                working = _set_assignment(
                    working,
                    person.person_id,
                    _MEALS[key[7:]],
                    driver=value,
                    field="driver",
                )
            else:
                values = list(_values(working, key, person.person_id, on))
                values[index] = value
                working = _set_values(working, key, person.person_id, tuple(values), on)
    # Dated routing values live in events. Rewriting baseline fields would also
    # change the resolved address/driver on dates before the user's effective day.
    temporal_people = {
        (member.person_id, key)
        for (hid, key, _index) in proposals
        for member in after.household_members(hid, on=on)
        if (member.person_id, key) not in exceptions
        and (key in ("address", "detail_address", "jurisdiction") or key.startswith("status:"))
    }
    for pid, key in temporal_people:
        if pid not in old_ids:
            continue
        if key.startswith("status:"):
            meal = _MEALS[key[7:]]
            working = _set_assignment(
                working,
                pid,
                meal,
                driver=before.resolve_assignment(pid, meal).driver,
                field="driver",
            )
        else:
            working = _set_values(working, key, pid, _values(before, key, pid, on), on)
    working = _propagate_enrollments(before, after, working, active, on, exceptions)
    extra = [c for c in record.changes if c.entity_kind not in ROSTER_ENTITY_KINDS]
    routing = after_context.routing
    position = max((e.position for e in routing.events), default=0)
    event_claims = {}
    routing_claims = {}
    # Profile and assignment editors also need a dated routing override when
    # older movement records would otherwise hide the explicitly entered value.
    projected = {}
    for (hid, key, index), value in proposals.items():
        if key not in ("address", "detail_address", "jurisdiction") and not key.startswith("status:"):
            continue
        for member in after.household_members(hid, on=on):
            if (member.person_id, key) in exceptions:
                continue
            target = (member.person_id, key)
            if target not in projected:
                projected[target] = list(
                    _values(after, key, member.person_id, on, context=after_context)
                )
            projected[target][2 if key.startswith("status:") else index] = value
    address_projections = {}
    for (pid, key), values in projected.items():
        if key in ("address", "detail_address"):
            pair = address_projections.setdefault(pid, _address_pair(after, pid, on, routing, extra))
            pair["road" if key == "address" else "detail"] = values[0]
            continue
        position += 1
        if key == "jurisdiction":
            extra.append(_routing_change(pid, on, position, jurisdiction=values[0]))
        else:
            meal = _MEALS[key[7:]]
            driver_id = _shared_driver_id(
                routing, values[2], extra, record.metadata.revision_id
            )
            extra.append(
                _routing_change(pid, on, position, driver_ids={meal: driver_id})
            )
    for pid, pair in address_projections.items():
        position += 1
        extra.append(_routing_change(pid, on, position, **pair))
    for change in record.changes:
        if change.entity_kind == STATUS_EVENT_KIND and change.after is not None:
            event = status_event_from_state(change.entity_id, change.after)
            key = "status:" + next(k for k, v in _MEALS.items() if v == event.meal)
            if key not in active or (event.person_id, key) in exceptions:
                continue
            day = event.period.starts_on or on
            household = after.household_for(event.person_id, on=day)
            if household is None:
                continue
            for member in after.household_members(household.household_id, on=day):
                if (member.person_id, key) in exceptions:
                    continue
                period = (
                    event.period
                    if member.person_id == event.person_id
                    else _shared_period(
                        after,
                        event.person_id,
                        member.person_id,
                        household.household_id,
                        day,
                        event.period,
                    )
                )
                if period is None:
                    continue
                target = (member.person_id, event.meal)
                for previous_period, previous_status in event_claims.get(target, ()):
                    if (
                        previous_period.overlaps(period)
                        and previous_status != event.status
                    ):
                        raise HouseholdSharedConflict(
                            key, day,
                            "같은 가구의 상태 변경이 서로 충돌합니다. 상태와 기간을 일치시켜주세요."
                        )
                event_claims.setdefault(target, []).append((period, event.status))
                if (
                    member.person_id != event.person_id
                    and event.status is MealStatus.DELIVERY
                ):
                    working = replace(
                        working,
                        enrollments=_cover_delivery_period(
                            working.enrollments, member.person_id, event.meal, period
                        ),
                    )
                elif (
                    member.person_id != event.person_id
                    and event.meal not in working.meals_for(member.person_id, on=day)
                ):
                    raise HouseholdSharedConflict(
                        key, day,
                        "공용 상태를 적용할 가구원이 해당 식종에 미가입입니다. 가구 공용 설정에서 상태를 함께 지정해주세요."
                    )
                if member.person_id != event.person_id and not any(
                    c.entity_kind == STATUS_EVENT_KIND
                    and c.after
                    and c.after["person_id"] == str(member.person_id)
                    and c.after["meal"] == event.meal.value
                    and c.after["starts_on"] == change.after["starts_on"]
                    and c.after["ends_on"] == change.after["ends_on"]
                    for c in extra
                ):
                    extra.append(
                        status_event_audit_change(
                            replace(
                                event,
                                event_id=uuid4(),
                                person_id=member.person_id,
                                period=period,
                            )
                        )
                    )
        elif change.entity_kind == ROUTING_EVENT_KIND and change.after is not None:
            if str(change.entity_id) in corrected_events:
                continue
            event = next(e for e in routing.events if e.event_id == change.entity_id)
            household = after.household_for(event.person_id, on=event.effective_on)
            if household is None:
                continue
            fields = {}
            edited_components = record.metadata.decisions.get("household_shared_profile_components")
            for key, field, value, component in (
                ("address", "road", event.road_address, "road_address"),
                ("detail_address", "detail", event.detail_address, "detail_address"),
            ):
                if key in active and (event.person_id, key) not in exceptions and event.road_address is not None and (edited_components is None or component in edited_components):
                    fields[field] = value or ""
            if "jurisdiction" in active and (event.person_id, "jurisdiction") not in exceptions and event.jurisdiction is not None:
                fields["jurisdiction"] = event.jurisdiction
            drivers = {
                meal: driver
                for meal, driver in event.driver_ids.items()
                if "status:" + next(k for k, v in _MEALS.items() if v == meal) in active
                and (event.person_id, "status:" + next(k for k, v in _MEALS.items() if v == meal)) not in exceptions
            }
            if drivers:
                fields["driver_ids"] = drivers
            if fields:
                for field, value in fields.items():
                    claims = (
                        value.items() if field == "driver_ids" else ((field, value),)
                    )
                    for component, proposed in claims:
                        target = (
                            household.household_id,
                            event.effective_on,
                            field,
                            component,
                        )
                        if (
                            target in routing_claims
                            and routing_claims[target] != proposed
                        ):
                            conflict_key = (
                                "status:" + next(k for k, v in _MEALS.items() if v == component)
                                if field == "driver_ids" else
                                {"road": "address", "detail": "detail_address", "jurisdiction": "jurisdiction"}[field]
                            )
                            raise HouseholdSharedConflict(
                                conflict_key, event.effective_on,
                                "같은 가구의 주소·관할동·배송담당 변경이 서로 충돌합니다."
                            )
                        routing_claims[target] = proposed
                for member in after.household_members(
                    household.household_id, on=event.effective_on
                ):
                    if member.person_id != event.person_id:
                        position += 1
                        member_fields = dict(fields)
                        if (member.person_id, "address") in exceptions:
                            member_fields.pop("road", None)
                        if (member.person_id, "detail_address") in exceptions:
                            member_fields.pop("detail", None)
                        if (member.person_id, "jurisdiction") in exceptions:
                            member_fields.pop("jurisdiction", None)
                        if "driver_ids" in member_fields:
                            member_fields["driver_ids"] = {
                                meal: driver for meal, driver in member_fields["driver_ids"].items()
                                if (member.person_id, "status:" + next(k for k, v in _MEALS.items() if v == meal)) not in exceptions
                            }
                            if not member_fields["driver_ids"]:
                                member_fields.pop("driver_ids")
                        if not member_fields:
                            continue
                        if "road" in member_fields or "detail" in member_fields:
                            pair = _address_pair(after, member.person_id, event.effective_on, routing, extra)
                            pair.update({key: member_fields[key] for key in ("road", "detail") if key in member_fields})
                            member_fields.update(pair)
                        extra.append(
                            _routing_change(
                                member.person_id,
                                event.effective_on,
                                position,
                                **member_fields,
                            )
                        )
    # Removing a custom definition also removes its obsolete opt-in key.
    valid_exceptions = frozenset((pid, key) for pid, key in exceptions if pid in {p.person_id for p in after.persons} and key in available_shared_column_keys(after))
    if available != selected or valid_exceptions != exceptions:
        state = snapshot.entity(HOUSEHOLD_SHARED_KIND, HOUSEHOLD_SHARED_ID)
        extra.append(
            AuditChange(
                HOUSEHOLD_SHARED_KIND,
                HOUSEHOLD_SHARED_ID,
                ChangeOperation.UPDATE,
                state,
                _shared_state(available, valid_exceptions),
            )
        )
    return replace(record, changes=(*roster_audit_changes(before, working), *extra))


def _profile_routing_columns(snapshot, person_id):
    if snapshot is None:
        return frozenset()
    selected = set(
        shared_columns_from_state(
            snapshot.entity(HOUSEHOLD_SHARED_KIND, HOUSEHOLD_SHARED_ID)
        )
    )
    for event in routing_state_from_snapshot(snapshot).events:
        if event.person_id != person_id:
            continue
        if event.road_address is not None:
            selected.add("address")
        if event.jurisdiction is not None:
            selected.add("jurisdiction")
    if selected.intersection({"address", "detail_address"}):
        selected.update({"address", "detail_address"})
    return selected.intersection({"address", "detail_address", "jurisdiction"})


def profile_with_shared_routing(roster, person_id, on, snapshot):
    """Seed dated profile values, including values retained after sharing stops."""
    profile = roster.person(person_id).profile
    if snapshot is None:
        return profile
    selected = _profile_routing_columns(snapshot, person_id)
    if not selected:
        return profile
    view = routing_state_from_snapshot(snapshot).resolve(roster, person_id, on)
    fields = {}
    if "address" in selected:
        fields.update(
            road_address=view.road_address, detail_address=view.detail_address
        )
    if "jurisdiction" in selected:
        fields["jurisdiction"] = view.jurisdiction
    return replace(profile, **fields)


def build_profile_correction_revision(
    roster,
    person_id,
    updated_profile,
    *,
    snapshot=None,
    forced_routing_fields=(),
    **kwargs,
):
    """Correct entered routing components through today, preserving reservations.

    Sharing uses today's membership and per-column exceptions. Existing events
    are updated in this revision, leaving previous revision snapshots intact.
    """
    from .profile_edits import build_person_profile_revision, PreparedPersonProfileRevision

    on = kwargs["effective_on"]
    baseline = roster.person(person_id).profile
    displayed = profile_with_shared_routing(roster, person_id, on, snapshot)
    columns = {"road_address": "address", "detail_address": "detail_address", "jurisdiction": "jurisdiction"}
    forced = frozenset(forced_routing_fields)
    if not forced.issubset(columns):
        raise ValueError("정정할 주소 항목이 올바르지 않습니다.")
    changed = {
        field: getattr(updated_profile, field)
        for field in columns
        if field in forced
        or (getattr(updated_profile, field) or "") != (getattr(displayed, field) or "")
    }
    # The dialog displays resolved routing values. Unedited components must not
    # leak back into the baseline when another profile field is saved.
    profile = replace(updated_profile, **{
        field: changed.get(field, getattr(baseline, field)) for field in columns
    })
    try:
        prepared = build_person_profile_revision(roster, person_id, profile, **kwargs)
    except ValueError as error:
        if str(error) != "변경된 사람 정보가 없습니다." or not changed:
            raise
        prepared = None
    if not changed:
        return prepared

    state = snapshot.entity(HOUSEHOLD_SHARED_KIND, HOUSEHOLD_SHARED_ID) if snapshot else None
    shared = shared_columns_from_state(state)
    exceptions = shared_exceptions_from_state(state)
    household = roster.household_for(person_id, on=on)
    corrections = {person_id: dict(changed)}
    for field, value in changed.items():
        column = columns[field]
        if column not in shared or (person_id, column) in exceptions or household is None:
            continue
        for member in roster.household_members(household.household_id, on=on):
            if (member.person_id, column) not in exceptions:
                corrections.setdefault(member.person_id, {})[field] = value

    working = prepared.snapshot if prepared else replace(roster, revision_id=kwargs["revision_id"])
    working = replace(working, persons=tuple(
        replace(person, profile=replace(person.profile, **corrections[person.person_id]))
        if person.person_id in corrections else person
        for person in working.persons
    ))
    event_changes = []
    for event in routing_state_from_snapshot(snapshot).events if snapshot else ():
        if event.person_id not in corrections or event.effective_on > on:
            continue
        values = {
            field: value or ""
            for field, value in corrections[event.person_id].items()
            if (field == "jurisdiction" and event.jurisdiction is not None)
            or (field != "jurisdiction" and event.road_address is not None)
        }
        updated = replace(event, **values)
        if updated != event:
            event_changes.append(AuditChange(
                ROUTING_EVENT_KIND, event.event_id, ChangeOperation.UPDATE,
                _routing_event_state(event), _routing_event_state(updated),
            ))
    metadata = prepared.record.metadata if prepared else RevisionMetadata(
        kwargs["revision_id"], roster.revision_id, kwargs["recorded_at"], on,
        kwargs["actor"], kwargs.get("reason", "사람 정보 수정"), RevisionSource.UI,
    )
    metadata = replace(metadata, decisions={
        **metadata.decisions,
        "profile_routing_correction": {
            "components": [
                {"person_id": str(pid), "column": columns[field]}
                for pid, fields in corrections.items() for field in fields
            ],
            "event_ids": [str(change.entity_id) for change in event_changes],
        },
    })
    changes = (*roster_audit_changes(roster, working), *event_changes)
    if not changes:
        raise ValueError("변경된 사람 정보가 없습니다.")
    record = RevisionRecord(metadata, changes)
    changed_fields = tuple(dict.fromkeys((*(prepared.changed_fields if prepared else ()), *changed)))
    cleared_fields = tuple(field for field in changed_fields if getattr(updated_profile, field) is None)
    if prepared:
        return replace(prepared, snapshot=working, record=record,
                       changed_fields=changed_fields, cleared_fields=cleared_fields)
    return PreparedPersonProfileRevision(working, record, changed_fields, cleared_fields)


def build_profile_revision_with_shared_routing(
    roster, person_id, updated_profile, *, snapshot=None, **kwargs
):
    """Keep temporal edit intent even when the user restores a baseline value."""
    from .profile_edits import (
        build_person_profile_revision,
        PreparedPersonProfileRevision,
    )

    selected = _profile_routing_columns(snapshot, person_id)
    if not selected:
        return build_person_profile_revision(
            roster, person_id, updated_profile, **kwargs
        )
    baseline = roster.person(person_id).profile
    displayed = profile_with_shared_routing(
        roster, person_id, kwargs["effective_on"], snapshot
    )
    baseline_fields = {}
    routing_fields = {}
    changed_fields = []
    if "address" in selected:
        baseline_fields.update(
            road_address=baseline.road_address, detail_address=baseline.detail_address
        )
        if (updated_profile.road_address, updated_profile.detail_address) != (
            displayed.road_address,
            displayed.detail_address,
        ):
            routing_fields.update(
                road=updated_profile.road_address or "",
                detail=updated_profile.detail_address or "",
            )
            changed_fields.extend(
                field
                for field in ("road_address", "detail_address")
                if getattr(updated_profile, field) != getattr(displayed, field)
            )
    if "jurisdiction" in selected:
        baseline_fields["jurisdiction"] = baseline.jurisdiction
        if updated_profile.jurisdiction != displayed.jurisdiction:
            routing_fields["jurisdiction"] = updated_profile.jurisdiction or ""
            changed_fields.append("jurisdiction")
    try:
        prepared = build_person_profile_revision(
            roster, person_id, replace(updated_profile, **baseline_fields), **kwargs
        )
    except ValueError as error:
        if str(error) != "변경된 사람 정보가 없습니다." or not routing_fields:
            raise
        prepared = None
    if not routing_fields:
        return prepared
    routing = routing_state_from_snapshot(snapshot)
    change = _routing_change(
        person_id,
        kwargs["effective_on"],
        max((event.position for event in routing.events), default=0) + 1,
        **routing_fields,
    )
    if prepared is not None:
        return replace(
            prepared,
            record=replace(
                prepared.record,
                metadata=replace(
                    prepared.record.metadata,
                    decisions={
                        **prepared.record.metadata.decisions,
                        "household_shared_profile_components": tuple(changed_fields),
                    },
                ),
                changes=(*prepared.record.changes, change),
            ),
        )
    metadata = RevisionMetadata(
        kwargs["revision_id"],
        roster.revision_id,
        kwargs["recorded_at"],
        kwargs["effective_on"],
        kwargs["actor"],
        kwargs.get("reason", "사람 정보 수정"),
        RevisionSource.UI,
        decisions={"household_shared_profile_components": tuple(changed_fields)},
    )
    record = RevisionRecord(metadata, (change,))
    cleared = tuple(
        field for field in changed_fields if getattr(updated_profile, field) is None
    )
    return PreparedPersonProfileRevision(
        replace(roster, revision_id=kwargs["revision_id"]),
        record,
        tuple(changed_fields),
        cleared,
    )
