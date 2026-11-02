"""Revision-backed creation and removal of roster persons."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID, uuid4

from .delivery_dates import validate_actual_delivery_date
from .delivery_routing import ROUTING_EVENT_KIND, RoutingEvent, RoutingState, _routing_event_state
from .projections import roster_audit_changes, status_event_audit_change
from .revisions import AuditChange, ChangeOperation, RevisionActor, RevisionMetadata, RevisionRecord, RevisionSource
from .roster import DeliveryAssignment, Household, HouseholdMembership, InclusiveDateRange, Meal, MealEnrollment, Person, RosterSnapshot
from .status import MealStatus, StatusEvent
from .intake import IntakeChannel


@dataclass(frozen=True, slots=True)
class PreparedRosterRowRevision:
    snapshot: RosterSnapshot
    record: RevisionRecord


def build_roster_row_revision(
    current_roster: RosterSnapshot,
    *,
    actor: RevisionActor,
    recorded_at: datetime,
    effective_on: date,
    revision_id: UUID,
    add_person: Person | None = None,
    enroll_meals: tuple[Meal, ...] = (),
    delivery_starts_on: date | None = None,
    delivery_starts_by_meal: Mapping[Meal, date] | None = None,
    meal_assignments: tuple[DeliveryAssignment, ...] = (),
    household_id: UUID | None = None,
    driver_ids: dict[Meal, UUID] | None = None,
    routing_state: RoutingState | None = None,
    routing_event_id: UUID | None = None,
    routing_event_ids: Mapping[date, UUID] | None = None,
    initial_statuses: dict[Meal, tuple[MealStatus, date | None]] | None = None,
    status_event_ids: dict[Meal, UUID] | None = None,
    delete_person_id: UUID | None = None,
    intake_channel: IntakeChannel | None = None,
) -> PreparedRosterRowRevision:
    """Apply exactly one person insertion or deletion to the current roster."""
    if not isinstance(current_roster, RosterSnapshot):
        raise TypeError("current roster는 RosterSnapshot이어야 합니다.")
    if not isinstance(actor, RevisionActor):
        raise TypeError("actor는 RevisionActor여야 합니다.")
    if not isinstance(recorded_at, datetime) or recorded_at.tzinfo is None or recorded_at.utcoffset() is None:
        raise ValueError("recorded_at에는 시간대가 있어야 합니다.")
    if not isinstance(effective_on, date):
        raise TypeError("effective_on은 date여야 합니다.")
    if not isinstance(revision_id, UUID) or revision_id == current_roster.revision_id:
        raise ValueError("새 revision ID가 필요합니다.")
    if (add_person is None) == (delete_person_id is None):
        raise ValueError("추가 또는 삭제 중 하나만 지정해야 합니다.")
    if intake_channel is not None and not isinstance(intake_channel, IntakeChannel):
        raise TypeError("접수 경로는 IntakeChannel이어야 합니다.")

    if add_person is not None:
        if not isinstance(add_person, Person):
            raise TypeError("add_person은 Person이어야 합니다.")
        if not enroll_meals or any(not isinstance(meal, Meal) for meal in enroll_meals):
            raise ValueError("새 대상자에게 하나 이상의 식종을 지정해야 합니다.")
        if len(set(enroll_meals)) != len(enroll_meals):
            raise ValueError("식종을 중복 지정할 수 없습니다.")
        common_start = effective_on if delivery_starts_on is None else delivery_starts_on
        if isinstance(common_start, datetime) or not isinstance(common_start, date):
            raise TypeError("배송 시작일은 날짜여야 합니다.")
        starts_by_meal = (
            {meal: common_start for meal in enroll_meals}
            if delivery_starts_by_meal is None
            else dict(delivery_starts_by_meal)
        )
        if set(starts_by_meal) != set(enroll_meals):
            raise ValueError("가입 식종마다 배송 시작일이 필요합니다.")
        for meal, starts_on in starts_by_meal.items():
            validate_actual_delivery_date(
                current_roster.delivery_defaults,
                meal,
                starts_on,
                field_name="배송 시작일",
            )
        persons = (*current_roster.persons, add_person)
        enrollments = (*current_roster.enrollments, *(MealEnrollment(add_person.person_id, meal, InclusiveDateRange(starts_on=starts_by_meal[meal])) for meal in enroll_meals))
        if household_id is not None and household_id not in {item.household_id for item in current_roster.current_households}:
            raise ValueError("선택한 가구가 현재 명단에 없습니다.")
        # An unmatched row is its own household, as it is in the Excel import.
        # Never infer a later join from mutable contact fields.
        selected_household_id = household_id or uuid4()
        households = current_roster.households if household_id is not None else (*current_roster.households, Household(selected_household_id))
        if household_id is not None and household_id not in {item.household_id for item in households}:
            households = (*households, Household(household_id))
        # Household identity is available before delivery begins; only meal
        # enrollments and delivery events are bounded by the delivery start.
        memberships = (*current_roster.memberships, HouseholdMembership(add_person.person_id, selected_household_id))
        if any(item.person_id != add_person.person_id or item.meal not in enroll_meals for item in meal_assignments):
            raise ValueError("배송 배정은 새 대상자의 가입 식종에만 지정할 수 있습니다.")
        assignments = (*current_roster.assignments, *meal_assignments)
        selected_driver_ids = dict(driver_ids or {})
        if any(meal not in enroll_meals or not isinstance(driver_id, UUID) for meal, driver_id in selected_driver_ids.items()):
            raise ValueError("등록된 배송기사는 가입 식종에만 지정할 수 있습니다.")
        if selected_driver_ids and routing_state is None:
            raise ValueError("등록된 배송기사 배정 정보가 필요합니다.")
        if routing_state is not None and any(driver_id not in {driver.driver_id for driver in routing_state.drivers} for driver_id in selected_driver_ids.values()):
            raise ValueError("등록되지 않은 배송기사가 지정되어 있습니다.")
        statuses = dict(initial_statuses or {})
        if any(meal not in enroll_meals or status is MealStatus.DELIVERY or not isinstance(status, MealStatus) for meal, (status, _) in statuses.items()):
            raise ValueError("초기 배송 상태는 가입 식종의 일시중지 또는 중지만 지정할 수 있습니다.")
        if set(statuses) != set(status_event_ids or {}):
            raise ValueError("초기 배송 상태마다 기록 ID가 필요합니다.")
        for meal, (status, ends_on) in statuses.items():
            starts_on = starts_by_meal[meal]
            if status is MealStatus.PAUSED and (type(ends_on) is not date or ends_on < starts_on):
                raise ValueError("일시중지 종료일은 배송 시작일 이후여야 합니다.")
            if status is MealStatus.STOPPED and ends_on is not None:
                raise ValueError("중지에는 종료일을 지정할 수 없습니다.")
            if ends_on is not None:
                validate_actual_delivery_date(
                    current_roster.delivery_defaults,
                    meal,
                    ends_on,
                    field_name=f"{status.value} 종료일",
                )
        operation, person_id = "add", add_person.person_id
    else:
        assert delete_person_id is not None
        if not isinstance(delete_person_id, UUID):
            raise TypeError("person ID는 UUID여야 합니다.")
        current_roster.person(delete_person_id)
        persons = tuple(person for person in current_roster.persons if person.person_id != delete_person_id)
        households = current_roster.households
        memberships = tuple(item for item in current_roster.memberships if item.person_id != delete_person_id)
        enrollments = tuple(item for item in current_roster.enrollments if item.person_id != delete_person_id)
        assignments = tuple(item for item in current_roster.assignments if item.person_id != delete_person_id)
        operation, person_id = "delete", delete_person_id

    snapshot = RosterSnapshot(
        revision_id,
        persons=persons,
        households=households,
        memberships=memberships,
        enrollments=enrollments,
        assignments=assignments,
        custom_fields=current_roster.custom_fields,
        delivery_defaults=current_roster.delivery_defaults,
    )
    changes = list(roster_audit_changes(current_roster, snapshot))
    if add_person is not None:
        if selected_driver_ids:
            assert routing_state is not None
            drivers_by_date: dict[date, dict[Meal, UUID]] = {}
            for meal, driver_id in selected_driver_ids.items():
                drivers_by_date.setdefault(starts_by_meal[meal], {})[meal] = driver_id
            ids_by_date = dict(routing_event_ids or {})
            if not ids_by_date and routing_event_id is not None and len(drivers_by_date) == 1:
                ids_by_date[next(iter(drivers_by_date))] = routing_event_id
            if set(ids_by_date) != set(drivers_by_date) or any(
                not isinstance(event_id, UUID) for event_id in ids_by_date.values()
            ) or len(set(ids_by_date.values())) != len(ids_by_date):
                raise ValueError("배송 시작일별로 중복되지 않는 배송 배정 기록 ID가 필요합니다.")
            existing_events = list(routing_state.events)
            next_position = max((item.position for item in existing_events), default=0) + 1
            for offset, starts_on in enumerate(sorted(drivers_by_date)):
                event = RoutingEvent(
                    ids_by_date[starts_on],
                    add_person.person_id,
                    starts_on,
                    next_position + offset,
                    drivers_by_date[starts_on],
                )
                existing_events.append(event)
                changes.append(AuditChange(ROUTING_EVENT_KIND, event.event_id, ChangeOperation.INSERT, None, _routing_event_state(event)))
            RoutingState(routing_state.drivers, tuple(existing_events))
        for meal, (status, ends_on) in statuses.items():
            event = StatusEvent((status_event_ids or {})[meal], revision_id, add_person.person_id, meal, status, InclusiveDateRange(starts_by_meal[meal], ends_on), recorded_at)
            changes.append(status_event_audit_change(event))
    decision = {"operation": operation, "person_id": str(person_id)}
    if add_person is not None and intake_channel is not None:
        decision["intake_channel"] = intake_channel.value
    metadata = RevisionMetadata(
        revision_id=revision_id,
        parent_revision_id=current_roster.revision_id,
        recorded_at=recorded_at,
        effective_on=effective_on,
        actor=actor,
        reason="명단 행 추가" if operation == "add" else "명단 행 삭제",
        source=RevisionSource.UI,
        decisions={"roster_row_change": decision},
        rule_versions={"roster_row_change_policy_version": 2},
    )
    return PreparedRosterRowRevision(snapshot, RevisionRecord(metadata, tuple(changes)))
