"""PyInstaller-friendly launcher for the SQLite v2 desktop application."""

from datetime import date
import sys

from src.desktop_v2.__main__ import main
from src.domain import south_korea_public_holiday_candidates


def packaging_smoke() -> int:
    """Return success only when the bundled Korean calendar is usable."""

    candidates = {
        candidate.day: candidate.name
        for candidate in south_korea_public_holiday_candidates(2026)
    }
    expected = {
        date(2026, 3, 2): "삼일절 대체 휴일",
        date(2026, 7, 17): "제헌절",
    }
    return 0 if all(candidates.get(day) == name for day, name in expected.items()) else 2


if __name__ == "__main__":
    if "--packaging-smoke" in sys.argv:
        raise SystemExit(packaging_smoke())
    raise SystemExit(main())
