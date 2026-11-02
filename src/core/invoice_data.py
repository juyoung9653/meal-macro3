from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from collections.abc import Mapping
from types import MappingProxyType

from src.domain.roster import Meal

from .delivery_matrix import RosterEntry


@dataclass(frozen=True)
class MealPrices:
    breakfast: int
    lunch: int
    dinner: int


@dataclass(frozen=True)
class WeeklyDelivery:
    week: int
    start_date: date
    end_date: date
    breakfast_count: int
    lunch_count: int
    dinner_count: int


@dataclass(frozen=True)
class MonthlyDelivery:
    """One recipient's immutable, calculated delivery facts for a target month."""

    roster: RosterEntry
    note: str
    delivery_staff: str
    delivery_sequence: str
    breakfast_count: int
    lunch_count: int
    dinner_count: int
    weekly: tuple[WeeklyDelivery, ...]
    billing_amount: int | None = None
    household_key: str | None = None
    billing_amounts: Mapping[Meal, int] | None = None

    def __post_init__(self) -> None:
        if self.billing_amount is not None:
            if isinstance(self.billing_amount, bool) or not isinstance(
                self.billing_amount,
                int,
            ):
                raise TypeError("청구금액은 정수 또는 None이어야 합니다.")
            if self.billing_amount < 0:
                raise ValueError("청구금액은 0원 이상이어야 합니다.")
        if self.household_key is not None:
            if not isinstance(self.household_key, str):
                raise TypeError("가구 key는 문자열 또는 None이어야 합니다.")
            normalized = self.household_key.strip()
            if not normalized:
                raise ValueError("가구 key는 비어 있을 수 없습니다.")
            object.__setattr__(self, "household_key", normalized)
        if self.billing_amounts is not None:
            amounts = dict(self.billing_amounts)
            if set(amounts) != set(Meal) or any(not isinstance(meal, Meal) for meal in amounts):
                raise ValueError("식종별 청구금액에는 조식·중식·석식 금액이 모두 필요합니다.")
            if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in amounts.values()):
                raise ValueError("식종별 청구금액은 0원 이상의 정수여야 합니다.")
            if self.billing_amount is not None and sum(amounts.values()) != self.billing_amount:
                raise ValueError("식종별 청구금액의 합계가 전체 청구금액과 다릅니다.")
            object.__setattr__(self, "billing_amounts", MappingProxyType(amounts))

    @property
    def total_count(self) -> int:
        return self.breakfast_count + self.lunch_count + self.dinner_count

    def amount(self, prices: MealPrices) -> int:
        if self.billing_amount is not None:
            return self.billing_amount
        if self.billing_amounts is not None:
            return sum(self.billing_amounts.values())
        return (
            self.breakfast_count * prices.breakfast
            + self.lunch_count * prices.lunch
            + self.dinner_count * prices.dinner
        )

    def amount_for_meals(self, prices: MealPrices, meals: tuple[Meal, ...]) -> int:
        """Keep service-date prices when a workbook splits one person by meal."""
        counts = {Meal.BREAKFAST: self.breakfast_count, Meal.LUNCH: self.lunch_count,
                  Meal.DINNER: self.dinner_count}
        if self.billing_amounts is not None:
            return sum(self.billing_amounts[meal] for meal in meals)
        if all(count == 0 for meal, count in counts.items() if meal not in meals):
            return self.amount(prices)
        if self.billing_amount is not None:
            raise ValueError("식종별 청구금액이 없어 학기중·방중 금액을 나눌 수 없습니다. 명단에서 청구 내역을 다시 계산해주세요.")
        rates = {Meal.BREAKFAST: prices.breakfast, Meal.LUNCH: prices.lunch, Meal.DINNER: prices.dinner}
        return sum(counts[meal] * rates[meal] for meal in meals)


@dataclass(frozen=True)
class MonthlyDeliveryBatch:
    """Immutable monthly billing snapshot consumed by invoice and confirmation writers."""

    year: int
    month: int
    prices: MealPrices
    is_vacation_month: bool
    deliveries: tuple[MonthlyDelivery, ...]


@dataclass
class InvoiceData:
    """Invoice-tab state handed to the confirmation tab."""

    batch: MonthlyDeliveryBatch | None = None
    col_mapping: dict = field(default_factory=dict)

    @property
    def year(self) -> int:
        return self.batch.year if self.batch is not None else 0

    @property
    def month(self) -> int:
        return self.batch.month if self.batch is not None else 0
