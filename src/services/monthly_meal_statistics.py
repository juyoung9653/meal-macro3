"""Read-only monthly meal counts by actual physical delivery day."""

from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import date, timedelta
from uuid import UUID

from src.domain.revisions import HistoricalSnapshot
from src.domain.roster import Meal
from src.services.sqlite_delivery_report_service import prepare_physical_delivery_data

MEALS = (Meal.DINNER, Meal.BREAKFAST, Meal.LUNCH)


@dataclass(frozen=True, slots=True)
class MonthlyMealDay:
    day: date
    dinner: int
    breakfast: int
    lunch: int

    @property
    def total(self) -> int:
        return self.dinner + self.breakfast + self.lunch


@dataclass(frozen=True, slots=True)
class MonthlyMealStatistics:
    revision_id: UUID
    year: int
    month: int
    days: tuple[MonthlyMealDay, ...]
    dinner_total: int
    breakfast_total: int
    lunch_total: int

    @property
    def total(self) -> int:
        return self.dinner_total + self.breakfast_total + self.lunch_total


def build_monthly_meal_statistics(snapshot: HistoricalSnapshot, year: int, month: int) -> MonthlyMealStatistics:
    """Count entitlements, including zero dates, from one immutable DB revision."""
    if not isinstance(snapshot, HistoricalSnapshot):
        raise TypeError("명단 변경 이력을 읽을 수 없습니다.")
    if isinstance(year, bool) or not isinstance(year, int) or isinstance(month, bool) or not isinstance(month, int):
        raise ValueError("조회 연월이 올바르지 않습니다.")
    start = date(year, month, 1)
    end = start + timedelta(days=monthrange(year, month)[1] - 1)
    physical = prepare_physical_delivery_data(
        snapshot, start, end, MEALS, missing_lunch_schedule_as_zero=True,
    )
    counts: dict[date, dict[Meal, int]] = {}
    for shipment in physical.shipments:
        # The source also includes earlier comparison shipments. Never count those.
        if not start <= shipment.delivery_on <= end:
            continue
        by_meal = counts.setdefault(shipment.delivery_on, {})
        by_meal[shipment.meal] = by_meal.get(shipment.meal, 0) + shipment.entitlement_count
    days = tuple(
        MonthlyMealDay(
            day,
            counts.get(day, {}).get(Meal.DINNER, 0),
            counts.get(day, {}).get(Meal.BREAKFAST, 0),
            counts.get(day, {}).get(Meal.LUNCH, 0),
        )
        for offset in range((end - start).days + 1)
        for day in (start + timedelta(days=offset),)
    )
    return MonthlyMealStatistics(
        snapshot.revision_id, year, month, days,
        sum(item.dinner for item in days),
        sum(item.breakfast for item in days),
        sum(item.lunch for item in days),
    )
