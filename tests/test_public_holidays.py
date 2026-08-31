"""Pinned offline South Korean public-holiday provider checks."""

from datetime import date

import pytest

from src.domain import (
    PUBLIC_HOLIDAY_PROVIDER_ID,
    PublicHolidayCandidate,
    south_korea_public_holiday_candidates,
)


def test_2026_korean_candidates_include_substitute_and_new_constitution_day():
    candidates = south_korea_public_holiday_candidates(2026)
    by_day = {candidate.day: candidate for candidate in candidates}

    assert tuple(by_day) == tuple(sorted(by_day))
    assert by_day[date(2026, 3, 2)].name == "삼일절 대체 휴일"
    assert by_day[date(2026, 7, 17)].name == "제헌절"
    assert all(
        candidate.provider_id == PUBLIC_HOLIDAY_PROVIDER_ID
        for candidate in candidates
    )
    assert all(candidate.provider_version == "0.102" for candidate in candidates)


def test_candidate_key_uses_country_and_date_not_mutable_display_name():
    candidate = PublicHolidayCandidate(
        date(2026, 10, 9),
        "한글날",
        "provider",
        "1.0",
    )

    assert candidate.candidate_key == "KR:2026-10-09"


@pytest.mark.parametrize("year", [1947, 10_000])
def test_provider_rejects_years_outside_supported_calendar(year: int):
    with pytest.raises(ValueError, match="1948~9999"):
        south_korea_public_holiday_candidates(year)
