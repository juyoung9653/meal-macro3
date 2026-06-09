"""날짜 유틸리티 — 날짜 범위, 요일 상수.

excel_utils에서 분리: Excel 연산과 무관한 순수 날짜 로직.
"""

from datetime import date, timedelta

# ── 요일 상수 ──
MON, TUE, WED, THU, FRI, SAT, SUN = range(7)


def date_range(start: date, end: date):
    """start~end 사이 날짜 생성기."""
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)
