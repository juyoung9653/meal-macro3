"""Startup routing between initial database creation and login."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from src.desktop_v2.database_access import (
    CreateDatabaseDialog,
    LoginDialog,
)
from src.desktop_v2.application import DesktopStartupController
from src.desktop_v2.portable_settings import DesktopStorageSettings
from src.storage.database_credentials import credential_path_for


@pytest.fixture(scope="module")
def app():
    instance = QApplication.instance() or QApplication([])
    yield instance


def _controller(settings, create_factory, login_factory):
    return DesktopStartupController(
        settings=settings,
        create_dialog_type=create_factory,
        login_dialog_type=login_factory,
    )


def test_unset_database_path_routes_to_creation():
    settings = SimpleNamespace(database_path=None, backup_directory=None)
    create_dialog = object()
    selected = []

    controller = _controller(
        settings,
        lambda **kwargs: selected.append(("create", kwargs)) or create_dialog,
        lambda *args, **kwargs: selected.append(("login", args, kwargs)),
    )

    assert controller._access_dialog(None) is create_dialog
    assert selected[0][0] == "create"


def test_deleted_saved_database_routes_to_creation_and_cancel_preserves_path(
    app: QApplication,
    tmp_path: Path,
):
    database_path = tmp_path / "removed.sqlite3"
    database_path.write_bytes(b"former database")
    database_path.unlink()
    raw_settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    settings = DesktopStorageSettings(raw_settings)
    settings.save_paths(database_path, tmp_path / "backups")

    controller = _controller(settings, CreateDatabaseDialog, LoginDialog)
    dialog = controller._access_dialog(None)

    assert isinstance(dialog, CreateDatabaseDialog)
    assert dialog.result() == 0
    dialog.reject()
    assert settings.database_path == database_path.resolve()
    assert not database_path.exists()


def test_existing_database_routes_to_login_even_without_credentials_sidecar(
    tmp_path: Path,
):
    database_path = tmp_path / "existing.sqlite3"
    database_path.write_bytes(b"possibly corrupt database")
    settings = SimpleNamespace(
        database_path=database_path,
        backup_directory=tmp_path / "backups",
    )
    login_dialog = object()
    selected = []

    controller = _controller(
        settings,
        lambda **kwargs: selected.append(("create", kwargs)),
        lambda *args, **kwargs: selected.append(("login", args, kwargs)) or login_dialog,
    )

    assert controller._access_dialog(None) is login_dialog
    assert selected[0][0] == "login"
    assert selected[0][1][0] == database_path
    assert not credential_path_for(database_path).exists()
