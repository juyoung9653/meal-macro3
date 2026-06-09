"""배송일 계산기 — 달력 계산 + 비고 파싱 + 주차별 분해.

Public API:
  - count_delivery_days(year, month, flags, note, excluded) -> dict
  - calc_weekly_breakdown(year, month, flags, note, excluded) -> list[dict]
  - to_bool(val) -> bool


비고 파싱은 note_parser 모듈 위임.
"""

import calendar
import logging
from datetime import date, timedelta

from ..core.date_utils import FRI, MON, SAT, SUN, THU, TUE, WED, date_range
from ..core.meal_types import MEAL_KEYS, MealType
from ..core.settings import get_settings
from .holiday_fetcher import get_kr_holidays
from .note_parser import apply_note_to_month, expand_note_dates

log = logging.getLogger(__name__)

# ── 요일 alias (하위 호환) ──


def _get_schedule():
    """설정에서 배송 스케줄 로드."""
    s = get_settings()
    return {
        MealType.BREAKFAST: set(s.get("schedule", "breakfast_days", default=[FRI])),
        MealType.DINNER: set(
            s.get("schedule", "dinner_days", default=[MON, TUE, WED, THU])
        ),
        MealType.LUNCH: set(
            s.get("schedule", "lunch_days", default=[MON, TUE, WED, THU, FRI])
        ),
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


# ── 비고 파서 (note_parser 위임) ──
# expand_note_dates, apply_note_to_month 는 note_parser 모듈에서 import


def to_bool(val) -> bool:
    if isinstance(val, (int, float)):
        return val > 0
    if isinstance(val, str):
        return val.strip() in ("1", "1.0", "Y", "y", "예", "있음")
    return bool(val)


# ── 배송일 수 계산 (note_parser 기반) ──

# 조식: 주말(토·일)은 공휴일이어도 금요일 배송분으로 간주 → 공휴일 체크 생략
_WEEKEND = {SAT, SUN}


def count_delivery_days(
    year: int,
    month: int,
    flag_breakfast: bool,
    flag_dinner: bool,
    flag_lunch: bool,
    note: str = "",
    excluded_dates: set[date] | None = None,
) -> dict:
    """한 사람의 월별 배송일 수 계산.

    Pipeline:
      1. 기본 배송일 수 계산 (스케줄 요일 - 공휴일 - 예외일)
      2. apply_note_to_month 로 비고 기반 차감 건수 계산
      3. 기본 - 차감 = 최종 배송일 수

    반환: {"breakfast": int, "dinner": int, "lunch": int}
    """
    schedule = _get_schedule()

    # ── 기본 배송일 수 ──
    base = {}
    base[MealType.BREAKFAST] = (
        count_weekdays(
            year, month, schedule[MealType.BREAKFAST], excluded_dates, _WEEKEND
        )
        if flag_breakfast
        else 0
    )
    base[MealType.DINNER] = (
        count_weekdays(year, month, schedule[MealType.DINNER], excluded_dates)
        if flag_dinner
        else 0
    )
    base[MealType.LUNCH] = (
        count_weekdays(year, month, schedule[MealType.LUNCH], excluded_dates)
        if flag_lunch
        else 0
    )

    # ── 비고 기반 차감 건수 (Pipeline Steps 1-4) ──
    reduction = apply_note_to_month(
        note, year, month, flag_breakfast, flag_dinner, flag_lunch, excluded_dates
    )

    result = {
        MealType.BREAKFAST: max(
            0, base[MealType.BREAKFAST] - reduction[MealType.BREAKFAST]
        ),
        MealType.DINNER: max(0, base[MealType.DINNER] - reduction[MealType.DINNER]),
        MealType.LUNCH: max(0, base[MealType.LUNCH] - reduction[MealType.LUNCH]),
    }

    if note and note.strip():
        log.info(
            "[Delivery] %04d-%02d base=(bf=%d dn=%d lh=%d) note_reduction=(bf=%d dn=%d lh=%d) -> final=(bf=%d dn=%d lh=%d)",
            year,
            month,
            base[MealType.BREAKFAST],
            base[MealType.DINNER],
            base[MealType.LUNCH],
            reduction[MealType.BREAKFAST],
            reduction[MealType.DINNER],
            reduction[MealType.LUNCH],
            result[MealType.BREAKFAST],
            result[MealType.DINNER],
            result[MealType.LUNCH],
        )
    return result


# ── 주간 배송 계산 ──────────────────────────────


def get_week_ranges(year: int, month: int) -> list[tuple[int, date, date]]:
    """해당 월을 일(요일)~토(요일) 주차 단위로 분할."""
    last_day = calendar.monthrange(year, month)[1]
    weeks = []
    week_start = None

    for day in range(1, last_day + 1):
        d = date(year, month, day)
        if d.weekday() == SUN:
            week_start = d
        if week_start is None:
            week_start = date(year, month, 1)

        if d.weekday() == SAT or day == last_day:
            weeks.append((len(weeks) + 1, week_start, d))
            week_start = None

    return weeks


def week_date_str(w_start: date, w_end: date) -> str:
    return f"{w_start.month:02d}.{w_start.day:02d}~{w_end.month:02d}.{w_end.day:02d}"


def _is_excluded_for_meal(
    d: date, all_excluded: set[date], flags: dict, weekend: set[int]
) -> bool:
    """날짜가 배송 제외 대상인지 판정.
    조식 주말(토·일)은 공휴일이어도 제외하지 않음."""
    if d not in all_excluded:
        return False
    if flags.get(MealType.BREAKFAST) and d.weekday() in weekend:
        return False
    return True


def calc_weekly_breakdown(
    year: int,
    month: int,
    flag_breakfast: bool,
    flag_dinner: bool,
    flag_lunch: bool,
    note: str = "",
    excluded_dates: set[date] | None = None,
) -> list[dict]:
    """주차별 배송 건수 계산.

    Pipeline:
      1. expand_note_dates 로 비고 날짜 확장 (월 전체)
      2. 주차별 기본 배송일 수 계산
      3. 주차별 비고 차감 건수 계산
      4. 기본 - 차감 = 주차별 최종 배송 건수

    반환: [{"week": int, "start": date, "end": date,
            "breakfast": int, "dinner": int, "lunch": int}, ...]
    """
    schedule = _get_schedule()
    weeks = get_week_ranges(year, month)
    holidays = get_kr_holidays(year)
    all_excluded = set(holidays) | (excluded_dates or set())

    # ── Step 1-2: 비고 날짜 확장 ──
    expanded = expand_note_dates(note, year, month)

    flags = {
        MealType.BREAKFAST: flag_breakfast,
        MealType.DINNER: flag_dinner,
        MealType.LUNCH: flag_lunch,
    }
    meal_keys = MEAL_KEYS

    result = []
    for wn, wstart, wend in weeks:
        # ── 주차별 기본 배송일 수 (raw) ──
        week_base = {MealType.BREAKFAST: 0, MealType.DINNER: 0, MealType.LUNCH: 0}
        for d in date_range(wstart, wend):
            if _is_excluded_for_meal(d, all_excluded, flags, _WEEKEND):
                continue
            for mk in meal_keys:
                if flags[mk] and d.weekday() in schedule[mk]:
                    week_base[mk] += 1

        # ── 주차별 비고 차감 건수 (raw) ──
        week_reduction = {MealType.BREAKFAST: 0, MealType.DINNER: 0, MealType.LUNCH: 0}
        for d, meals in expanded:
            if not (wstart <= d <= wend):
                continue
            if _is_excluded_for_meal(d, all_excluded, flags, _WEEKEND):
                continue
            for mk in meals:
                if flags.get(mk) and d.weekday() in schedule[mk]:
                    week_reduction[mk] += 1

        # ── 차감 후 최종 ──
        result.append(
            {
                "week": wn,
                "start": wstart,
                "end": wend,
                "breakfast": max(
                    0,
                    week_base[MealType.BREAKFAST] - week_reduction[MealType.BREAKFAST],
                )
                if flag_breakfast
                else 0,
                "dinner": max(
                    0, week_base[MealType.DINNER] - week_reduction[MealType.DINNER]
                )
                if flag_dinner
                else 0,
                "lunch": max(
                    0, week_base[MealType.LUNCH] - week_reduction[MealType.LUNCH]
                )
                if flag_lunch
                else 0,
            }
        )
    return result
