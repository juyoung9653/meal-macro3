"""단일 명단 시트의 연간 배송 매트릭스 도메인 모델."""

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
    STOPPED = "STOPPED"
    PAUSED = "PAUSED"
    DELIVERED = "DELIVERED"


@dataclass(frozen=True)
class RosterEntry:
    """명단 시트의 연속된 식종 3행으로 표현되는 한 사람."""

    source_row: int
    extra: Mapping[str, Any] = field(
        default_factory=dict,
        compare=False,
        hash=False,
    )

    def __post_init__(self) -> None:
        if isinstance(self.source_row, bool) or not isinstance(self.source_row, int):
            raise TypeError("명단 시작 행은 정수여야 합니다.")
        if self.source_row < 2:
            raise ValueError("명단 시작 행은 헤더 다음인 2행 이상이어야 합니다.")
        object.__setattr__(self, "extra", MappingProxyType(dict(self.extra)))

    def as_dict(self) -> dict[str, Any]:
        """출력기가 안전하게 수정할 수 있는 명단 정보 복사본."""
        return dict(self.extra)

    @property
    def display_name(self) -> str:
        for header in ("성명", "이름"):
            value = self.extra.get(header)
            if value is None:
                continue
            text = str(value).strip()
            if text:
                return text
        return ""

    @property
    def location_text(self) -> str:
        return f"명단 {self.source_row}~{self.source_row + 2}행"

    @property
    def description(self) -> str:
        if self.display_name:
            return f"{self.display_name!r} ({self.location_text})"
        return self.location_text


@dataclass(frozen=True)
class DeliveryRow:
    """한 사람의 한 식종 배송행."""

    person: RosterEntry
    meal: str
    staff: str
    sequence: Any
    values: Mapping[date, DeliveryCellValue]
    source_row: int | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.person, RosterEntry):
            raise TypeError("배송 행의 person은 RosterEntry여야 합니다.")
        copied = dict(self.values)
        invalid = [
            value
            for value in copied.values()
            if not isinstance(value, DeliveryCellValue)
        ]
        if invalid:
            raise ValueError("배송 값은 DeliveryCellValue여야 합니다.")
        source_row = self.person.source_row if self.source_row is None else self.source_row
        if isinstance(source_row, bool) or not isinstance(source_row, int):
            raise TypeError("배송 원본 행은 정수여야 합니다.")
        if source_row < 2:
            raise ValueError("배송 원본 행은 헤더 다음인 2행 이상이어야 합니다.")
        object.__setattr__(self, "source_row", source_row)
        object.__setattr__(self, "values", MappingProxyType(copied))

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

    @property
    def description(self) -> str:
        return f"{self.person.description}, 식종={self.meal}, 원본 {self.source_row}행"

    def raw_value_on(self, day: date) -> DeliveryCellValue | None:
        """날짜 헤더가 없으면 None, 있으면 원시 tri-state를 반환한다."""
        return self.values.get(day)


@dataclass(frozen=True)
class AnnualDeliveryBook:
    """한 연도의 사람·식종별 배송행과 조회 인덱스."""

    year: int
    roster: tuple[RosterEntry, ...]
    delivery_rows: tuple[DeliveryRow, ...]
    dates: tuple[date, ...]
    rows_by_person: Mapping[RosterEntry, tuple[DeliveryRow, ...]] = field(
        init=False,
        repr=False,
    )
    _delivery_rows_by_key: Mapping[tuple[RosterEntry, str], DeliveryRow] = field(
        init=False,
        repr=False,
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

        roster_by_source_row: dict[int, RosterEntry] = {}
        for entry in roster:
            if entry.source_row in roster_by_source_row:
                raise ValueError(
                    f"명단 시작 행이 중복되었습니다: {entry.source_row}행"
                )
            roster_by_source_row[entry.source_row] = entry

        rows_by_key: dict[tuple[RosterEntry, str], DeliveryRow] = {}
        mutable_rows_by_person: dict[RosterEntry, list[DeliveryRow]] = {
            entry: [] for entry in roster
        }
        date_set = frozenset(dates)
        for row in delivery_rows:
            canonical_person = roster_by_source_row.get(row.person.source_row)
            if canonical_person is None:
                raise ValueError(
                    "명단에 연결되지 않은 배송 행입니다: " f"{row.description}"
                )
            if row.person is not canonical_person:
                raise ValueError(
                    "배송 행의 person은 장부 roster의 동일 객체여야 합니다: "
                    f"{row.description}"
                )
            if row.meal not in ALLOWED_MEALS:
                raise ValueError(f"허용되지 않은 식종입니다: {row.meal}")
            key = (row.person, row.meal)
            if key in rows_by_key:
                raise ValueError(
                    f"한 사람의 식종 행이 중복되었습니다: {row.description}"
                )
            row_dates = set(row.values)
            missing_dates = date_set - row_dates
            additional_dates = row_dates - date_set
            if missing_dates or additional_dates:
                missing = _format_dates(missing_dates) or "없음"
                additional = _format_dates(additional_dates) or "없음"
                raise ValueError(
                    f"배송 행 {row.description!r}의 날짜 키가 워크북 날짜와 "
                    f"일치하지 않습니다. 누락: {missing}; 추가: {additional}"
                )
            rows_by_key[key] = row
            mutable_rows_by_person[row.person].append(row)

        rows_by_person = {
            person: tuple(rows)
            for person, rows in mutable_rows_by_person.items()
        }
        object.__setattr__(self, "roster", roster)
        object.__setattr__(self, "delivery_rows", delivery_rows)
        object.__setattr__(self, "dates", dates)
        object.__setattr__(self, "_date_set", date_set)
        object.__setattr__(
            self,
            "rows_by_person",
            MappingProxyType(rows_by_person),
        )
        object.__setattr__(
            self,
            "_delivery_rows_by_key",
            MappingProxyType(rows_by_key),
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

    def get_delivery_row(
        self,
        person: RosterEntry,
        meal: str,
    ) -> DeliveryRow | None:
        return self._delivery_rows_by_key.get((person, meal))

    def raw_delivery_value(
        self,
        person: RosterEntry,
        meal: str,
        day: date,
    ) -> DeliveryCellValue | None:
        row = self._require_delivery_row(person, meal)
        return row.raw_value_on(day)

    def effective_delivery_state(
        self,
        person: RosterEntry,
        meal: str,
        day: date,
    ) -> EffectiveDeliveryState:
        """원시 값과 월/일 전역 상태를 함께 고려한 의미를 반환한다.

        입력된 날짜의 개별 blank는 중지, 명시적 0은 일시중지로 구분한다.
        월 전체가 미입력이거나 해당 날짜 열 전체가 blank이면 각각 별도
        상태를 반환한다.
        """
        row = self._require_delivery_row(person, meal)
        day_state = self.day_state(day)
        if day_state is DayState.MISSING:
            return EffectiveDeliveryState.DATE_MISSING
        if self.month_state(day.year, day.month) is MonthState.UNENTERED:
            return EffectiveDeliveryState.MONTH_UNENTERED
        if day_state is DayState.NO_DELIVERY:
            return EffectiveDeliveryState.NO_DELIVERY

        raw_value = row.raw_value_on(day)
        if raw_value is DeliveryCellValue.DELIVERED:
            return EffectiveDeliveryState.DELIVERED
        if raw_value is DeliveryCellValue.PAUSED:
            return EffectiveDeliveryState.PAUSED
        return EffectiveDeliveryState.STOPPED

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
                    row.person.source_row,
                    row.meal,
                ),
            )
        )

    def monthly_delivery_count(
        self,
        person: RosterEntry,
        meal: str,
        year: int,
        month: int,
    ) -> int:
        row = self._require_delivery_row(person, meal)
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

    def _require_delivery_row(
        self,
        person: RosterEntry,
        meal: str,
    ) -> DeliveryRow:
        row = self.get_delivery_row(person, meal)
        if row is None:
            raise KeyError(f"{person.description}에 식종 '{meal}' 행이 없습니다.")
        return row


def _format_dates(dates: set[date] | frozenset[date]) -> str:
    return ", ".join(f"{day:%Y%m%d}" for day in sorted(dates))
