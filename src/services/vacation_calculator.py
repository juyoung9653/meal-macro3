"""방학 중식 배송일 계산기.

Public API:
  - count_lunch_vacation_days(year, month, vacation_date, opening_date, ...) -> int
  - count_lunch_vacation_days_with_note(year, month, entry, grade, excluded_dates) -> int

배송 요일/공휴일 정보는 schedule_counter, holiday_fetcher 위임.
"""

import calendar
import logging
from datetime import date, timedelta

from ..core.meal_types import MealType
from .holiday_fetcher import get_kr_holidays
from .schedule_counter import _get_schedule

log = logging.getLogger(__name__)


def _calc_effective_range(
    vacation_date: date | None,
    opening_date: date | None,
    vacation_meal: bool,
    opening_meal: bool,
) -> tuple[date | None, date | None]:
    """방학식/개학식 + 급식여부 → 유효 배송 기간 (시작일, 종료일).

    Returns (effective_start, effective_end). 둘 다 None이면 배송 없음.
    """
    if not vacation_meal and not opening_meal:
        return None, None

    eff_start: date | None = None
    eff_end: date | None = None

    if vacation_meal and vacation_date:
        eff_start = vacation_date + timedelta(days=1)
    if opening_meal and opening_date:
        eff_end = opening_date - timedelta(days=1)

    return eff_start, eff_end


def count_lunch_vacation_days(
    year: int,
    month: int,
    vacation_date: date | None,
    opening_date: date | None,
    vacation_meal: bool,
    opening_meal: bool,
    excluded_dates: set[date] | None = None,
) -> int:
    """중식 방학 기간 중 해당 월의 배송일 수 계산.

    규칙:
      - 방학식 급식 제공: 방학일 다음날부터 배송
      - 개학식 급식 제공: 개학일 전날까지 배송
      - 둘 다 O: 방학+1 ~ 개학-1
      - 둘 다 X: 0

    배송 요일: 월~금 (lunch_days). 공휴일/제외일 제외.
    """
    effective_start, effective_end = _calc_effective_range(
        vacation_date, opening_date, vacation_meal, opening_meal
    )
    if effective_start is None and effective_end is None:
        return 0

    last_day = calendar.monthrange(year, month)[1]
    month_start = date(year, month, 1)
    month_end = date(year, month, last_day)
    holidays = get_kr_holidays(year)
    blocked = set(holidays) | (excluded_dates or set())
    schedule = _get_schedule()[MealType.LUNCH]

    # 기간 보정
    if effective_start is None:
        effective_start = month_start
    if effective_end is None:
        effective_end = month_end

    # 월과 교차
    if effective_start > month_end or effective_end < month_start:
        return 0

    range_start = max(effective_start, month_start)
    range_end = min(effective_end, month_end)

    # ── 배송일 카운트 ──
    total = 0
    d = range_start
    while d <= range_end:
        if d.weekday() in schedule and d not in blocked:
            total += 1
        d += timedelta(days=1)

    return total


def count_lunch_vacation_days_with_note(
    year: int,
    month: int,
    entry,  # LunchSchoolEntry
    grade: int = 0,
    excluded_dates: set[date] | None = None,
) -> int:
    """중식 방학 기간 배송일 수 계산 (비고에 따른 학년 제외 적용).

    entry: LunchSchoolEntry (방학/개학 일정 + 파싱된 비고)
    grade: 학생 학년 (0 → 학년 정보 없음, 제외 로직 무시)
    """
    vacation_meal = entry.vacation_meal
    opening_meal = entry.opening_meal

    # ── 학년 제외 적용 ──
    if grade > 0:
        # 전학년 제외
        if entry.all_grades_excluded_vacation:
            vacation_meal = False
        elif grade in entry.excluded_grades_vacation:
            vacation_meal = False

        if entry.all_grades_excluded_opening:
            opening_meal = False
        elif grade in entry.excluded_grades_opening:
            opening_meal = False

    # ── 기본 배송일 계산 ──
    base_days = count_lunch_vacation_days(
        year,
        month,
        entry.vacation_date,
        entry.opening_date,
        vacation_meal,
        opening_meal,
        excluded_dates,
    )

    # ── 특수 일정 적용 ──
    if entry.special_schedules:
        for sched in entry.special_schedules:
            sched_start = sched.get("start")
            sched_end = sched.get("end")
            meals_provided = sched.get("meals", True)
            if not sched_start or not sched_end:
                continue

            # 월과 교차
            last_day = calendar.monthrange(year, month)[1]
            month_start = date(year, month, 1)
            month_end = date(year, month, last_day)

            if sched_start > month_end or sched_end < month_start:
                continue

            r_start = max(sched_start, month_start)
            r_end = min(sched_end, month_end)

            schedule = _get_schedule()[MealType.LUNCH]
            holidays = get_kr_holidays(year)
            blocked = set(holidays) | (excluded_dates or set())

            if meals_provided:
                # 급식 제공 → 해당 기간에 배송일 추가
                d = r_start
                while d <= r_end:
                    if d.weekday() in schedule and d not in blocked:
                        base_days += 1
                    d += timedelta(days=1)
            else:
                # 미급식 → 해당 기간에 배송일 제외
                d = r_start
                while d <= r_end:
                    if d.weekday() in schedule and d not in blocked:
                        base_days = max(0, base_days - 1)
                    d += timedelta(days=1)

    return base_days
