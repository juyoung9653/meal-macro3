from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any, NotRequired, TypedDict

from .delivery_matrix import RosterEntry


class WeeklyMealCount(TypedDict):
    """Legacy confirmation payload for direct mapping-based generator callers."""

    week: int
    start_date: str  # YYYYMMDD
    end_date: str  # YYYYMMDD
    조식: int
    중식: int
    석식: int


class EnrichedPersonDict(TypedDict):
    """Deprecated legacy renderer payload; application code uses MonthlyDelivery."""

    _note: str
    _total: int
    _amount: str
    _bf_cnt: int
    _dn_cnt: int
    _lh_cnt: int
    weekly_meal_counts: list[WeeklyMealCount]
    배송순서: str
    배송담당: str
    연번: str

    비고: NotRequired[Any]
    핸드폰: NotRequired[Any]
    전체주소: NotRequired[str]


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


def _legacy_date(value: object) -> date:
    text = str(value)
    if len(text) != 8 or not text.isdigit():
        raise ValueError("주차 날짜는 YYYYMMDD 형식이어야 합니다.")
    return date(int(text[:4]), int(text[4:6]), int(text[6:]))


def legacy_recipient(
    source: Mapping[str, object], *, position: int = 1
) -> MonthlyDelivery:
    """Adapt a legacy direct-generator record at the public compatibility boundary."""
    def count(key: str) -> int:
        value = source.get(key, 0)
        return value if isinstance(value, int) and not isinstance(value, bool) else 0

    weekly: list[WeeklyDelivery] = []
    raw_weekly = source.get("weekly_meal_counts", [])
    if isinstance(raw_weekly, Sequence) and not isinstance(raw_weekly, (str, bytes)):
        for item in raw_weekly:
            if not isinstance(item, Mapping):
                continue
            try:
                weekly.append(
                    WeeklyDelivery(
                        week=int(item.get("week", 0)),
                        start_date=_legacy_date(item.get("start_date", "")),
                        end_date=_legacy_date(item.get("end_date", "")),
                        breakfast_count=int(item.get("조식", 0)),
                        lunch_count=int(item.get("중식", 0)),
                        dinner_count=int(item.get("석식", 0)),
                    )
                )
            except (TypeError, ValueError):
                continue
    return MonthlyDelivery(
        roster=RosterEntry(source_row=position + 1, extra=source),
        note="" if source.get("_note") is None else str(source.get("_note", "")).strip(),
        delivery_staff="" if source.get("배송담당") is None else str(source.get("배송담당", "")).strip(),
        delivery_sequence="" if source.get("배송순서") is None else str(source.get("배송순서", "")).strip(),
        breakfast_count=count("_bf_cnt"),
        lunch_count=count("_lh_cnt"),
        dinner_count=count("_dn_cnt"),
        weekly=tuple(weekly),
    )


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
