"""Tests for src.core.date_utils — date_range, weekday constants."""

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
)


class TestWeekdayConstants:
    def test_values(self):
        assert MON == 0
        assert TUE == 1
        assert WED == 2
        assert THU == 3
        assert FRI == 4
        assert SAT == 5
        assert SUN == 6


class TestDateRange:
    def test_single_day(self):
        d = date(2025, 3, 15)
        result = list(date_range(d, d))
        assert result == [d]

    def test_three_days(self):
        start = date(2025, 3, 15)
        end = date(2025, 3, 17)
        result = list(date_range(start, end))
        assert result == [
            date(2025, 3, 15),
            date(2025, 3, 16),
            date(2025, 3, 17),
        ]

    def test_full_month_january(self):
        """January has 31 days."""
        start = date(2025, 1, 1)
        end = date(2025, 1, 31)
        result = list(date_range(start, end))
        assert len(result) == 31
        assert result[0] == date(2025, 1, 1)
        assert result[-1] == date(2025, 1, 31)

    def test_leap_year_february(self):
        """2024 is a leap year, February has 29 days."""
        start = date(2024, 2, 1)
        end = date(2024, 2, 29)
        result = list(date_range(start, end))
        assert len(result) == 29
        assert result[0] == date(2024, 2, 1)
        assert result[-1] == date(2024, 2, 29)

    def test_non_leap_year_february(self):
        """2025 is not a leap year, February has 28 days."""
        start = date(2025, 2, 1)
        end = date(2025, 2, 28)
        result = list(date_range(start, end))
        assert len(result) == 28
        assert result[0] == date(2025, 2, 1)
        assert result[-1] == date(2025, 2, 28)

    def test_year_boundary(self):
        """Spans across New Year."""
        start = date(2025, 12, 30)
        end = date(2026, 1, 2)
        result = list(date_range(start, end))
        assert len(result) == 4
        assert result[0] == date(2025, 12, 30)
        assert result[1] == date(2025, 12, 31)
        assert result[2] == date(2026, 1, 1)
        assert result[3] == date(2026, 1, 2)

    def test_start_after_end_yields_empty(self):
        """start > end yields no dates."""
        result = list(date_range(date(2025, 3, 17), date(2025, 3, 15)))
        assert result == []

    def test_generator_lazy(self):
        """date_range returns a generator (not a list)."""
        g = date_range(date(2025, 1, 1), date(2025, 1, 5))
        import types

        assert isinstance(g, types.GeneratorType)
