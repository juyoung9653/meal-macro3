"""Registered drivers and effective-date address/assignment instructions."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from types import MappingProxyType
from uuid import UUID, uuid4

from .revisions import (
    AuditChange,
    ChangeOperation,
    HistoricalSnapshot,
    RevisionActor,
    RevisionMetadata,
    RevisionRecord,
    RevisionSource,
)
from .roster import Meal, RosterSnapshot

DRIVER_KIND = "delivery.driver"
DRIVER_ORDER_KIND = "delivery.driver_order"
DRIVER_ORDER_ID = UUID("32fbf66a-4498-4a30-965d-a7b3b1b433cd")
ROUTING_EVENT_KIND = "delivery.routing_event"
MEAL_ORDER = (Meal.DINNER, Meal.BREAKFAST, Meal.LUNCH)


@dataclass(frozen=True, slots=True)
class Driver:
    driver_id: UUID
    name: str
    telephone: str = ""

    def __post_init__(self):
        if not isinstance(self.driver_id, UUID):
            raise TypeError("기사 ID가 올바르지 않습니다.")
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("기사 이름을 입력해주세요.")
        if not isinstance(self.telephone, str):
            raise TypeError("전화번호는 문자열이어야 합니다.")
        object.__setattr__(self, "name", self.name.strip())
        object.__setattr__(self, "telephone", self.telephone.strip())


@dataclass(frozen=True, slots=True)
class RoutingEvent:
    event_id: UUID
    person_id: UUID
    effective_on: date
    position: int
    driver_ids: Mapping[Meal, UUID | None]
    road_address: str | None = None
    detail_address: str | None = None
    jurisdiction: str | None = None

    def __post_init__(self):
        if not isinstance(self.event_id, UUID) or not isinstance(self.person_id, UUID):
            raise TypeError("이사·배정 기록 ID가 올바르지 않습니다.")
        if type(self.effective_on) is not date:
            raise TypeError("적용일은 날짜여야 합니다.")
        if type(self.position) is not int or self.position < 1:
            raise ValueError("이사·배정 기록 순서가 올바르지 않습니다.")
        for meal, driver_id in self.driver_ids.items():
            if not isinstance(meal, Meal) or (
                driver_id is not None and not isinstance(driver_id, UUID)
            ):
                raise TypeError("식종별 기사 지정 값이 올바르지 않습니다.")
        if self.road_address is not None:
            if not isinstance(self.road_address, str):
                raise ValueError("주소는 문자열이어야 합니다.")
            object.__setattr__(self, "road_address", self.road_address.strip())
        if self.detail_address is not None and not isinstance(self.detail_address, str):
            raise TypeError("상세주소는 문자열이어야 합니다.")
        if self.jurisdiction is not None:
            if not isinstance(self.jurisdiction, str):
                raise TypeError("관할동은 문자열이어야 합니다.")
            object.__setattr__(self, "jurisdiction", self.jurisdiction.strip())
        if self.road_address is None and self.detail_address is not None:
            raise ValueError("상세주소 변경에는 기본 주소가 필요합니다.")
        if (
            self.road_address is None
            and self.jurisdiction is None
            and not self.driver_ids
        ):
            raise ValueError(
                "변경할 주소·관할동 또는 배송담당을 선택해주세요."
            )
        object.__setattr__(self, "driver_ids", MappingProxyType(dict(self.driver_ids)))


@dataclass(frozen=True, slots=True)
class PersonDeliveryView:
    road_address: str | None
    detail_address: str | None
    drivers: Mapping[Meal, str]
    driver_ids: Mapping[Meal, UUID | None]
    jurisdiction: str | None = None


@dataclass(frozen=True, slots=True)
class DriverReplacementTarget:
    """One person's meal assignments selected for a batch driver replacement."""

    person_id: UUID
    meals: tuple[Meal, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.person_id, UUID):
            raise TypeError("사람 ID가 올바르지 않습니다.")
        meals = tuple(self.meals)
        if not meals or any(not isinstance(meal, Meal) for meal in meals):
            raise ValueError("교체할 식종을 선택해주세요.")
        if len(set(meals)) != len(meals):
            raise ValueError("교체할 식종이 중복되었습니다.")
        canonical = tuple(meal for meal in MEAL_ORDER if meal in meals)
        if meals != canonical:
            raise ValueError("교체할 식종 순서가 올바르지 않습니다.")
        object.__setattr__(self, "meals", meals)


@dataclass(frozen=True, slots=True)
class RoutingState:
    drivers: tuple[Driver, ...] = ()
    events: tuple[RoutingEvent, ...] = ()
    driver_order: tuple[UUID, ...] | None = None
    _drivers_by_id: Mapping[UUID, Driver] = field(init=False, repr=False)
    _events_by_person: Mapping[UUID, tuple[RoutingEvent, ...]] = field(
        init=False, repr=False
    )
    _unique_names: Mapping[str, UUID] = field(init=False, repr=False)

    def __post_init__(self):
        drivers = tuple(self.drivers)
        events = tuple(self.events)
        drivers_by_id = {driver.driver_id: driver for driver in drivers}
        if len(drivers_by_id) != len(drivers):
            raise ValueError("기사 ID가 중복되었습니다.")
        if self.driver_order is not None:
            order = tuple(self.driver_order)
            if any(not isinstance(item, UUID) for item in order):
                raise TypeError("기사 표시 순서의 ID가 올바르지 않습니다.")
            if len(set(order)) != len(order) or not set(order) <= drivers_by_id.keys():
                raise ValueError("기사 표시 순서에 중복되거나 등록되지 않은 기사가 있습니다.")
            object.__setattr__(self, "driver_order", order)
        if len({event.event_id for event in events}) != len(events):
            raise ValueError("이사·배정 기록 ID가 중복되었습니다.")
        if len({event.position for event in events}) != len(events):
            raise ValueError("이사·배정 기록 순서가 중복되었습니다.")
        by_person: dict[UUID, list[RoutingEvent]] = {}
        names: dict[str, list[UUID]] = {}
        for driver in drivers:
            names.setdefault(driver.name, []).append(driver.driver_id)
        for event in events:
            if any(
                driver_id is not None and driver_id not in drivers_by_id
                for driver_id in event.driver_ids.values()
            ):
                raise ValueError("등록되지 않은 배송기사가 지정되어 있습니다.")
            by_person.setdefault(event.person_id, []).append(event)
        object.__setattr__(self, "drivers", drivers)
        object.__setattr__(self, "events", events)
        object.__setattr__(self, "_drivers_by_id", MappingProxyType(drivers_by_id))
        object.__setattr__(
            self,
            "_unique_names",
            MappingProxyType(
                {name: ids[0] for name, ids in names.items() if len(ids) == 1}
            ),
        )
        object.__setattr__(
            self,
            "_events_by_person",
            MappingProxyType(
                {
                    person_id: tuple(
                        sorted(
                            items,
                            key=lambda event: (event.effective_on, event.position),
                        )
                    )
                    for person_id, items in by_person.items()
                }
            ),
        )

    def resolve(
        self, roster: RosterSnapshot, person_id: UUID, day: date
    ) -> PersonDeliveryView:
        if type(day) is not date:
            raise TypeError("조회일은 날짜여야 합니다.")
        profile = roster.person(person_id).profile
        names = {
            meal: roster.resolve_assignment(person_id, meal).driver or ""
            for meal in Meal
        }
        ids = {meal: self._unique_names.get(name) for meal, name in names.items()}
        road, detail = profile.road_address, profile.detail_address
        jurisdiction = profile.jurisdiction
        for event in self._events_by_person.get(person_id, ()):
            if event.effective_on > day:
                break
            if event.road_address is not None:
                road, detail = event.road_address, event.detail_address
            if event.jurisdiction is not None:
                jurisdiction = event.jurisdiction
            for meal, driver_id in event.driver_ids.items():
                ids[meal] = driver_id
                names[meal] = (
                    "" if driver_id is None else self._drivers_by_id[driver_id].name
                )
        return PersonDeliveryView(
            road,
            detail,
            MappingProxyType(names),
            MappingProxyType(ids),
            jurisdiction,
        )


def routing_state_from_snapshot(snapshot: HistoricalSnapshot) -> RoutingState:
    drivers, events = [], []
    driver_order = None
    for reference, state in snapshot.entities.items():
        if reference.entity_kind == DRIVER_KIND:
            drivers.append(
                Driver(reference.entity_id, state["name"], state["telephone"])
            )
        elif reference.entity_kind == DRIVER_ORDER_KIND:
            if reference.entity_id != DRIVER_ORDER_ID:
                raise ValueError("기사 표시 순서 기록 ID가 올바르지 않습니다.")
            raw_order = state["ordered_driver_ids"]
            if not isinstance(raw_order, (list, tuple)):
                raise ValueError("저장된 기사 표시 순서가 올바르지 않습니다.")
            if any(not isinstance(item, str) for item in raw_order):
                raise ValueError("저장된 기사 표시 순서의 ID가 올바르지 않습니다.")
            driver_order = tuple(UUID(item) for item in raw_order)
        elif reference.entity_kind == ROUTING_EVENT_KIND:
            events.append(
                RoutingEvent(
                    reference.entity_id,
                    UUID(state["person_id"]),
                    date.fromisoformat(state["effective_on"]),
                    state["position"],
                    {
                        Meal(meal): None if driver_id is None else UUID(driver_id)
                        for meal, driver_id in state["driver_ids"].items()
                    },
                    state["road_address"],
                    state["detail_address"],
                    state.get("jurisdiction"),
                )
            )
    drivers.sort(key=lambda driver: (driver.name, driver.telephone, str(driver.driver_id)))
    if driver_order is not None:
        positions = {driver_id: index for index, driver_id in enumerate(driver_order)}
        drivers.sort(key=lambda driver: positions.get(driver.driver_id, len(positions)))
    return RoutingState(tuple(drivers), tuple(events), driver_order)


def _driver_state(driver: Driver):
    return {"name": driver.name, "telephone": driver.telephone}


def _record(roster, change, actor, recorded_at, revision_id, effective_on, reason):
    if revision_id == roster.revision_id:
        raise ValueError("새 변경번호가 필요합니다.")
    return RevisionRecord(
        RevisionMetadata(
            revision_id=revision_id,
            parent_revision_id=roster.revision_id,
            recorded_at=recorded_at,
            effective_on=effective_on,
            actor=actor,
            reason=reason,
            source=RevisionSource.UI,
            rule_versions={"delivery_routing": 1},
        ),
        (change,),
    )


def build_driver_revision(
    roster: RosterSnapshot,
    state: RoutingState,
    *,
    driver_id: UUID,
    name: str,
    telephone: str,
    actor: RevisionActor,
    recorded_at: datetime,
    revision_id: UUID,
    ordered_driver_ids: tuple[UUID, ...] | None = None,
) -> RevisionRecord:
    driver = Driver(driver_id, name, telephone)
    if any(
        other.driver_id != driver_id
        and (other.name, other.telephone) == (driver.name, driver.telephone)
        for other in state.drivers
    ):
        raise ValueError("같은 이름과 전화번호의 기사가 이미 등록되어 있습니다.")
    original = state._drivers_by_id.get(driver_id)
    if original == driver and ordered_driver_ids is None:
        raise ValueError("변경된 기사 정보가 없습니다.")
    changes = [] if original == driver else [AuditChange(
        DRIVER_KIND,
        driver_id,
        ChangeOperation.INSERT if original is None else ChangeOperation.UPDATE,
        None if original is None else _driver_state(original),
        _driver_state(driver),
    )]
    if ordered_driver_ids is not None:
        updated_drivers = tuple(item for item in state.drivers if item.driver_id != driver_id) + (driver,)
        order_state = RoutingState(updated_drivers, state.events, state.driver_order)
        order_change = _driver_order_change(order_state, ordered_driver_ids)
        if order_change is not None:
            changes.append(order_change)
    if not changes:
        raise ValueError("변경된 기사 정보나 순서가 없습니다.")
    record = _record(roster, changes[0], actor, recorded_at, revision_id, None, "배송기사 등록·수정")
    return RevisionRecord(record.metadata, tuple(changes))


def _driver_order_change(state: RoutingState, ordered_driver_ids: tuple[UUID, ...]) -> AuditChange | None:
    order = tuple(ordered_driver_ids)
    if any(not isinstance(item, UUID) for item in order):
        raise TypeError("기사 표시 순서의 ID가 올바르지 않습니다.")
    if len(order) != len(state.drivers) or set(order) != set(state._drivers_by_id):
        raise ValueError("기사 표시 순서는 등록된 기사를 한 번씩 포함해야 합니다.")
    if state.driver_order == order:
        return None
    return AuditChange(
        DRIVER_ORDER_KIND,
        DRIVER_ORDER_ID,
        ChangeOperation.INSERT if state.driver_order is None else ChangeOperation.UPDATE,
        None if state.driver_order is None else {"ordered_driver_ids": tuple(str(item) for item in state.driver_order)},
        {"ordered_driver_ids": tuple(str(item) for item in order)},
    )


def build_driver_order_revision(
    roster: RosterSnapshot,
    state: RoutingState,
    *,
    ordered_driver_ids: tuple[UUID, ...],
    actor: RevisionActor,
    recorded_at: datetime,
    revision_id: UUID,
) -> RevisionRecord | None:
    if tuple(ordered_driver_ids) == tuple(driver.driver_id for driver in state.drivers):
        return None
    change = _driver_order_change(state, ordered_driver_ids)
    if change is None:
        return None
    return _record(roster, change, actor, recorded_at, revision_id, None, "배송기사 표시 순서 변경")


def build_routing_revision(
    roster: RosterSnapshot,
    state: RoutingState,
    *,
    person_id: UUID,
    effective_on: date,
    driver_ids: Mapping[Meal, UUID | None],
    actor: RevisionActor,
    recorded_at: datetime,
    revision_id: UUID,
    event_id: UUID,
    road_address: str | None = None,
    detail_address: str | None = None,
    jurisdiction: str | None = None,
    whole_household: bool = False,
    event_id_factory: Callable[[], UUID] = uuid4,
) -> RevisionRecord:
    roster.person(person_id)
    if road_address is not None and (not isinstance(road_address, str) or not road_address.strip()):
        raise ValueError("이사할 주소를 입력해주세요.")
    if not isinstance(whole_household, bool):
        raise TypeError("가구 전체 이사 여부는 bool이어야 합니다.")
    if not callable(event_id_factory):
        raise TypeError("이사·배정 기록 ID 생성기가 올바르지 않습니다.")

    target_ids = (person_id,)
    if whole_household:
        membership = next(
            (
                item
                for item in roster.memberships
                if item.person_id == person_id and item.period.contains(effective_on)
            ),
            None,
        )
        if membership is None:
            raise ValueError("적용일 기준 가구 소속이 없어 가구 전체 이사를 할 수 없습니다.")
        target_ids = (person_id,) + tuple(
            person.person_id
            for person in roster.household_members(membership.household_id, on=effective_on)
            if person.person_id != person_id
        )

    existing_ids = {item.event_id for item in state.events}
    event_ids = [event_id]
    for _ in target_ids[1:]:
        event_ids.append(event_id_factory())
    if any(not isinstance(value, UUID) for value in event_ids):
        raise TypeError("이사·배정 기록 ID가 올바르지 않습니다.")
    if len(set(event_ids)) != len(event_ids) or any(
        value in existing_ids for value in event_ids
    ):
        raise ValueError("이미 사용한 이사·배정 기록 ID입니다.")

    next_position = max((item.position for item in state.events), default=0)
    events = tuple(
        RoutingEvent(
            target_event_id,
            target_person_id,
            effective_on,
            next_position + offset,
            driver_ids,
            road_address,
            detail_address,
            jurisdiction,
        )
        for offset, (target_person_id, target_event_id) in enumerate(
            zip(target_ids, event_ids), start=1
        )
    )
    RoutingState(state.drivers, (*state.events, *events))
    changes = tuple(
        AuditChange(
            ROUTING_EVENT_KIND,
            event.event_id,
            ChangeOperation.INSERT,
            None,
            _routing_event_state(event),
        )
        for event in events
    )
    return RevisionRecord(
        RevisionMetadata(
            revision_id=revision_id,
            parent_revision_id=roster.revision_id,
            recorded_at=recorded_at,
            effective_on=effective_on,
            actor=actor,
            reason=(
                "가구 전체 이사 및 식종별 배송담당 변경"
                if whole_household
                and (road_address is not None or jurisdiction is not None)
                else "가구 전체 이사"
                if whole_household
                else "이사 및 식종별 배송담당 변경"
                if road_address is not None or jurisdiction is not None
                else "식종 배송담당 변경"
            ),
            source=RevisionSource.UI,
            rule_versions={"delivery_routing": 1},
        ),
        changes,
    )


def build_batch_assignment_revision(
    roster: RosterSnapshot,
    state: RoutingState,
    *,
    targets: tuple[tuple[UUID, Meal], ...],
    driver_id: UUID | None,
    effective_on: date,
    actor: RevisionActor,
    recorded_at: datetime,
    revision_id: UUID,
    event_id_factory: Callable[[], UUID] = uuid4,
) -> RevisionRecord | None:
    """Change only the selected person/meal pairs in one revision."""
    if type(effective_on) is not date:
        raise TypeError("적용일은 날짜여야 합니다.")
    if driver_id is not None and (
        not isinstance(driver_id, UUID) or driver_id not in state._drivers_by_id
    ):
        raise ValueError("등록되지 않은 배송기사입니다.")
    if not targets:
        raise ValueError("배송담당을 변경할 식종 셀을 선택해주세요.")
    assignments: dict[UUID, dict[Meal, UUID | None]] = {}
    for person_id, meal in targets:
        roster.person(person_id)
        if not isinstance(meal, Meal):
            raise TypeError("변경할 식종이 올바르지 않습니다.")
        view = state.resolve(roster, person_id, effective_on)
        if view.driver_ids[meal] == driver_id and (
            driver_id is not None or not view.drivers[meal]
        ):
            continue
        assignments.setdefault(person_id, {})[meal] = driver_id
    if not assignments:
        return None
    position = max((event.position for event in state.events), default=0)
    events = tuple(
        RoutingEvent(event_id_factory(), person_id, effective_on, position + offset, meals)
        for offset, (person_id, meals) in enumerate(assignments.items(), start=1)
    )
    event_ids = {event.event_id for event in events}
    if len(event_ids) != len(events) or event_ids.intersection(event.event_id for event in state.events):
        raise ValueError("이미 사용한 이사·배정 기록 ID입니다.")
    RoutingState(state.drivers, (*state.events, *events))
    return RevisionRecord(
        RevisionMetadata(
            revision_id=revision_id, parent_revision_id=roster.revision_id,
            recorded_at=recorded_at, effective_on=effective_on, actor=actor,
            reason="선택한 식종 배송담당 일괄 변경", source=RevisionSource.UI,
            rule_versions={"delivery_routing": 1},
        ),
        tuple(AuditChange(ROUTING_EVENT_KIND, event.event_id, ChangeOperation.INSERT,
                          None, _routing_event_state(event)) for event in events),
    )


def _replacement_inputs(
    state: RoutingState,
    *,
    source_driver: UUID | str,
    target_driver_id: UUID,
    effective_on: date,
    meals: tuple[Meal, ...],
) -> tuple[tuple[Meal, ...], UUID | None, str | None]:
    """Validate replacement inputs and normalize the selected meal order."""

    if type(effective_on) is not date:
        raise TypeError("적용일은 날짜여야 합니다.")
    if not isinstance(target_driver_id, UUID):
        raise TypeError("대상 기사 ID가 올바르지 않습니다.")
    if target_driver_id not in state._drivers_by_id:
        raise ValueError("등록되지 않은 배송기사입니다.")
    if not isinstance(meals, tuple):
        raise TypeError("식종은 tuple이어야 합니다.")
    if not meals or any(not isinstance(meal, Meal) for meal in meals):
        raise ValueError("교체할 식종을 선택해주세요.")
    if len(set(meals)) != len(meals):
        raise ValueError("교체할 식종이 중복되었습니다.")
    selected_meals = tuple(meal for meal in MEAL_ORDER if meal in meals)

    if isinstance(source_driver, UUID):
        if source_driver not in state._drivers_by_id:
            raise ValueError("등록되지 않은 배송기사입니다.")
        if source_driver == target_driver_id:
            raise ValueError("기존 기사와 대상 기사는 달라야 합니다.")
        return selected_meals, source_driver, None
    if isinstance(source_driver, str):
        if not source_driver.strip():
            raise ValueError("기존 기사를 선택해주세요.")
        return selected_meals, None, source_driver
    raise TypeError("기존 기사는 UUID 또는 이름 문자열이어야 합니다.")


def preview_driver_replacement(
    roster: RosterSnapshot,
    state: RoutingState,
    *,
    source_driver: UUID | str,
    target_driver_id: UUID,
    effective_on: date,
    meals: tuple[Meal, ...],
) -> tuple[DriverReplacementTarget, ...]:
    """Return all effective-date assignments that a batch replacement will change.

    A string source deliberately only addresses unresolved legacy assignments.  It
    never maps a registered driver's display name back to an ID, because duplicate
    registry names make that operation unsafe.
    """

    selected_meals, source_id, source_name = _replacement_inputs(
        state,
        source_driver=source_driver,
        target_driver_id=target_driver_id,
        effective_on=effective_on,
        meals=meals,
    )
    targets: list[DriverReplacementTarget] = []
    for person in roster.persons:
        resolved = state.resolve(roster, person.person_id, effective_on)
        matching = tuple(
            meal
            for meal in selected_meals
            if (
                resolved.driver_ids[meal] == source_id
                if source_id is not None
                else (
                    resolved.driver_ids[meal] is None
                    and resolved.drivers[meal] == source_name
                )
            )
            and resolved.driver_ids[meal] != target_driver_id
        )
        if matching:
            targets.append(DriverReplacementTarget(person.person_id, matching))
    return tuple(targets)


def _routing_event_state(event: RoutingEvent) -> dict[str, object]:
    return {
        "person_id": str(event.person_id),
        "effective_on": event.effective_on.isoformat(),
        "position": event.position,
        "driver_ids": {
            meal.value: None if driver_id is None else str(driver_id)
            for meal, driver_id in event.driver_ids.items()
        },
        "road_address": event.road_address,
        "detail_address": event.detail_address,
        "jurisdiction": event.jurisdiction,
    }


def build_driver_replacement_revision(
    roster: RosterSnapshot,
    state: RoutingState,
    *,
    source_driver: UUID | str,
    target_driver_id: UUID,
    effective_on: date,
    meals: tuple[Meal, ...],
    actor: RevisionActor,
    recorded_at: datetime,
    revision_id: UUID,
    event_id_factory: Callable[[], UUID] = uuid4,
) -> RevisionRecord:
    """Build one atomic revision that replaces a driver's matching assignments."""

    targets = preview_driver_replacement(
        roster,
        state,
        source_driver=source_driver,
        target_driver_id=target_driver_id,
        effective_on=effective_on,
        meals=meals,
    )
    if not targets:
        raise ValueError("교체할 배송 배정이 없습니다.")
    if revision_id == roster.revision_id:
        raise ValueError("새 변경번호가 필요합니다.")
    if not callable(event_id_factory):
        raise TypeError("이사·배정 기록 ID 생성기가 올바르지 않습니다.")

    existing_ids = {event.event_id for event in state.events}
    next_position = max((event.position for event in state.events), default=0)
    events: list[RoutingEvent] = []
    for offset, target in enumerate(targets, start=1):
        event_id = event_id_factory()
        if not isinstance(event_id, UUID):
            raise TypeError("이사·배정 기록 ID가 올바르지 않습니다.")
        if event_id in existing_ids:
            raise ValueError("이미 사용한 이사·배정 기록 ID입니다.")
        existing_ids.add(event_id)
        events.append(
            RoutingEvent(
                event_id,
                target.person_id,
                effective_on,
                next_position + offset,
                {meal: target_driver_id for meal in target.meals},
            )
        )
    RoutingState(state.drivers, (*state.events, *events))
    changes = tuple(
        AuditChange(
            ROUTING_EVENT_KIND,
            event.event_id,
            ChangeOperation.INSERT,
            None,
            _routing_event_state(event),
        )
        for event in events
    )
    source_trace = str(source_driver) if isinstance(source_driver, UUID) else source_driver
    return RevisionRecord(
        RevisionMetadata(
            revision_id=revision_id,
            parent_revision_id=roster.revision_id,
            recorded_at=recorded_at,
            effective_on=effective_on,
            actor=actor,
            reason="배송기사 일괄 교체",
            source=RevisionSource.UI,
            decisions={
                "source_driver": source_trace,
                "target_driver_id": str(target_driver_id),
                "meals": tuple(meal.value for meal in meals),
            },
            rule_versions={"delivery_routing": 1},
        ),
        changes,
    )
