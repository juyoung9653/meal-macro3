"""Read-only monthly meal counts by actual physical delivery day."""

from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass, replace
from datetime import date, timedelta
from uuid import UUID
from enum import StrEnum

from src.domain.revisions import HistoricalSnapshot
from src.domain.roster import Meal
from src.domain.invoice_settings import invoice_settings_from_snapshot
from src.services.sqlite_delivery_report_service import prepare_physical_delivery_data

MEALS = (Meal.DINNER, Meal.BREAKFAST, Meal.LUNCH)


class MealCountBasis(StrEnum):
    DELIVERY = "delivery"
    SERVICE = "service"


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
    include_next_month: bool = True
    basis: MealCountBasis = MealCountBasis.DELIVERY
    delivery_days: tuple[MonthlyMealDay, ...] = ()
    service_days: tuple[MonthlyMealDay, ...] = ()

    def for_basis(self, basis):
        basis = MealCountBasis(basis)
        days = self.delivery_days if basis is MealCountBasis.DELIVERY else self.service_days
        if not days:
            days = self.days
        return replace(self, days=days, basis=basis,
                       dinner_total=sum(item.dinner for item in days),
                       breakfast_total=sum(item.breakfast for item in days),
                       lunch_total=sum(item.lunch for item in days))

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
        snapshot, start, end, MEALS, missing_lunch_schedule_as_zero=True, include_service_period=True,
    )
    counts = {basis: {} for basis in MealCountBasis}
    policy = invoice_settings_from_snapshot(snapshot)
    for shipment in physical.shipments:
        for service_on in shipment.service_dates:
            for basis, day in ((MealCountBasis.DELIVERY, shipment.delivery_on),
                               (MealCountBasis.SERVICE, service_on)):
                if start <= day <= end:
                    by_meal = counts[basis].setdefault(day, {})
                    by_meal[shipment.meal] = by_meal.get(shipment.meal, 0) + 1
    def make_days(basis):
        return tuple(MonthlyMealDay(
            day, counts[basis].get(day, {}).get(Meal.DINNER, 0),
            counts[basis].get(day, {}).get(Meal.BREAKFAST, 0),
            counts[basis].get(day, {}).get(Meal.LUNCH, 0),
        ) for offset in range((end - start).days + 1)
          for day in (start + timedelta(days=offset),))
    result = MonthlyMealStatistics(
        snapshot.revision_id, year, month, (), 0, 0, 0,
        policy.include_next_month, delivery_days=make_days(MealCountBasis.DELIVERY),
        service_days=make_days(MealCountBasis.SERVICE),
    )
    return result.for_basis(MealCountBasis.DELIVERY if policy.include_next_month else MealCountBasis.SERVICE)
