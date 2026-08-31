"""Offscreen checks for simple DB setup, login, recovery, and restore flows."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
pytest.importorskip(
    "apsw",
    reason="install requirements-sqlite-v2.txt for encrypted DB access UI tests",
)

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QDialog, QLineEdit

from src.desktop_v2 import (
    CreateDatabaseDialog,
    DesktopStorageSettings,
    LoginDialog,
    RecoveryPasswordDialog,
    RestoreBackupDialog,
)
from src.storage import CredentialUnlockError, DatabaseSession


@pytest.fixture(scope="module")
def app():
    application = QApplication.instance() or QApplication([])
    yield application


def _settings(path: Path) -> DesktopStorageSettings:
    return DesktopStorageSettings(
        QSettings(str(path), QSettings.Format.IniFormat)
    )


def test_document_output_directory_round_trips_as_a_path_only_setting(
    tmp_path: Path,
):
    settings_file = tmp_path / "desktop-v2.ini"
    output_directory = tmp_path / "생성 문서"
    output_directory.mkdir()
    settings = _settings(settings_file)

    settings.save_document_output_directory(output_directory)

    assert settings.document_output_directory == output_directory.resolve()
    settings_text = settings_file.read_text(encoding="utf-8")
    assert "[documents]" in settings_text
    assert "output_directory=" in settings_text
    assert "생성 문서" in settings_text


def test_first_setup_creates_db_initial_backup_and_path_only_settings(
    app: QApplication,
    tmp_path: Path,
):
    database_path = tmp_path / "operations.sqlite3"
    backup_directory = tmp_path / "automatic-backups"
    backup_directory.mkdir()
    settings_file = tmp_path / "desktop-v2.ini"
    settings = _settings(settings_file)
    dialog = CreateDatabaseDialog(settings=settings)

    dialog.database_path_edit.setText(str(database_path))
    dialog.backup_directory_edit.setText(str(backup_directory))
    dialog.user_password_edit.setText("everyday-password")
    dialog.user_password_confirmation_edit.setText("everyday-password")
    dialog.recovery_password_edit.setText("institution-recovery")
    dialog.recovery_password_confirmation_edit.setText("institution-recovery")
    dialog._submit()

    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.session is not None
    assert dialog.backup_result.succeeded
    assert settings.database_path == database_path.resolve()
    assert settings.backup_directory == backup_directory.resolve()
    assert len(tuple(backup_directory.glob("*.mealbackup"))) == 1
    settings_text = settings_file.read_text(encoding="utf-8")
    assert "everyday-password" not in settings_text
    assert "institution-recovery" not in settings_text
    dialog.session.close()


def test_setup_explains_recovery_but_keeps_passwords_masked(app: QApplication):
    dialog = CreateDatabaseDialog()

    assert dialog.user_password_edit.echoMode() is QLineEdit.EchoMode.Password
    assert dialog.recovery_password_edit.echoMode() is QLineEdit.EchoMode.Password
    assert "기관 복구" in dialog.findChild(
        type(dialog._error), "recoveryNotice"
    ).text()
    dialog.close()


def test_login_keeps_dialog_open_on_wrong_password_then_opens_on_correct_one(
    app: QApplication,
    tmp_path: Path,
):
    database_path = tmp_path / "operations.sqlite3"
    DatabaseSession.create(
        database_path,
        "correct-password",
        "institution-recovery",
    ).close()
    dialog = LoginDialog(database_path)

    dialog.password_edit.setText("wrong-password")
    dialog._submit()
    assert dialog.result() != QDialog.DialogCode.Accepted
    assert not dialog._error.isHidden()
    assert "비밀번호" in dialog._error.text()

    dialog.password_edit.setText("correct-password")
    dialog._submit()
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.session is not None
    dialog.session.close()


def test_recovery_dialog_changes_only_everyday_password_and_backs_up(
    app: QApplication,
    tmp_path: Path,
):
    database_path = tmp_path / "operations.sqlite3"
    backup_directory = tmp_path / "automatic-backups"
    backup_directory.mkdir()
    DatabaseSession.create(
        database_path,
        "forgotten-password",
        "institution-recovery",
    ).close()
    dialog = RecoveryPasswordDialog(
        database_path,
        backup_directory=backup_directory,
    )

    dialog.recovery_password_edit.setText("institution-recovery")
    dialog.new_password_edit.setText("new-password")
    dialog.new_password_confirmation_edit.setText("new-password")
    dialog._submit()

    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.session is not None
    assert dialog.backup_result.succeeded
    dialog.session.close()
    with pytest.raises(CredentialUnlockError):
        DatabaseSession.open(database_path, "forgotten-password")
    DatabaseSession.open(database_path, "new-password").close()


def test_restore_dialog_uses_institution_password_when_everyday_is_forgotten(
    app: QApplication,
    tmp_path: Path,
):
    source = tmp_path / "lost.sqlite3"
    backup_directory = tmp_path / "automatic-backups"
    backup_directory.mkdir()
    with DatabaseSession.create(
        source,
        "forgotten-password",
        "institution-recovery",
        backup_directory=backup_directory,
    ) as session:
        backup = session.backup_now()
    source.unlink()
    source.with_name(f"{source.name}.credentials.json").unlink()

    restored = tmp_path / "restored.sqlite3"
    settings = _settings(tmp_path / "restored.ini")
    dialog = RestoreBackupDialog(
        destination_database_path=restored,
        backup_directory=backup_directory,
        settings=settings,
    )
    dialog.backup_path_edit.setText(str(backup.path))
    dialog.forgotten_checkbox.setChecked(True)
    dialog.password_edit.setText("institution-recovery")
    dialog.new_password_edit.setText("new-password")
    dialog.new_password_confirmation_edit.setText("new-password")
    dialog._submit()

    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.session is not None
    assert settings.database_path == restored.resolve()
    dialog.session.close()
    with pytest.raises(CredentialUnlockError):
        DatabaseSession.open(restored, "forgotten-password")
    DatabaseSession.open(restored, "new-password").close()
