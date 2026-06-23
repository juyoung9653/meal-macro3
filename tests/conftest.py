"""Shared test fixtures."""

import copy
from datetime import date
from typing import Any

import pytest

from src.core.settings import DEFAULTS, SettingsManager
from src.excel.data_source import DataSource


class InMemoryDataSource(DataSource):
    """Test double that returns pre-configured data instead of reading Excel."""

    def __init__(self, **kwargs):
        self._db_path = kwargs.get("db_path", ":memory:")
        self._roster = kwargs.get("roster", [])
        self._delivery_matrix = kwargs.get("delivery_matrix", [])
        self._delivery_map = kwargs.get("delivery_map", {})
        self._order_map = kwargs.get("order_map", {})
        self._delivery_contacts = kwargs.get("delivery_contacts", [])
        self._holiday_set = kwargs.get("holiday_set", set())
        self._year = kwargs.get("year", 2026)
        self._month = kwargs.get("month", 6)

    @property
    def db_path(self) -> str:
        return self._db_path

    def read_roster(self) -> list[dict]:
        return list(self._roster)

    def read_delivery_matrix(self) -> list[dict]:
        return list(self._delivery_matrix)

    def build_delivery_map(self) -> dict[str, dict]:
        return dict(self._delivery_map)

    def build_order_map(self) -> dict[str, dict]:
        return dict(self._order_map)

    def read_delivery_contacts(self) -> list[dict]:
        return list(self._delivery_contacts)

    def get_holiday_set(self) -> set[date]:
        return set(self._holiday_set)

    def detect_month(self) -> tuple[int, int]:
        return self._year, self._month


# ── Known holidays for testing ──

KNOWN_HOLIDAYS: dict[int, set[date]] = {
    2025: {
        date(2025, 1, 1),  # 신정
        date(2025, 1, 28),  # 설
        date(2025, 1, 29),
        date(2025, 1, 30),
        date(2025, 3, 1),  # 삼일절
        date(2025, 3, 3),  # 대체공휴일
        date(2025, 5, 5),  # 어린이날
        date(2025, 5, 6),  # 대체공휴일
        date(2025, 6, 6),  # 현충일
        date(2025, 8, 15),  # 광복절
        date(2025, 10, 3),  # 개천절
        date(2025, 10, 5),  # 추석
        date(2025, 10, 6),
        date(2025, 10, 7),
        date(2025, 10, 8),  # 대체공휴일
        date(2025, 12, 25),  # 성탄절
    },
    2026: {
        date(2026, 1, 1),
        date(2026, 2, 16),
        date(2026, 2, 17),
        date(2026, 2, 18),
        date(2026, 3, 1),
        date(2026, 5, 5),
        date(2026, 6, 6),
        date(2026, 8, 15),
        date(2026, 10, 3),
        date(2026, 10, 4),
        date(2026, 10, 5),
        date(2026, 10, 6),
        date(2026, 12, 25),
    },
}


@pytest.fixture
def known_holidays() -> dict[int, set[date]]:
    """Return a dict of year → set[date] with known Korean holidays."""
    return copy.deepcopy(KNOWN_HOLIDAYS)


# ── Settings helpers ──


def _build_settings_data(overrides: dict | None = None) -> dict:
    """Build settings _data dict from DEFAULTS, optionally overridden."""
    data = copy.deepcopy(DEFAULTS)
    if overrides:
        _deep_merge(data, overrides)
    return data


def _deep_merge(base: dict, override: dict):
    for k, v in override.items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


def make_settings_manager(overrides: dict | None = None) -> SettingsManager:
    """Create a fresh SettingsManager with given overrides.

    The returned manager has ``_data`` populated from DEFAULTS + overrides.
    It does NOT touch the singleton or the filesystem.
    """
    sm = SettingsManager()
    sm._data = _build_settings_data(overrides)
    return sm


@pytest.fixture
def sample_settings() -> SettingsManager:
    """Returns a SettingsManager with default settings values."""
    return make_settings_manager()


@pytest.fixture(autouse=True)
def _reset_settings_singleton():
    """Reset the SettingsManager singleton before each test.

    This ensures tests don't leak state via the singleton.
    It does NOT load from .settings.json to avoid filesystem coupling.
    """
    SettingsManager._instance = None
    yield
    SettingsManager._instance = None
