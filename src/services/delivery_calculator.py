"""배송일 계산기 — 달력 계산 + 비고 파싱 + 주차별 분해.

Public API:
  - count_delivery_days(year, month, flags, note, excluded) -> dict
  - calc_weekly_breakdown(year, month, flags, note, excluded) -> list[dict]
  - to_bool(val) -> bool
  - count_weekdays(year, month, dow_set, ...) -> int
  - count_weekdays_in_range(start, end, dow_set, ...) -> int
  - count_lunch_vacation_days(...) -> int
  - count_lunch_vacation_days_with_note(...) -> int

비고 파싱은 note_parser 모듈 위임.
순수 요일/공휴일 계산은 schedule_counter 모듈 위임.
방학 중식 계산은 vacation_calculator 모듈 위임.
"""

import calendar
import logging
from datetime import date, timedelta

from ..core.date_utils import SAT, SUN, date_range
from ..core.meal_types import MEAL_KEYS, MealType
from .holiday_fetcher import get_kr_holidays
from .note_parser import apply_note_to_month, expand_note_dates

# ── 임포트 + 재익스포트 (하위 호환 보장) ──────────────
from .schedule_counter import (
    _WEEKEND,
    _get_schedule,
    _is_holiday,
    count_weekdays,
    count_weekdays_in_range,
    to_bool,
)
from .vacation_calculator import (
    count_lunch_vacation_days,
    count_lunch_vacation_days_with_note,
)

log = logging.getLogger(__name__)


# ── 배송일 수 계산 (note_parser 기반) ──


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
