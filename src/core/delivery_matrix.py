"""연간 배송 매트릭스의 프레임워크 독립 도메인 모델."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from enum import Enum, StrEnum
from types import MappingProxyType
from typing import Any

ALLOWED_MEALS = ("조식", "중식", "석식")


class DeliveryCellValue(Enum):
    """Excel 셀의 원시 tri-state 값."""

    BLANK = None
    PAUSED = 0
    DELIVERED = 1


class MonthState(StrEnum):
    MISSING = "MISSING"
    UNENTERED = "UNENTERED"
    ENTERED = "ENTERED"


class DayState(StrEnum):
    MISSING = "MISSING"
    NO_DELIVERY = "NO_DELIVERY"
    ENTERED = "ENTERED"


class EffectiveDeliveryState(StrEnum):
    """전역 월/일 상태까지 고려한 개별 행의 의미."""

    DATE_MISSING = "DATE_MISSING"
    MONTH_UNENTERED = "MONTH_UNENTERED"
    NO_DELIVERY = "NO_DELIVERY"
    PAUSED = "PAUSED"
    DELIVERED = "DELIVERED"


@dataclass(frozen=True)
class RosterEntry:
    """명단 한 행. ``extra``에는 ID 외 모든 원본 컬럼을 보존한다."""

    person_id: str
    extra: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "extra", MappingProxyType(dict(self.extra)))

    def as_dict(self) -> dict[str, Any]:
        return {"ID": self.person_id, **self.extra}


@dataclass(frozen=True)
class DeliveryRow:
    """(ID, 식종)으로 식별되는 배송현황 한 행."""

    person_id: str
    meal: str
    staff: str
    sequence: Any
    values: Mapping[date, DeliveryCellValue]

    def __post_init__(self) -> None:
        copied = dict(self.values)
        invalid = [
            value
            for value in copied.values()
            if not isinstance(value, DeliveryCellValue)
        ]
        if invalid:
            raise ValueError("배송 값은 DeliveryCellValue여야 합니다.")
        object.__setattr__(self, "values", MappingProxyType(copied))

    @property
    def key(self) -> tuple[str, str]:
        return self.person_id, self.meal

    @property
    def sequence_display(self) -> str:
        """연번의 원본 표시 문자열. 문자열의 선행 0도 유지한다."""
        if self.sequence is None:
            return ""
        if isinstance(self.sequence, str):
            return self.sequence
        if isinstance(self.sequence, float) and self.sequence.is_integer():
            return str(int(self.sequence))
        return str(self.sequence)

    @property
    def sequence_sort_key(self) -> tuple[int, Decimal, str]:
        """숫자 연번을 2보다 10이 뒤에 오도록 정렬하는 안전한 키."""
        text = self.sequence_display.strip()
        if not text:
            return 2, Decimal(0), ""
        if not isinstance(self.sequence, bool):
            try:
                number = Decimal(text)
                if number.is_finite():
                    return 0, number, text
            except InvalidOperation:
                pass
        return 1, Decimal(0), text.casefold()

    def raw_value_on(self, day: date) -> DeliveryCellValue | None:
        """날짜 헤더가 없으면 None, 있으면 원시 tri-state를 반환한다."""
        return self.values.get(day)


@dataclass(frozen=True)
class AnnualDeliveryBook:
    """한 연도의 명단과 배송현황 및 조회 인덱스."""

    year: int
    roster: tuple[RosterEntry, ...]
    delivery_rows: tuple[DeliveryRow, ...]
    dates: tuple[date, ...]
    roster_by_id: Mapping[str, RosterEntry] = field(init=False, repr=False)
    delivery_rows_by_key: Mapping[tuple[str, str], DeliveryRow] = field(
        init=False, repr=False
    )
    _date_set: frozenset[date] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        roster = tuple(self.roster)
        delivery_rows = tuple(self.delivery_rows)
        raw_dates = tuple(self.dates)
        if len(set(raw_dates)) != len(raw_dates):
            raise ValueError("날짜는 중복될 수 없습니다.")
        dates = tuple(sorted(raw_dates))
        if any(day.year != self.year for day in dates):
            raise ValueError("모든 날짜는 워크북 연도와 같아야 합니다.")

        roster_by_id: dict[str, RosterEntry] = {}
        for entry in roster:
            if not entry.person_id:
                raise ValueError("명단 ID는 빈칸일 수 없습니다.")
            if entry.person_id in roster_by_id:
                raise ValueError(f"명단 ID가 중복되었습니다: {entry.person_id}")
            roster_by_id[entry.person_id] = entry

        rows_by_key: dict[tuple[str, str], DeliveryRow] = {}
        date_set = frozenset(dates)
        for row in delivery_rows:
            if row.meal not in ALLOWED_MEALS:
                raise ValueError(f"허용되지 않은 식종입니다: {row.meal}")
            if row.person_id not in roster_by_id:
                raise ValueError(f"명단에 없는 배송 ID입니다: {row.person_id}")
            if row.key in rows_by_key:
                raise ValueError(f"배송 키가 중복되었습니다: {row.key}")
            row_dates = set(row.values)
            missing_dates = date_set - row_dates
            additional_dates = row_dates - date_set
            if missing_dates or additional_dates:
                missing = _format_dates(missing_dates) or "없음"
                additional = _format_dates(additional_dates) or "없음"
                raise ValueError(
                    f"배송 행 {row.key!r}의 날짜 키가 워크북 날짜와 일치하지 "
                    f"않습니다. 누락: {missing}; 추가: {additional}"
                )
            rows_by_key[row.key] = row

        object.__setattr__(self, "roster", roster)
        object.__setattr__(self, "delivery_rows", delivery_rows)
        object.__setattr__(self, "dates", dates)
        object.__setattr__(self, "_date_set", date_set)
        object.__setattr__(self, "roster_by_id", MappingProxyType(roster_by_id))
        object.__setattr__(
            self, "delivery_rows_by_key", MappingProxyType(rows_by_key)
        )

    @property
    def available_months(self) -> tuple[int, ...]:
        return tuple(sorted({day.month for day in self.dates}))

    def month_state(self, year: int, month: int) -> MonthState:
        if not 1 <= month <= 12:
            raise ValueError("월은 1부터 12 사이여야 합니다.")
        if year != self.year:
            return MonthState.MISSING
        month_dates = tuple(
            day for day in self.dates if day.year == year and day.month == month
        )
        if not month_dates:
            return MonthState.MISSING
        if any(
            row.raw_value_on(day)
            in (DeliveryCellValue.PAUSED, DeliveryCellValue.DELIVERED)
            for row in self.delivery_rows
            for day in month_dates
        ):
            return MonthState.ENTERED
        return MonthState.UNENTERED

    def day_state(self, day: date) -> DayState:
        if day not in self._date_set:
            return DayState.MISSING
        if any(
            row.raw_value_on(day)
            in (DeliveryCellValue.PAUSED, DeliveryCellValue.DELIVERED)
            for row in self.delivery_rows
        ):
            return DayState.ENTERED
        return DayState.NO_DELIVERY

    def get_delivery_row(self, person_id: str, meal: str) -> DeliveryRow | None:
        return self.delivery_rows_by_key.get((person_id, meal))

    def raw_delivery_value(
        self, person_id: str, meal: str, day: date
    ) -> DeliveryCellValue | None:
        row = self._require_delivery_row(person_id, meal)
        return row.raw_value_on(day)

    def effective_delivery_state(
        self, person_id: str, meal: str, day: date
    ) -> EffectiveDeliveryState:
        """blank를 월/일 전역 상태에 맞춰 안전하게 해석한다.

        입력된 날짜에서만 개별 blank를 중지로 본다. 월 전체가 미입력이거나
        해당 날짜 열 전체가 blank이면 각각 별도 상태를 반환한다.
        """
        row = self._require_delivery_row(person_id, meal)
        if self.day_state(day) is DayState.MISSING:
            return EffectiveDeliveryState.DATE_MISSING
        if self.month_state(day.year, day.month) is MonthState.UNENTERED:
            return EffectiveDeliveryState.MONTH_UNENTERED
        if self.day_state(day) is DayState.NO_DELIVERY:
            return EffectiveDeliveryState.NO_DELIVERY
        if row.raw_value_on(day) is DeliveryCellValue.DELIVERED:
            return EffectiveDeliveryState.DELIVERED
        return EffectiveDeliveryState.PAUSED

    def delivered_rows_on(
        self,
        day: date,
        *,
        meal: str | None = None,
        staff: str | None = None,
    ) -> tuple[DeliveryRow, ...]:
        """특정 날짜에 값이 1인 행을 식종/담당자로 선택해 연번순 반환한다."""
        if self.day_state(day) is not DayState.ENTERED:
            return ()
        rows = (
            row
            for row in self.delivery_rows
            if row.raw_value_on(day) is DeliveryCellValue.DELIVERED
            and (meal is None or row.meal == meal)
            and (staff is None or row.staff == staff)
        )
        return tuple(
            sorted(
                rows,
                key=lambda row: (
                    row.sequence_sort_key,
                    row.person_id,
                    row.meal,
                ),
            )
        )

    def monthly_delivery_count(
        self, person_id: str, meal: str, year: int, month: int
    ) -> int:
        row = self._require_delivery_row(person_id, meal)
        if self.month_state(year, month) is MonthState.MISSING:
            return 0
        return sum(
            row.raw_value_on(day) is DeliveryCellValue.DELIVERED
            for day in self.dates
            if day.year == year and day.month == month
        )

    def previous_delivery_date(self, before: date, meal: str) -> date | None:
        """기준일 전에 해당 식종에서 실제 배송(1)이 있었던 가장 최근 날짜."""
        meal_rows = tuple(row for row in self.delivery_rows if row.meal == meal)
        for day in reversed(self.dates):
            if day >= before:
                continue
            if any(
                row.raw_value_on(day) is DeliveryCellValue.DELIVERED
                for row in meal_rows
            ):
                return day
        return None

    def _require_delivery_row(self, person_id: str, meal: str) -> DeliveryRow:
        row = self.get_delivery_row(person_id, meal)
        if row is None:
            raise KeyError(f"배송현황에 (ID='{person_id}', 식종='{meal}') 행이 없습니다.")
        return row


def _format_dates(dates: set[date] | frozenset[date]) -> str:
    return ", ".join(f"{day:%Y%m%d}" for day in sorted(dates))
