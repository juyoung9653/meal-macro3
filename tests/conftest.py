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


@pytest.fixture(autouse=True)
def _isolate_roster_archive_settings(monkeypatch, tmp_path):
    """Do not let the operator's archive threshold hide synthetic test rows."""
    import sys

    archive_module = sys.modules.get('src.desktop_v2.inactive_roster')
    if archive_module is None:
        return
    settings_type = archive_module.QSettings
    monkeypatch.setattr(
        archive_module,
        'QSettings',
        lambda *args, **kwargs: settings_type(
            str(tmp_path / 'roster-archive.ini'), settings_type.Format.IniFormat
        ),
    )


@pytest.fixture(scope="session", autouse=True)
def _keep_qt_application_alive():
    """Module-local app fixtures must not destroy Qt between GUI test modules."""
    import sys

    widgets = sys.modules.get("PySide6.QtWidgets")
    if widgets is None:
        yield None
        return
    app = widgets.QApplication.instance() or widgets.QApplication([])
    yield app


@pytest.fixture(autouse=True)
def _collect_qt_objects_on_gui_thread(_keep_qt_application_alive):
    """Release prior-test UI cycles before a later data worker can collect them."""
    yield
    if _keep_qt_application_alive is None:
        return
    import gc
    from PySide6.QtCore import QCoreApplication, QEvent

    gc.collect()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


@pytest.fixture(autouse=True)
def _require_marker_for_native_excel(request, monkeypatch):
    """Keep the quick suite from silently acquiring a new real-Excel dependency."""
    if request.node.get_closest_marker("excel") is not None:
        return
    import sys

    metrics = sys.modules.get("src.excel.substitute_excel_metrics")
    if metrics is None:
        return

    def unmarked_excel():
        pytest.fail("Real Excel requires @pytest.mark.excel and @pytest.mark.slow")

    monkeypatch.setattr(metrics, "_create_excel_application", unmarked_excel)


def pytest_collection_modifyitems(items):
    """A native Excel test must always be excluded by the documented quick command."""
    for item in items:
        if item.get_closest_marker("excel") and not item.get_closest_marker("slow"):
            raise pytest.UsageError(f"{item.nodeid}: excel tests must also have the slow marker")


@pytest.fixture(autouse=True)
def _isolate_delivery_template_preferences_and_backups(monkeypatch, tmp_path):
    """Keep synthetic template backups and selections out of operator folders."""
    import sys
    module = sys.modules.get("src.desktop_v2.delivery_template")
    if module is None:
        return
    store_type = module.DeliveryTemplateStore
    # Some tests replace the factory themselves; their explicit store is isolated.
    if not isinstance(store_type, type):
        return
    original = store_type.__init__
    def initialize(self, database_id, *, profile=module.DocumentTemplateProfile.SCHOOL_MEAL, settings=None, root=None):
        if settings is None:
            settings = module.QSettings(str(tmp_path / "delivery-preferences.ini"), module.QSettings.Format.IniFormat)
        if root is None:
            from hashlib import sha256
            root = tmp_path / "delivery-templates" / sha256(database_id.encode()).hexdigest() / module.DocumentTemplateProfile(profile).value
        original(self, database_id, profile=profile, settings=settings, root=root)
    monkeypatch.setattr(store_type, "__init__", initialize)
