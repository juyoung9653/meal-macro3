"""순수 요일/공휴일 카운터 — 배송 스케줄, 공휴일, 주중 계산.

Public API:
  - count_weekdays(year, month, dow_set, ...) -> int
  - count_weekdays_in_range(start, end, dow_set, ...) -> int
  - to_bool(val) -> bool
  - _get_schedule() -> dict[MealType, set[int]]
  - _is_holiday(d) -> bool

비고 파싱, 방학 계산과 무관.
"""

import calendar
import logging
import re
from datetime import date, timedelta

from ..core.date_utils import SAT, SUN
from ..core.meal_types import MealType
from ..core.settings import get_settings
from .holiday_fetcher import get_kr_holidays

log = logging.getLogger(__name__)

# ── 주말 상수 (조식: 토·일은 공휴일이어도 금요일 배송분으로 간주) ──
_WEEKEND = {SAT, SUN}


def _get_schedule():
    """설정에서 배송 스케줄 로드."""
    s = get_settings()
    return {
        MealType.BREAKFAST: s.get_schedule_days("breakfast"),
        MealType.DINNER: s.get_schedule_days("dinner"),
        MealType.LUNCH: s.get_schedule_days("lunch"),
    }


def _is_holiday(d: date) -> bool:
    return d in get_kr_holidays(d.year)


def count_weekdays(
    year: int,
    month: int,
    dow_set: set[int],
    excluded_dates: set[date] | None = None,
    holiday_exempt_days: set[int] | None = None,
) -> int:
    """해당 월에서 특정 요일(dow_set)에 속하고 공휴일 아닌 날짜 수.
    excluded_dates 제외. holiday_exempt_days 에 속한 요일은 공휴일 체크 생략."""
    total = 0
    last_day = calendar.monthrange(year, month)[1]
    for day in range(1, last_day + 1):
        d = date(year, month, day)
        if d.weekday() not in dow_set:
            continue
        if not (
            holiday_exempt_days and d.weekday() in holiday_exempt_days
        ) and _is_holiday(d):
            continue
        if excluded_dates and d in excluded_dates:
            continue
        total += 1
    return total


def count_weekdays_in_range(
    start: date,
    end: date,
    dow_set: set[int],
    excluded_dates: set[date] | None = None,
    holiday_exempt_days: set[int] | None = None,
) -> int:
    """기간 내 특정 요일 중 공휴일 아닌 날짜 수.
    excluded_dates 제외. holiday_exempt_days 에 속한 요일은 공휴일 체크 생략."""
    total = 0
    d = start
    while d <= end:
        if d.weekday() not in dow_set:
            d += timedelta(days=1)
            continue
        if not (
            holiday_exempt_days and d.weekday() in holiday_exempt_days
        ) and _is_holiday(d):
            d += timedelta(days=1)
            continue
        if not (excluded_dates and d in excluded_dates):
            total += 1
        d += timedelta(days=1)
    return total


def to_bool(val) -> bool:
    """식종 대상 셀은 숫자 1 또는 그와 같은 십진 문자열일 때만 참이다."""

    if isinstance(val, bool):
        return False
    if isinstance(val, (int, float)):
        return val == 1
    if isinstance(val, str):
        return re.fullmatch(r"1(?:\.0+)?", val.strip()) is not None
    return False
