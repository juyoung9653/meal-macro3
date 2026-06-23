"""Tests for src.core.date_utils — 날짜 범위, 요일 상수, 주차 계산, to_bool."""

from datetime import date

from src.core.date_utils import (
    FRI,
    MON,
    SAT,
    SUN,
    THU,
    TUE,
    WED,
    date_range,
    get_week_ranges,
    to_bool,
    week_date_str,
)


class TestDateRange:
    def test_single_day(self):
        d = date(2026, 6, 15)
        assert list(date_range(d, d)) == [d]

    def test_three_days(self):
        result = list(date_range(date(2026, 6, 1), date(2026, 6, 3)))
        assert result == [date(2026, 6, 1), date(2026, 6, 2), date(2026, 6, 3)]

    def test_cross_month(self):
        result = list(date_range(date(2026, 1, 30), date(2026, 2, 2)))
        assert len(result) == 4
        assert result[0] == date(2026, 1, 30)
        assert result[-1] == date(2026, 2, 2)


class TestToBool:
    def test_int_1(self):
        assert to_bool(1) is True

    def test_int_0(self):
        assert to_bool(0) is False

    def test_float_1_0(self):
        assert to_bool(1.0) is True

    def test_float_0_0(self):
        assert to_bool(0.0) is False

    def test_str_O(self):
        assert to_bool("O") is True

    def test_str_1(self):
        assert to_bool("1") is True

    def test_str_empty(self):
        assert to_bool("") is False

    def test_bool(self):
        assert to_bool(True) is True
        assert to_bool(False) is False

    def test_none(self):
        assert to_bool(None) is False


class TestWeekRanges:
    def test_june_2026(self):
        """2026년 6월: 1일 월요일, 30일 화요일 → 5주."""
        weeks = get_week_ranges(2026, 6)
        assert len(weeks) == 5
        # 1주차: 6/1(월) ~ 6/6(토)
        assert weeks[0][0] == 1
        assert weeks[0][1] == date(2026, 6, 1)
        assert weeks[0][2] == date(2026, 6, 6)

    def test_31day_6weeks_merge(self):
        """31일 월 + 금요일 시작 → 마지막 2주 병합."""
        # 2026년 5월: 1일 금요일, 31일 일요일 → 6주 → 병합
        weeks = get_week_ranges(2026, 5)
        assert len(weeks) == 5  # 6주였던 것이 5주로 병합됨

    def test_feb_2026(self):
        """2026년 2월: 28일."""
        weeks = get_week_ranges(2026, 2)
        assert len(weeks) >= 4

    def test_week_numbers_sequential(self):
        weeks = get_week_ranges(2026, 6)
        for i, (wn, _, _) in enumerate(weeks):
            assert wn == i + 1


class TestWeekDateStr:
    def test_format(self):
        result = week_date_str(date(2026, 6, 1), date(2026, 6, 7))
        assert result == "06.01~06.07"

    def test_cross_month(self):
        result = week_date_str(date(2026, 5, 25), date(2026, 6, 7))
        assert result == "05.25~06.07"


class TestWeekdayConstants:
    def test_values(self):
        assert MON == 0
        assert TUE == 1
        assert WED == 2
        assert THU == 3
        assert FRI == 4
        assert SAT == 5
        assert SUN == 6
