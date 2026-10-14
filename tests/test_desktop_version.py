"""The runtime and packaged executable share one release version."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication

from src.desktop_v2 import __main__ as entry
from src.desktop_v2 import portable_settings
from src.desktop_v2.application import DesktopStartupOutcome
from src.version import APP_VERSION, WINDOWS_VERSION


@pytest.mark.parametrize("status, expected", [(entry.StartupStatus.CANCELLED, 0), (entry.StartupStatus.FAILED, 1)])
def test_entry_exposes_version_without_changing_settings_identity(monkeypatch, tmp_path, status, expected):
    app = QApplication.instance() or QApplication([])
    old = (app.organizationName(), app.applicationName(), app.applicationVersion())
    (tmp_path / "data").mkdir()
    (tmp_path / "data/settings.ini").touch()
    monkeypatch.setattr(portable_settings, "application_directory", lambda: tmp_path)
    errors = []
    monkeypatch.setattr(entry.QMessageBox, "critical", lambda *args: errors.append(args))
    monkeypatch.setattr(entry, "DesktopStartupController", lambda *, settings: SimpleNamespace(
        start=lambda: DesktopStartupOutcome(status)
    ))
    try:
        assert entry.main() == expected
        assert errors == []
        assert app.applicationVersion() == APP_VERSION == "0.1.0"
        assert app.organizationName() == "분당구"
        assert app.applicationName() == "급식 배송 운영 SQLite v2"
    finally:
        QCoreApplication.setOrganizationName(old[0])
        QCoreApplication.setApplicationName(old[1])
        QCoreApplication.setApplicationVersion(old[2])


def test_packaging_uses_shared_windows_resource_and_stable_name(monkeypatch):
    pytest.importorskip("PyInstaller")
    from PyInstaller.utils import hooks
    from PyInstaller.utils.win32.versioninfo import VSVersionInfo

    monkeypatch.setattr(hooks, "collect_data_files", lambda *_args, **_kwargs: [])
    captured = {}
    namespace = {
        "Analysis": lambda *_args, **_kwargs: SimpleNamespace(pure=[], scripts=[], binaries=[], datas=[]),
        "PYZ": lambda *_args: None,
        "EXE": lambda *_args, **kwargs: captured.update(kwargs),
    }
    spec = Path(__file__).resolve().parents[1] / "sqlite_v2.spec"
    exec(compile(spec.read_text(encoding="utf-8"), str(spec), "exec"), namespace)
    resource = captured["version"]
    assert isinstance(resource, VSVersionInfo)
    assert resource.ffi.fileVersionMS == resource.ffi.productVersionMS == 1
    assert resource.ffi.fileVersionLS == resource.ffi.productVersionLS == 0
    assert WINDOWS_VERSION == (0, 1, 0, 0)
    assert captured["name"] == "meal_delivery"
    assert captured["console"] is False
    strings = {item.name: item.val for item in resource.kids[0].kids[0].kids}
    assert strings["FileVersion"] == strings["ProductVersion"] == APP_VERSION
