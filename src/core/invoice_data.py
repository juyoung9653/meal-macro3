from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

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

    @property
    def total_count(self) -> int:
        return self.breakfast_count + self.lunch_count + self.dinner_count

    def amount(self, prices: MealPrices) -> int:
        if self.billing_amount is not None:
            return self.billing_amount
        return (
            self.breakfast_count * prices.breakfast
            + self.lunch_count * prices.lunch
            + self.dinner_count * prices.dinner
        )


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
