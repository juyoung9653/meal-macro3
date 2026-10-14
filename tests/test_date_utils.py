"""Tests for src.core.date_utils — 날짜 범위, 요일 상수, 주차 계산, to_bool."""

from datetime import date

import pytest

from src.core.date_utils import (
    FRI,
    MON,
    SAT,
    SUN,
    THU,
    TUE,
    WED,
    get_week_ranges,
    to_bool,
    week_date_str,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param(1, True, id="int-one"),
        pytest.param(0, False, id="int-zero"),
        pytest.param(1.0, True, id="float-one"),
        pytest.param(0.0, False, id="float-zero"),
        pytest.param("O", True, id="letter-o"),
        pytest.param("TRUE", True, id="upper-true"),
        pytest.param("true", True, id="lower-true"),
        pytest.param("급식", True, id="meal-label"),
        pytest.param("1", True, id="string-one"),
        pytest.param("", False, id="empty"),
        pytest.param(True, True, id="bool-true"),
        pytest.param(False, False, id="bool-false"),
        pytest.param(None, False, id="none"),
    ],
)
def test_to_bool(value, expected):
    assert to_bool(value) is expected


class TestWeekRanges:
    def test_june_2026(self):
        """2026년 6월: 1일 월요일, 30일 화요일 → 5주."""
        weeks = get_week_ranges(2026, 6)
        assert len(weeks) == 5
        # 1주차: 6/1(월) ~ 6/6(토)
        assert weeks[0][0] == 1
        assert weeks[0][1] == date(2026, 6, 1)
        assert weeks[0][2] == date(2026, 6, 6)
        assert [number for number, _, _ in weeks] == [1, 2, 3, 4, 5]

    def test_31day_6weeks_merge(self):
        """31일 월 + 금요일 시작 → 마지막 2주 병합."""
        # 2026년 5월: 1일 금요일, 31일 일요일 → 6주 → 병합
        weeks = get_week_ranges(2026, 5)
        assert len(weeks) == 5  # 6주였던 것이 5주로 병합됨

    def test_30day_saturday_start_merges_first_weeks(self):
        """30일 월 + 토요일 시작도 처음 2주를 병합해 5주로 제한."""
        # 2025년 11월: 1일 토요일, 30일 일요일
        weeks = get_week_ranges(2025, 11)

        assert len(weeks) == 5
        assert weeks[0] == (1, date(2025, 11, 1), date(2025, 11, 8))
        assert weeks[-1] == (5, date(2025, 11, 30), date(2025, 11, 30))

    def test_feb_2026(self):
        """2026년 2월: 28일."""
        weeks = get_week_ranges(2026, 2)
        assert len(weeks) >= 4


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
