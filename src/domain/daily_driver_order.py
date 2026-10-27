"""Per-meal driver display order for daily meal-count documents."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from uuid import UUID, uuid5

from .delivery_routing import MEAL_ORDER, RoutingState
from .revisions import (
    AuditChange, ChangeOperation, HistoricalSnapshot, RevisionActor,
    RevisionMetadata, RevisionRecord, RevisionSource,
)
from .roster import Meal, RosterSnapshot

DAILY_DRIVER_ORDER_KIND = "delivery.daily_driver_order"
_NAMESPACE = UUID("c33744d8-d146-48d1-9d87-c08ea37f64e7")


def daily_driver_order_id(meal: Meal) -> UUID:
    return uuid5(_NAMESPACE, meal.value)


def saved_daily_driver_orders(snapshot: HistoricalSnapshot, routing: RoutingState):
    orders = {}
    registered = {driver.driver_id for driver in routing.drivers}
    for reference, state in snapshot.entities.items():
        if reference.entity_kind != DAILY_DRIVER_ORDER_KIND:
            continue
        meal = Meal(state["meal"])
        if reference.entity_id != daily_driver_order_id(meal):
            raise ValueError("일일식수 기사 순서 기록 ID가 올바르지 않습니다.")
        raw = state["ordered_driver_ids"]
        if not isinstance(raw, (list, tuple)) or any(not isinstance(item, str) for item in raw):
            raise ValueError("저장된 일일식수 기사 순서가 올바르지 않습니다.")
        order = tuple(UUID(item) for item in raw)
        if len(set(order)) != len(order) or not set(order) <= registered:
            raise ValueError("일일식수 기사 순서에 중복되거나 등록되지 않은 기사가 있습니다.")
        orders[meal] = order
    return orders


def daily_driver_orders(snapshot: HistoricalSnapshot, routing: RoutingState):
    """Use registry order for unset meals; append newly registered drivers."""
    saved = saved_daily_driver_orders(snapshot, routing)
    default = tuple(driver.driver_id for driver in routing.drivers)
    return {
        meal: saved.get(meal, ()) + tuple(item for item in default if item not in saved.get(meal, ()))
        for meal in MEAL_ORDER
    }


def build_daily_driver_order_revision(
    roster: RosterSnapshot,
    snapshot: HistoricalSnapshot,
    routing: RoutingState,
    *,
    orders: Mapping[Meal, Sequence[UUID]],
    actor: RevisionActor,
    recorded_at: datetime,
    revision_id: UUID,
) -> RevisionRecord | None:
    if snapshot.revision_id != roster.revision_id or revision_id == roster.revision_id:
        raise ValueError("기사 순서를 저장할 원본 변경번호가 올바르지 않습니다.")
    if any(not isinstance(meal, Meal) for meal in orders):
        raise ValueError("기사 순서의 식종이 올바르지 않습니다.")
    saved = saved_daily_driver_orders(snapshot, routing)
    current = daily_driver_orders(snapshot, routing)
    registered = {driver.driver_id for driver in routing.drivers}
    changes = []
    for meal in MEAL_ORDER:
        if meal not in orders:
            continue
        order = tuple(orders[meal])
        if any(not isinstance(item, UUID) for item in order):
            raise ValueError("기사 순서의 ID가 올바르지 않습니다.")
        if len(order) != len(registered) or set(order) != registered:
            raise ValueError("기사 순서는 등록된 기사를 한 번씩 포함해야 합니다.")
        if order == current[meal]:
            continue
        def payload(ids):
            return {"meal": meal.value, "ordered_driver_ids": tuple(str(item) for item in ids)}
        changes.append(AuditChange(
            DAILY_DRIVER_ORDER_KIND, daily_driver_order_id(meal),
            ChangeOperation.UPDATE if meal in saved else ChangeOperation.INSERT,
            payload(saved[meal]) if meal in saved else None, payload(order),
        ))
    if not changes:
        return None
    return RevisionRecord(RevisionMetadata(
        revision_id=revision_id, parent_revision_id=roster.revision_id,
        recorded_at=recorded_at, effective_on=None, actor=actor,
        reason="일일식수 식종별 기사 순서 변경", source=RevisionSource.UI,
        rule_versions={"daily_driver_order": 1},
    ), tuple(changes))
