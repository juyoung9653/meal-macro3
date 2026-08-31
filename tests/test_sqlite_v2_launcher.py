"""Packaged SQLite v2 launcher smoke contract."""

from sqlite_v2 import packaging_smoke


def test_packaging_smoke_loads_pinned_korean_holiday_data():
    assert packaging_smoke() == 0
