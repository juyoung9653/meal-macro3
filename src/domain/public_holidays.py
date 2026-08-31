"""Offline South Korean public-holiday candidates for operator review."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime


PUBLIC_HOLIDAY_PROVIDER_ID = "python-holidays:KR"


@dataclass(frozen=True, slots=True, order=True)
class PublicHolidayCandidate:
    """One provider fact shown as a candidate, never an automatic closure."""

    day: date
    name: str
    provider_id: str
    provider_version: str

    def __post_init__(self) -> None:
        if isinstance(self.day, datetime) or not isinstance(self.day, date):
            raise TypeError("공휴일 날짜는 date여야 합니다.")
        object.__setattr__(self, "name", _required_text(self.name, "공휴일 이름"))
        object.__setattr__(
            self,
            "provider_id",
            _required_text(self.provider_id, "공휴일 제공자"),
        )
        object.__setattr__(
            self,
            "provider_version",
            _required_text(self.provider_version, "공휴일 제공자 version"),
        )

    @property
    def candidate_key(self) -> str:
        """Stable business key; a date can have a combined holiday name."""

        return f"KR:{self.day.isoformat()}"


def south_korea_public_holiday_candidates(
    year: int,
) -> tuple[PublicHolidayCandidate, ...]:
    """Generate one year's Korean public holidays without network access."""

    if isinstance(year, bool) or not isinstance(year, int):
        raise TypeError("공휴일 연도는 정수여야 합니다.")
    if not 1948 <= year <= 9999:
        raise ValueError("한국 공휴일 연도는 1948~9999 범위여야 합니다.")

    import holidays

    calendar = holidays.country_holidays(
        "KR",
        years=(year,),
        language="ko",
        observed=True,
        expand=False,
    )
    provider_version = str(holidays.__version__)
    return tuple(
        PublicHolidayCandidate(
            holiday_on,
            name,
            PUBLIC_HOLIDAY_PROVIDER_ID,
            provider_version,
        )
        for holiday_on, name in sorted(calendar.items())
        if holiday_on.year == year
    )


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label}은 문자열이어야 합니다.")
    normalized = " ".join(value.split())
    if not normalized:
        raise ValueError(f"{label}은 비어 있을 수 없습니다.")
    return normalized
