"""Tests for src.services.note_parser — parse_note, expand_note_dates, apply_note_to_month."""

from datetime import date
from unittest.mock import patch

import pytest

from src.core.settings import SettingsManager
from src.services.note_parser import apply_note_to_month, expand_note_dates, parse_note

from .conftest import KNOWN_HOLIDAYS, make_settings_manager

# ══════════════════════════════════════════════════════
#  Fixtures
# ══════════════════════════════════════════════════════


@pytest.fixture
def mock_settings():
    """Patch get_settings in the note_parser module.

    Default schedule:
      breakfast_days = {4, 5, 6}  # Thu, Fri, Sat (0=Mon)
      dinner_days    = {0, 1, 2, 3}  # Mon-Thu
      lunch_days     = {0, 1, 2, 3, 4}  # Mon-Fri
    """
    sm = make_settings_manager()
    with patch("src.services.note_parser.get_settings", return_value=sm) as m:
        yield m


@pytest.fixture
def mock_holidays():
    """Patch get_kr_holidays in the note_parser module."""
    with patch(
        "src.services.note_parser.get_kr_holidays",
        side_effect=lambda y: KNOWN_HOLIDAYS.get(y, set()),
    ) as m:
        yield m


# ══════════════════════════════════════════════════════
#  parse_note
# ══════════════════════════════════════════════════════


class TestParseNote:
    def test_empty_note(self):
        assert parse_note("", 2025) == []
        assert parse_note("  ", 2025) == []
        assert parse_note(None, 2025) == []

    def test_note_without_dates(self):
        """Note that has no date patterns returns results from non-date patterns only."""
        result = parse_note("그냥 메모", 2025)
        # "그냥 메모" has no date pattern, no weekday pattern, no action keyword
        # It'll be a segment but won't match any pattern → empty results
        assert result == []

    # ── Single date ──

    def test_single_date_pause(self):
        """4/2석식일시중지 → single pause for dinner on April 2."""
        result = parse_note("4/2석식일시중지", 2025)
        assert len(result) == 1
        r = result[0]
        assert r["type"] == "pause"
        assert r["meals"] == {"dinner"}
        assert r["start"] == date(2025, 4, 2)
        assert r["end"] == date(2025, 4, 2)
        assert r["weekdays"] is None

    def test_single_date_stop(self):
        """4/14석식중지 → single stop."""
        result = parse_note("4/14석식중지", 2025)
        assert len(result) == 1
        r = result[0]
        assert r["type"] == "stop"
        assert r["meals"] == {"dinner"}
        assert r["start"] == date(2025, 4, 14)
        assert r["end"] == date(2025, 4, 14)

    def test_single_date_open_ended(self):
        """3/5~재배송 → open-ended resume (start only)."""
        result = parse_note("3/5~재배송", 2026)
        assert len(result) == 1
        r = result[0]
        assert r["type"] == "resume"
        assert r["meals"] == {"breakfast", "dinner", "lunch"}
        assert r["start"] == date(2026, 3, 5)
        assert r["end"] is None

    def test_single_date_with_buta(self):
        """8/1부터 조식만배송희망 → not a standard action word, falls back to weekday_only?

        "부터" triggers open-ended in single_date, and action defaults from "배송희망"
        → "배송" in text → _action_type returns "new".
        """
        result = parse_note("8/1부터조식배송", 2025)
        assert len(result) >= 1
        # "부터" → open-ended (start only)
        r = next(r for r in result if r["type"] == "new")
        assert r["start"] == date(2025, 8, 1)
        assert r["end"] is None
        assert "breakfast" in r["meals"]

    # ── Multi-date ──

    def test_multi_date_pause(self):
        """12/2,3,5,6,7,8,9,10일시중지 → multiple dates."""
        result = parse_note("12/2,3,5,6,7,8,9,10일시중지", 2025)
        assert len(result) == 8
        for r in result:
            assert r["type"] == "pause"
            assert r["start"] == r["end"]
        dates = {r["start"].day for r in result}
        assert dates == {2, 3, 5, 6, 7, 8, 9, 10}

    def test_multi_date_with_meal(self):
        """4/9,14 석식 일시중지."""
        result = parse_note("4/9,14 석식 일시중지", 2025)
        assert len(result) == 2
        for r in result:
            assert r["meals"] == {"dinner"}
            assert r["type"] == "pause"

    # ── Year range ──

    def test_yr_range_open(self):
        """25/7/7~신규배송 → new from 2025-07-07 onward."""
        result = parse_note("25/7/7~신규배송", 2025)
        assert len(result) >= 1
        r = next(r for r in result if r["type"] == "new")
        assert r["start"] == date(2025, 7, 7)
        assert r["end"] is None

    def test_yr_range_full(self):
        """23/11/20~12/20석식일시중지."""
        result = parse_note("23/11/20~12/20석식일시중지", 2023)
        assert len(result) == 1
        r = result[0]
        assert r["type"] == "pause"
        assert r["meals"] == {"dinner"}
        assert r["start"] == date(2023, 11, 20)
        assert r["end"] == date(2023, 12, 20)

    def test_yr_range_cross_year(self):
        """12/20~1/10중지 → Dec 20 to Jan 10 next year."""
        result = parse_note("12/20~1/10중지", 2023)
        r = result[0]
        assert r["type"] == "stop"
        assert r["start"] == date(2023, 12, 20)
        # end month(1) < start month(12) → year + 1
        assert r["end"] == date(2024, 1, 10)

    # ── Range with weekdays ──

    def test_range_with_weekdays(self):
        """26/3/12~화,목(석식) 중지."""
        result = parse_note("26/3/12~화,목(석식) 중지", 2026)
        assert len(result) == 1
        r = result[0]
        assert r["type"] == "stop"
        assert r["meals"] == {"dinner"}
        assert r["start"] == date(2026, 3, 12)
        assert r["end"] is None
        assert r["weekdays"] is not None
        # 화=Tue=1, 목=Thu=3
        assert r["weekdays"] == {1, 3}

    # ── Range abbreviation (same month) ──

    def test_range_abbrev(self):
        """12/5~9중지 → Dec 5-9."""
        result = parse_note("12/5~9중지", 2025)
        assert len(result) == 1
        r = result[0]
        assert r["type"] == "stop"
        assert r["meals"] == {"breakfast", "dinner", "lunch"}
        assert r["start"] == date(2025, 12, 5)
        assert r["end"] == date(2025, 12, 9)

    # ── Tilde end ──

    def test_tilde_end(self):
        """~3/16 신규 → new until March 16."""
        result = parse_note("~3/16 신규", 2025)
        assert len(result) >= 1
        r = result[0]
        assert r["type"] == "new"
        assert r["start"] is None
        assert r["end"] == date(2025, 3, 16)

    # ── Meal detection ──

    def test_meal_detection_breakfast(self):
        result = parse_note("4/2조식일시중지", 2025)
        assert len(result) == 1
        assert result[0]["meals"] == {"breakfast"}

    def test_meal_detection_lunch(self):
        result = parse_note("4/2중식일시중지", 2025)
        assert len(result) == 1
        assert result[0]["meals"] == {"lunch"}

    def test_meal_detection_vacation_lunch(self):
        """방중 → lunch."""
        result = parse_note("4/2방중일시중지", 2025)
        assert len(result) == 1
        assert result[0]["meals"] == {"lunch"}

    def test_meal_detection_all_meals(self):
        """No meal keyword → ALL_MEALS."""
        result = parse_note("4/2일시중지", 2025)
        assert len(result) == 1
        assert result[0]["meals"] == {"breakfast", "dinner", "lunch"}

    def test_daily_weekday_pause(self):
        """매주월,목일시중지(석식만) → weekly weekday pause."""
        result = parse_note("매주월,목일시중지(석식만)", 2025)
        assert len(result) >= 2
        meals_set = {frozenset(r["meals"]) for r in result}
        assert frozenset({"dinner"}) in meals_set

    def test_irrelevant_keyword_alone_returns_empty(self):
        """Note containing ONLY an irrelevant keyword → no date match → empty result."""
        result = parse_note("개명", 2025)
        assert result == []
        result = parse_note("주소변경", 2025)
        assert result == []
        result = parse_note("흰우유", 2025)
        assert result == []

    def test_irrelevant_whitelist_keyword_in_raw(self):
        """When irrelevant keyword is within the date match, it's filtered."""
        # "주소" inside matched text
        result = parse_note("4/2주소변경일시중지", 2025)
        assert len(result) == 0

    def test_irrelevant_not_in_matched_raw(self):
        """Irrelevant keyword in segment but outside regex match → not filtered."""
        result = parse_note("4/2일시중지 개명", 2025)
        assert len(result) == 1  # raw="4/2일시중지", no irrelevant keyword


# ══════════════════════════════════════════════════════
#  expand_note_dates
# ══════════════════════════════════════════════════════


class TestExpandNoteDates:
    def test_empty_note(self):
        assert expand_note_dates("", 2025, 4) == []

    def test_single_date_in_month(self, mock_holidays, mock_settings):
        """4/2석식일시중지 → one (date, meal) pair in April."""
        result = expand_note_dates("4/2석식일시중지", 2025, 4)
        assert len(result) == 1
        d, meals = result[0]
        assert d == date(2025, 4, 2)
        assert meals == {"dinner"}

    def test_single_date_out_of_month(self, mock_holidays, mock_settings):
        """5/2일시중지 filtered to April → no results."""
        result = expand_note_dates("5/2석식일시중지", 2025, 4)
        assert result == []

    def test_multi_date_all_in_month(self, mock_holidays, mock_settings):
        """12/2,3,4일시중지 in December → 3 pairs."""
        result = expand_note_dates("12/2,3,4일시중지", 2025, 12)
        # Each date gets ALL_MEALS (3 meals) since no meal specified
        # → 3 dates × 3 meals = 9 pairs
        assert len(result) == 9
        days = {d.day for d, _ in result}
        assert days == {2, 3, 4}
        # Verify all 3 meals present for day 2
        meals_on_day_2 = [m for d, m in result if d.day == 2]
        all_meals = set().union(*meals_on_day_2)
        assert all_meals == {"breakfast", "dinner", "lunch"}
        assert len(meals_on_day_2) == 3

    def test_deduplication(self, mock_holidays, mock_settings):
        """Same (date, meal) from multiple segments → deduped."""
        result = expand_note_dates("4/2석식일시중지 4/2석식일시중지", 2025, 4)
        assert len(result) == 1

    def test_date_range_spans_month(self, mock_holidays, mock_settings):
        """3/25~4/5중지 filtered to April → dates April 1-5.

        No meal specified → ALL_MEALS (3 meals) → 5 × 3 = 15 pairs.
        """
        result = expand_note_dates("3/25~4/5중지", 2025, 4)
        assert len(result) == 15
        days = {d.day for d, _ in result}
        assert days == {1, 2, 3, 4, 5}

    def test_open_ended_pause(self, mock_holidays, mock_settings):
        """7/7~신규배송 in July → dates 7/1-7/6 (before effective start) are reduction.

        No meal specified → ALL_MEALS (3 meals) → 6 × 3 = 18 pairs.
        """
        result = expand_note_dates("7/7~신규배송", 2025, 7)
        assert len(result) == 18
        days = {d.day for d, _ in result}
        assert days == {1, 2, 3, 4, 5, 6}


# ══════════════════════════════════════════════════════
#  apply_note_to_month
# ══════════════════════════════════════════════════════


class TestApplyNoteToMonth:
    def test_empty_note_returns_zeros(self, mock_holidays, mock_settings):
        """Empty note → no reduction regardless of flags."""
        result = apply_note_to_month("", 2025, 4, False, False, False)
        assert result == {"breakfast": 0, "dinner": 0, "lunch": 0}

    def test_note_without_dates_zeros(self, mock_holidays, mock_settings):
        """Note with no date/action patterns → no reduction."""
        result = apply_note_to_month("그냥 메모", 2025, 4, True, True, True)
        assert result == {"breakfast": 0, "dinner": 0, "lunch": 0}

    def test_dinner_pause_on_delivery_day(self, mock_holidays, mock_settings):
        """4/2석식일시중지: April 2, 2025 is Wednesday (weekday 2).

        dinner_days = {0,1,2,3} (Mon-Thu). Wednesday is in dinner_days → 1 dinner reduction.
        April 2 is NOT in KNOWN_HOLIDAYS → not blocked.
        """
        result = apply_note_to_month(
            "4/2석식일시중지",
            2025,
            4,
            flag_breakfast=True,
            flag_dinner=True,
            flag_lunch=True,
        )
        assert result["breakfast"] == 0
        assert result["dinner"] == 1
        assert result["lunch"] == 0

    def test_dinner_pause_on_non_delivery_day(self, mock_holidays, mock_settings):
        """4/5석식일시중지: April 5, 2025 is Saturday (weekday 5).

        dinner_days = {0,1,2,3} (Mon-Thu). Saturday NOT in dinner_days → 0 dinner reduction.
        lunch_days = {0,1,2,3,4} (Mon-Fri). Saturday NOT in lunch_days → 0 lunch reduction.
        breakfast_days = {4,5,6} (Thu-Sat). Saturday IS in breakfast_days → 1 breakfast reduction.
        But meals={"dinner"}, so only dinner is checked → 0.
        """
        result = apply_note_to_month(
            "4/5석식일시중지",
            2025,
            4,
            flag_breakfast=True,
            flag_dinner=True,
            flag_lunch=True,
        )
        assert result["dinner"] == 0  # Saturday not in dinner schedule

    def test_lunch_pause_on_delivery_day(self, mock_holidays, mock_settings):
        """4/2중식일시중지: April 2 (Wed, weekday 2) is in lunch_days {0-4} → 1 lunch reduction."""
        result = apply_note_to_month(
            "4/2중식일시중지",
            2025,
            4,
            flag_breakfast=True,
            flag_dinner=True,
            flag_lunch=True,
        )
        assert result["lunch"] == 1

    def test_breakfast_pause_on_delivery_day(self, mock_holidays, mock_settings):
        """4/4조식일시중지: April 4 (Fri, weekday 4) is in breakfast_days {4,5,6} → 1."""
        result = apply_note_to_month(
            "4/4조식일시중지",
            2025,
            4,
            flag_breakfast=True,
            flag_dinner=True,
            flag_lunch=True,
        )
        assert result["breakfast"] == 1

    def test_holiday_is_blocked(self, mock_holidays, mock_settings):
        """3/1조식일시중지: March 1 is 삼일절 (holiday). Blocked → 0 reduction.

        Even though it's Saturday (weekday 5, in breakfast_days {4,5,6}),
        the date is blocked by holiday. For breakfast, weekend holidays are NOT
        blocked (SAT/SUN are exempt), but March 1 2025 is Saturday so it should be exempt...
        Actually let me re-check apply_note_to_month logic:

        ``if d in blocked:
            if not (MealType.BREAKFAST in meals and d.weekday() in {SAT, SUN}):
                continue``

        So if the date is blocked AND it IS breakfast on weekend → still count it (don't skip).
        If the date is blocked AND it's NOT breakfast on weekend → skip.

        March 1 2025 is Saturday (weekday 5). If meals={"breakfast"} and d.weekday() in {5,6},
        then it's NOT skipped → reduction counts.

        So breakfast on a holiday weekend still counts as a reduction day. That matches
        the breakfast logic (周末配送分不计入公休日豁免).
        """
        result = apply_note_to_month(
            "3/1조식일시중지",
            2025,
            3,
            flag_breakfast=True,
            flag_dinner=True,
            flag_lunch=True,
        )
        # March 1 is Saturday (weekday 5), in breakfast_days {4,5,6}
        # It's a holiday, but breakfast+weekend → not blocked → reduction counts
        assert result["breakfast"] == 1

    def test_holiday_blocks_non_breakfast(self, mock_holidays, mock_settings):
        """3/3중식일시중지: March 3 is 대체공휴일 (Mon, weekday 0). lunch_days = {0-4}.
        Blocked by holiday and it's not breakfast+weekend → skip. → 0.
        """
        # March 3, 2025 is Monday
        result = apply_note_to_month(
            "3/3중식일시중지",
            2025,
            3,
            flag_breakfast=True,
            flag_dinner=True,
            flag_lunch=True,
        )
        # Blocked by holiday (대체공휴일), not breakfast+weekend → skip
        assert result["lunch"] == 0

    def test_flag_off_ignores_meal(self, mock_holidays, mock_settings):
        """When flag_dinner is False, dinner note should have no effect."""
        result = apply_note_to_month(
            "4/2석식일시중지",
            2025,
            4,
            flag_breakfast=True,
            flag_dinner=False,
            flag_lunch=True,
        )
        assert result["dinner"] == 0  # flag_breakfast=False → skipped

    def test_note_spanning_months(self, mock_holidays, mock_settings):
        """3/25~4/5중지 filtered to April → 5 dates (4/1~4/5).

        All 5 are weekdays (Mon-Fri) → all in lunch_days → 5 lunch reduction.
        """
        result = apply_note_to_month(
            "3/25~4/5중지",
            2025,
            4,
            flag_breakfast=False,
            flag_dinner=False,
            flag_lunch=True,
        )
        # April 1(Tue), 2(Wed), 3(Thu), 4(Fri), 5(Sat)
        # Apr 5 is Saturday, NOT in lunch_days {0-4} → 4 lunch reduction
        assert result["lunch"] == 4
