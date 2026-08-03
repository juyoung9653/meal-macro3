"""날짜 유틸리티 — 날짜 범위, 요일 상수, 주차 계산.

excel_utils에서 분리: Excel 연산과 무관한 순수 날짜 로직.
"""

import calendar as _cal
from datetime import date

# ── 요일 상수 ──
MON, TUE, WED, THU, FRI, SAT, SUN = range(7)


def to_bool(val) -> bool:
    """다양한 입력을 bool로 변환."""
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return val > 0
    if isinstance(val, str):
        v = val.strip().upper()
        return v in ("1", "1.0", "Y", "예", "있음", "O", "TRUE", "급식")
    return bool(val)


def get_week_ranges(year: int, month: int) -> list[tuple[int, date, date]]:
    """해당 월을 일(요일)~토(요일) 주차 단위로 분할.

    6주가 되는 달은 확인서의 5주 형식에 맞게 보정한다.
    금요일 시작 → 마지막 2주 병합, 토요일 시작 → 처음 2주 병합.
    """
    last_day = _cal.monthrange(year, month)[1]
    first_wday = date(year, month, 1).weekday()
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

    # ── 6주가 되는 달을 확인서의 5주 형식으로 보정 ──
    if len(weeks) == 6:
        if first_wday == FRI:
            w5 = weeks[-2]
            w6 = weeks[-1]
            weeks = weeks[:-2] + [(w5[0], w5[1], w6[2])]
        elif first_wday == SAT:
            w1 = weeks[0]
            w2 = weeks[1]
            weeks = [(w1[0], w1[1], w2[2])] + weeks[2:]

    # ── 주차 번호 재부여 ──
    weeks = [(i + 1, ws, we) for i, (_, ws, we) in enumerate(weeks)]
    return weeks


def week_date_str(w_start: date, w_end: date) -> str:
    """주차 날짜 문자열 (예: '06.01~06.07')."""
    return f"{w_start.month:02d}.{w_start.day:02d}~{w_end.month:02d}.{w_end.day:02d}"
