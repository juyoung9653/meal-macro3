"""Shared test fixtures."""

import copy

import pytest

from src.core.settings import DEFAULTS, SettingsManager

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


@pytest.fixture(autouse=True)
def _isolate_roster_column_settings(monkeypatch, tmp_path):
    """Keep window tests from reading or changing the operator's column layout."""
    import sys

    layout_module = sys.modules.get('src.desktop_v2.column_layout')
    if layout_module is None:
        return
    settings_type = layout_module.QSettings
    monkeypatch.setattr(
        layout_module,
        'QSettings',
        lambda *args, **kwargs: settings_type(
            str(tmp_path / 'roster-columns.ini'), settings_type.Format.IniFormat
        ),
    )
