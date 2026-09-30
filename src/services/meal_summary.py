"""월별 일자·식종 식수 집계.

확인서와 같은 배송 요일, 공휴일/제외일, 비고 차감 규칙을 사용한다.
중식은 청구서 준비 단계에서 확정한 개인별 배송일을 우선한다.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date

from ..core.meal_types import MealType
from .delivery_calculator import _is_excluded_for_meal
from .holiday_fetcher import get_kr_holidays
from .note_parser import expand_note_dates
from .schedule_counter import _get_schedule, to_bool


@dataclass(frozen=True)
class DailyMealCount:
    delivery_date: date
    breakfast: int = 0
    dinner: int = 0
    lunch: int = 0

    @property
    def total(self) -> int:
        return self.breakfast + self.dinner + self.lunch


def _normalized_dates(person: dict) -> set[date] | None:
    """개인별 확정 중식 배송일을 반환한다. 값 자체가 없으면 ``None``."""

    raw_dates = person.get("_daily_lunch_dates")
    if raw_dates is None:
        raw_dates = person.get("_lunch_dates")
    if raw_dates is None:
        return None
    if isinstance(raw_dates, (str, date)):
        raw_dates = [raw_dates]

    result: set[date] = set()
    for value in raw_dates:
        if isinstance(value, date):
            result.add(value)
            continue
        try:
            result.add(date.fromisoformat(str(value).strip()))
        except ValueError:
            continue
    return result


def build_monthly_meal_summary(
    people: list[dict],
    year: int,
    month: int,
    excluded_dates: set[date] | None = None,
) -> list[DailyMealCount]:
    """선택 월의 모든 날짜를 식종별 식수와 함께 반환한다."""

    schedule = _get_schedule()
    holidays = get_kr_holidays(year)
    last_day = calendar.monthrange(year, month)[1]
    counts = {
        date(year, month, day): {
            MealType.BREAKFAST: 0,
            MealType.DINNER: 0,
            MealType.LUNCH: 0,
        }
        for day in range(1, last_day + 1)
    }

    for person in people:
        flags = {
            MealType.BREAKFAST: to_bool(person.get("_breakfast", 0)),
            MealType.DINNER: to_bool(person.get("_dinner", 0)),
            MealType.LUNCH: to_bool(person.get("_lunch", 0)),
        }
        lunch_dates = _normalized_dates(person)
        paused_by_date: dict[date, set[str]] = {}
        for paused_date, meals in expand_note_dates(
            str(person.get("_note", "") or ""), year, month
        ):
            paused_by_date.setdefault(paused_date, set()).update(meals)

        for target, target_counts in counts.items():
            paused = paused_by_date.get(target, set())

            for meal in (MealType.BREAKFAST, MealType.DINNER):
                if not flags[meal] or meal in paused:
                    continue
                if target.weekday() not in schedule[meal]:
                    continue
                if _is_excluded_for_meal(
                    target,
                    meal,
                    holidays,
                    excluded_dates,
                ):
                    continue
                target_counts[meal] += 1

            if MealType.LUNCH in paused:
                continue
            if lunch_dates is not None:
                if target in lunch_dates:
                    target_counts[MealType.LUNCH] += 1
            elif (
                flags[MealType.LUNCH]
                and target.weekday() in schedule[MealType.LUNCH]
                and not _is_excluded_for_meal(
                    target,
                    MealType.LUNCH,
                    holidays,
                    excluded_dates,
                )
            ):
                target_counts[MealType.LUNCH] += 1

    return [
        DailyMealCount(
            delivery_date=target,
            breakfast=meal_counts[MealType.BREAKFAST],
            dinner=meal_counts[MealType.DINNER],
            lunch=meal_counts[MealType.LUNCH],
        )
        for target, meal_counts in counts.items()
    ]
