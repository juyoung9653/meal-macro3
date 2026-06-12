"""Tests for src.services.delivery_calculator.

count_weekdays, count_delivery_days, to_bool (+相关内部函数).
"""

from datetime import date
from unittest.mock import patch

import pytest

from src.core.settings import SettingsManager
from src.services.delivery_calculator import (
    count_delivery_days,
    count_weekdays,
    get_week_ranges,
    to_bool,
)

from .conftest import KNOWN_HOLIDAYS, make_settings_manager

# ══════════════════════════════════════════════════════
#  Fixtures
# ══════════════════════════════════════════════════════


@pytest.fixture
def mock_both_settings():
    """Patch get_settings in schedule_counter, delivery_calculator, and note_parser.

    schedule_counter._get_schedule() calls get_settings from schedule_counter module.
    apply_note_to_month() (imported from note_parser) calls get_settings from note_parser module.
    Both need to return the same controlled config.
    """
    sm = make_settings_manager()
    with (
        patch("src.services.schedule_counter.get_settings", return_value=sm),
        patch("src.services.note_parser.get_settings", return_value=sm),
    ):
        yield sm


@pytest.fixture
def mock_both_holidays():
    """Patch get_kr_holidays across all service modules."""
    with (
        patch(
            "src.services.delivery_calculator.get_kr_holidays",
            side_effect=lambda y: KNOWN_HOLIDAYS.get(y, set()),
        ),
        patch(
            "src.services.schedule_counter.get_kr_holidays",
            side_effect=lambda y: KNOWN_HOLIDAYS.get(y, set()),
        ),
        patch(
            "src.services.vacation_calculator.get_kr_holidays",
            side_effect=lambda y: KNOWN_HOLIDAYS.get(y, set()),
        ),
        patch(
            "src.services.note_parser.get_kr_holidays",
            side_effect=lambda y: KNOWN_HOLIDAYS.get(y, set()),
        ),
    ):
        yield


# ══════════════════════════════════════════════════════
#  to_bool
# ══════════════════════════════════════════════════════


class TestToBool:
    def test_int_positive(self):
        assert to_bool(1) is True
        assert to_bool(42) is True

    def test_int_non_positive(self):
        assert to_bool(0) is False
        assert to_bool(-1) is False

    def test_float_positive(self):
        assert to_bool(1.0) is True
        assert to_bool(0.5) is True

    def test_float_non_positive(self):
        assert to_bool(0.0) is False
        assert to_bool(-0.5) is False

    def test_str_true_values(self):
        assert to_bool("1") is True
        assert to_bool("1.0") is True
        assert to_bool("Y") is True
        assert to_bool("y") is True
        assert to_bool("예") is True
        assert to_bool("있음") is True

    def test_str_false_values(self):
        assert to_bool("") is False
        assert to_bool("0") is False
        assert to_bool("0.0") is False
        assert to_bool("N") is False
        assert to_bool("n") is False
        assert to_bool("아니오") is False
        assert to_bool("없음") is False

    def test_bool_input(self):
        assert to_bool(True) is True
        assert to_bool(False) is False

    def test_none(self):
        assert to_bool(None) is False  # bool(None) → False


# ══════════════════════════════════════════════════════
#  count_weekdays
# ══════════════════════════════════════════════════════


class TestCountWeekdays:
    """Note: count_weekdays calls _is_holiday() → get_kr_holidays(d.year)."""

    def test_no_holidays(self, mock_both_holidays):
        """April 2025 has 30 days. Mon-Thu (dow_set={0,1,2,3}) in April 2025.

        April 1, 2025 = Tuesday. Let's count manually:
        - Week 1: 1(Tue), 2(Wed), 3(Thu)
        - Week 2: 7(Mon), 8(Tue), 9(Wed), 10(Thu)
        - Week 3: 14(Mon), 15(Tue), 16(Wed), 17(Thu)
        - Week 4: 21(Mon), 22(Tue), 23(Wed), 24(Thu)
        - Week 5: 28(Mon), 29(Tue), 30(Wed)
        Total: 4 + 5 + 5 + 4 = 18
        """
        result = count_weekdays(2025, 4, {0, 1, 2, 3})
        assert result == 18

    def test_with_excluded_dates(self, mock_both_holidays):
        """April 2025, exclude {April 2, April 3}. Mon-Thu → 18 - 2 = 16."""
        excluded = {date(2025, 4, 2), date(2025, 4, 3)}
        result = count_weekdays(2025, 4, {0, 1, 2, 3}, excluded_dates=excluded)
        assert result == 16

    def test_with_holidays_march(self, mock_both_holidays):
        """March 2025 has holidays on 3/1 (Sat), 3/3 (Mon).

        For Mon-Fri (dow_set={0,1,2,3,4}):
        - 3/3 (Mon) is 대체공휴일 → excluded by holiday check
        - 3/1 (Sat) is holiday, but Sat is NOT in {0-4} → not counted anyway

        Total weekdays in March 2025: 21
        Holidays on weekdays: 3/3 (Mon) → 1 excluded
        Result: 21 - 1 = 20 (if holiday check applies)

        Wait, count_weekdays checks _is_holiday(d) which uses get_kr_holidays.
        If d is a holiday AND not in holiday_exempt_days, it's skipped.
        So 3/3 is a Monday (weekday 0) in dow_set, and is a holiday → skipped.
        """
        result = count_weekdays(2025, 3, {0, 1, 2, 3, 4})
        # 3/3 is in KNOWN_HOLIDAYS, skip it
        assert result == 20  # 21 weekdays - 1 holiday

    def test_holiday_exempt_weekend(self, mock_both_holidays):
        """March 2025. breakfast_days = {4,5,6} (Fri, Sat, Sun).

        3/1 (Sat) is 삼일절.
        With holiday_exempt_days={5,6} (SAT,SUN):
        - 3/1 is Sat (5) and in holiday_exempt_days → NO holiday check → counted.
        - 3/2 is Sun (6) and in holiday_exempt_days → NO holiday check → counted.
        - 3/3 is Mon (0) and NOT in holiday_exempt_days → holiday check → skipped.

        March 2025 has:
        - Fridays (4): 7, 14, 21, 28 → 4
        - Saturdays (5): 1, 8, 15, 22, 29 → 5 (all in exempt)
        - Sundays (6): 2, 9, 16, 23, 30 → 5 (all in exempt)
        Total: 4 + 5 + 5 = 14
        """
        result = count_weekdays(2025, 3, {4, 5, 6}, holiday_exempt_days={5, 6})
        assert result == 14  # Fri(4) + Sat(5, exempt) + Sun(6, exempt) = 14

    def test_no_dow_match(self, mock_both_holidays):
        """No days match the dow_set."""
        result = count_weekdays(2025, 4, set())
        assert result == 0


# ══════════════════════════════════════════════════════
#  count_delivery_days
# ══════════════════════════════════════════════════════


class TestCountDeliveryDays:
    def test_no_flags(self, mock_both_settings, mock_both_holidays):
        """All flags False → all zeros regardless of month."""
        result = count_delivery_days(2025, 4, False, False, False)
        assert result == {"breakfast": 0, "dinner": 0, "lunch": 0}

    def test_breakfast_only_no_note(self, mock_both_settings, mock_both_holidays):
        """Breakfast flag only, no note.

        April 2025.
        breakfast_days = {4,5,6} (Thu, Fri, Sat).

        April 2025 starts Tuesday.
        Thu: 3,10,17,24 → 4
        Fri: 4,11,18,25 → 4
        Sat: 5,12,19,26 → 4
        Total: 12

        No holidays in April.
        holiday_exempt_days for breakfast = {SAT, SUN} = {5, 6}
        So Sat(5) and Sun(6) are exempt from holiday check. But there are no
        holidays in April anyway.
        """
        result = count_delivery_days(
            2025, 4, flag_breakfast=True, flag_dinner=False, flag_lunch=False
        )
        assert result["breakfast"] == 12
        assert result["dinner"] == 0
        assert result["lunch"] == 0

    def test_dinner_only_no_note(self, mock_both_settings, mock_both_holidays):
        """Dinner flag only, no note.

        April 2025.
        dinner_days = {0,1,2,3} (Mon-Thu).
        Mon-Thu in April: 18 days (see test_no_holidays above).
        No holidays in April.
        """
        result = count_delivery_days(
            2025, 4, flag_breakfast=False, flag_dinner=True, flag_lunch=False
        )
        assert result["dinner"] == 18
        assert result["breakfast"] == 0
        assert result["lunch"] == 0

    def test_all_flags_no_note(self, mock_both_settings, mock_both_holidays):
        """All flags True, no note.

        April 2025.
        breakfast (Thu-Sat): 12
        So:
        breakfast: 12
        dinner: 18
        lunch: 22
        """
        result = count_delivery_days(
            2025, 4, flag_breakfast=True, flag_dinner=True, flag_lunch=True
        )
        assert result["breakfast"] == 12
        assert result["dinner"] == 18
        assert result["lunch"] == 22

    def test_with_note_reduction(self, mock_both_settings, mock_both_holidays):
        """All flags True, with dinner pause note.

        Note: "4/2석식일시중지" → dinner reduction on Apr 2 (Wednesday, weekday 2).
        Apr 2 is in dinner_days {0,1,2,3} → 1 dinner reduction.

        Base: breakfast=12, dinner=18, lunch=22
        Reduction: breakfast=0, dinner=1, lunch=0
        Result: breakfast=12, dinner=17, lunch=22
        """
        result = count_delivery_days(
            2025,
            4,
            flag_breakfast=True,
            flag_dinner=True,
            flag_lunch=True,
            note="4/2석식일시중지",
        )
        assert result["breakfast"] == 12
        assert result["dinner"] == 17
        assert result["lunch"] == 22

    def test_reduction_not_below_zero(self, mock_both_settings, mock_both_holidays):
        """Reduction clamped to 0 when note exceeds base delivery days.

        January 2025: 23 weekdays (Mon-Fri), 4 holidays on weekdays
        (Jan 1 신정, Jan 28-30 설) → lunch base = 19.
        Note "1/2~31중지" spans Jan 2-31. Holidays in range (Jan 28-30)
        are blocked in apply_note_to_month. Remaining weekdays in range = 19.
        Result = max(0, 19 - 19) = 0.
        """
        result_no_note = count_delivery_days(
            2025,
            1,
            flag_breakfast=False,
            flag_dinner=False,
            flag_lunch=True,
        )
        assert result_no_note["lunch"] == 19

        result = count_delivery_days(
            2025,
            1,
            flag_breakfast=False,
            flag_dinner=False,
            flag_lunch=True,
            note="1/2~31중지",
        )
        assert result["lunch"] == 0
        assert result["breakfast"] == 0
        assert result["dinner"] == 0


# ══════════════════════════════════════════════════════
#  get_week_ranges
# ══════════════════════════════════════════════════════


class TestGetWeekRanges:
    """Tests for week division logic."""

    def test_normal_5_weeks(self):
        """April 2025 (30 days, starts Tue) → 5 weeks."""
        weeks = get_week_ranges(2025, 4)
        assert len(weeks) == 5
        # Week 1: 4/1(Tue) ~ 4/5(Sat)
        assert weeks[0] == (1, date(2025, 4, 1), date(2025, 4, 5))
        # Week 5: 4/27(Sun) ~ 4/30(Wed, last_day)
        assert weeks[4] == (5, date(2025, 4, 27), date(2025, 4, 30))

    def test_31days_friday_start_merge_last(self):
        """May 2026 (31 days, starts Fri) → 5 weeks, last 2 merged.

        Without fix: 6 weeks (w5: 5/24~5/30, w6: 5/31 alone).
        With fix: 5 weeks (w5: 5/24~5/31 merged).
        """
        weeks = get_week_ranges(2026, 5)
        assert len(weeks) == 5
        # Week 1: 5/1(Fri) ~ 5/2(Sat)
        assert weeks[0] == (1, date(2026, 5, 1), date(2026, 5, 2))
        # Week 2-4 should be normal Sun-Sat weeks
        # Week 5: merged 5/24(Sun) ~ 5/31(Sun)
        assert weeks[4] == (5, date(2026, 5, 24), date(2026, 5, 31))

    def test_31days_saturday_start_merge_first(self):
        """August 2026 (31 days, starts Sat) → 5 weeks, first 2 merged.

        Without fix: 6 weeks (w1: 8/1 alone, w2: 8/2~8/8).
        With fix: 5 weeks (w1: 8/1~8/8 merged).
        """
        weeks = get_week_ranges(2026, 8)
        assert len(weeks) == 5
        # Week 1: merged 8/1(Sat) ~ 8/8(Sat)
        assert weeks[0] == (1, date(2026, 8, 1), date(2026, 8, 8))
        # Week 5: 8/30(Sun) ~ 8/31(Mon)
        assert weeks[4] == (5, date(2026, 8, 30), date(2026, 8, 31))

    def test_30days_no_merge(self):
        """June 2026 (30 days, starts Mon) → 5 weeks, no merge needed."""
        weeks = get_week_ranges(2026, 6)
        assert len(weeks) == 5
        # Week 1: 6/1(Mon) ~ 6/6(Sat)
        assert weeks[0] == (1, date(2026, 6, 1), date(2026, 6, 6))

    def test_28days_february(self):
        """February 2026 (28 days, starts Sun) → 4 weeks."""
        weeks = get_week_ranges(2026, 2)
        assert len(weeks) == 4
        # Week 4: 2/22(Sun) ~ 2/28(Sat)
        assert weeks[3] == (4, date(2026, 2, 22), date(2026, 2, 28))
