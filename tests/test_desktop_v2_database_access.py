"""Offscreen checks for simple DB setup, login, and restore flows."""

from __future__ import annotations

import os
import hashlib
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
pytest.importorskip(
    "apsw",
    reason="install requirements-sqlite-v2.txt for encrypted DB access UI tests",
)

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QDialog, QLineEdit, QPushButton

from src.desktop_v2 import (
    CreateDatabaseDialog,
    DesktopStorageSettings,
    LoginDialog,
    RestoreBackupDialog,
)
from src.desktop_v2.database_access import BackupDirectoryDialog, DatabaseLocationDialog, DatabasePasswordChangeDialog, relocate_database
from src.storage import AutomaticBackupResult, DatabaseSession, credential_path_for, inspect_backup
from src.storage.database_backup import DatabaseBackupService
from tests.test_database_backup import REVISION_ID, _record


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
    assert dialog.session.credentials.recovery_slot is None
    dialog.session.close()


def test_location_change_preserves_revision_credentials_and_restart_path(app, tmp_path):
    source = tmp_path / "operations.sqlite3"
    destination = tmp_path / "new-location" / "operations.sqlite3"
    destination.parent.mkdir()
    backups = tmp_path / "backups"
    backups.mkdir()
    settings_path = tmp_path / "settings.ini"
    settings = _settings(settings_path)
    settings.save_paths(source, backups)

    with DatabaseSession.create(source, "everyday-password", backup_directory=backups) as session:
        session.append_revision(_record())
        expected_revision = session.database.revision(REVISION_ID)
        dialog = DatabaseLocationDialog(session, settings)
        dialog.destination_path_edit.setText(str(destination))
        dialog.password_edit.setText("everyday-password")
        dialog._submit()
        assert dialog.result() == QDialog.DialogCode.Accepted
        assert session.database.revision(REVISION_ID) == expected_revision

    assert source.exists()
    assert settings.database_path == destination.resolve()
    assert settings.backup_directory == backups.resolve()
    assert _settings(settings_path).database_path == destination.resolve()
    with DatabaseSession.open(destination, "everyday-password", backup_directory=backups) as reopened:
        assert reopened.database.revision(REVISION_ID) == expected_revision
    assert "everyday-password" not in settings_path.read_text(encoding="utf-8")


def test_backup_folder_change_verifies_first_backup_and_switches_live_session(app, tmp_path, monkeypatch):
    source = tmp_path / "operations.sqlite3"
    old_folder = tmp_path / "old-backups"
    new_folder = tmp_path / "new-backups"
    old_folder.mkdir()
    new_folder.mkdir()
    settings_file = tmp_path / "settings.ini"
    settings = _settings(settings_file)
    settings.save_paths(source, old_folder)
    prune_calls = []
    original_create = DatabaseBackupService.create_backup

    def recording_create(self, *args, **kwargs):
        prune_calls.append((self.directory, kwargs.get("prune", True)))
        return original_create(self, *args, **kwargs)

    monkeypatch.setattr(DatabaseBackupService, "create_backup", recording_create)
    with DatabaseSession.create(source, "password", backup_directory=old_folder) as session:
        old_archive = session.backup_now().path
        old_hash = hashlib.sha256(old_archive.read_bytes()).digest()
        dialog = BackupDirectoryDialog(session, settings)
        assert dialog.current_directory_edit.text() == str(old_folder.resolve())
        assert dialog.current_directory_edit.isReadOnly()
        dialog.backup_directory_edit.setText(str(new_folder))
        dialog._submit()
        assert dialog.result() == QDialog.DialogCode.Accepted
        assert session.backup_directory == new_folder.resolve()
        first_new = tuple(new_folder.glob("*.mealbackup"))
        assert len(first_new) == 1
        assert inspect_backup(first_new[0]).path == first_new[0]
        assert (new_folder.resolve(), False) in prune_calls
        session.backup_now()
        assert len(tuple(new_folder.glob("*.mealbackup"))) == 2
        assert hashlib.sha256(old_archive.read_bytes()).digest() == old_hash
        assert tuple(old_folder.glob("*.mealbackup")) == (old_archive,)

    assert settings.database_path == source.resolve()
    assert _settings(settings_file).backup_directory == new_folder.resolve()
    with DatabaseSession.open(source, "password", backup_directory=_settings(settings_file).backup_directory) as reopened:
        assert reopened.backup_directory == new_folder.resolve()


def test_backup_folder_cancel_same_path_and_invalid_paths_are_noops(app, tmp_path):
    source = tmp_path / "operations.sqlite3"
    folder = tmp_path / "backups"
    folder.mkdir()
    settings = _settings(tmp_path / "settings.ini")
    settings.save_paths(source, folder)
    with DatabaseSession.create(source, "password", backup_directory=folder) as session:
        dialog = BackupDirectoryDialog(session, settings)
        dialog.backup_directory_edit.setText(str(tmp_path / "unused"))
        dialog.reject()
        assert not tuple(folder.glob("*.mealbackup"))
        assert session.backup_directory == folder.resolve()

        dialog = BackupDirectoryDialog(session, settings)
        dialog.backup_directory_edit.setText(str(folder).upper())
        dialog._submit()
        assert dialog.result() == QDialog.DialogCode.Accepted
        assert not tuple(folder.glob("*.mealbackup"))

        for invalid in ("", str(tmp_path / "missing"), str(source)):
            dialog = BackupDirectoryDialog(session, settings)
            dialog.backup_directory_edit.setText(invalid)
            dialog._submit()
            assert dialog.result() != QDialog.DialogCode.Accepted
            assert dialog._error.isVisible() or not dialog._error.isHidden()
            assert session.backup_directory == folder.resolve()
        assert settings.backup_directory == folder.resolve()


def test_backup_folder_write_failure_keeps_previous_location(app, tmp_path, monkeypatch):
    source = tmp_path / "operations.sqlite3"
    old_folder = tmp_path / "old"
    new_folder = tmp_path / "new"
    old_folder.mkdir()
    new_folder.mkdir()
    settings = _settings(tmp_path / "settings.ini")
    settings.save_paths(source, old_folder)
    original_create = DatabaseBackupService.create_backup

    def refuse_new(self, *args, **kwargs):
        if self.directory == new_folder.resolve():
            raise PermissionError("access denied")
        return original_create(self, *args, **kwargs)

    monkeypatch.setattr(DatabaseBackupService, "create_backup", refuse_new)
    with DatabaseSession.create(source, "password", backup_directory=old_folder) as session:
        dialog = BackupDirectoryDialog(session, settings)
        dialog.backup_directory_edit.setText(str(new_folder))
        dialog._submit()
        assert dialog.result() != QDialog.DialogCode.Accepted
        assert "쓰기 권한" in dialog._error.text()
        assert session.backup_directory == old_folder.resolve()
        assert settings.backup_directory == old_folder.resolve()
        assert not tuple(new_folder.iterdir())


def test_backup_folder_settings_failure_rolls_back_without_switching(app, tmp_path):
    source = tmp_path / "operations.sqlite3"
    old_folder = tmp_path / "old"
    new_folder = tmp_path / "new"
    old_folder.mkdir()
    new_folder.mkdir()

    class FailingSettings:
        def __init__(self):
            self.values = {
                "storage/database_path": str(source.resolve()),
                "storage/backup_directory": str(old_folder.resolve()),
            }
            self.durable = self.values.copy()
            self.sync_count = 0

        def contains(self, key):
            return key in self.values

        def value(self, key, default=""):
            return self.values.get(key, default)

        def setValue(self, key, value):
            self.values[key] = value

        def remove(self, key):
            self.values.pop(key, None)

        def sync(self):
            self.sync_count += 1
            if self.sync_count != 1:
                self.durable = self.values.copy()

        def status(self):
            return QSettings.Status.AccessError if self.sync_count == 1 else QSettings.Status.NoError

    raw = FailingSettings()
    settings = DesktopStorageSettings(raw)
    with DatabaseSession.create(source, "password", backup_directory=old_folder) as session:
        dialog = BackupDirectoryDialog(session, settings)
        dialog.backup_directory_edit.setText(str(new_folder))
        dialog._submit()
        assert dialog.result() != QDialog.DialogCode.Accepted
        assert "설정" in dialog._error.text()
        assert settings.backup_directory == old_folder.resolve()
        assert raw.durable["storage/backup_directory"] == str(old_folder.resolve())
        assert settings.database_path == source.resolve()
        assert session.backup_directory == old_folder.resolve()
        session.backup_now()
        assert len(tuple(old_folder.glob("*.mealbackup"))) == 1


def test_operating_password_change_reopens_and_restores_new_backup(app, tmp_path):
    path = tmp_path / "operations.sqlite3"
    backups = tmp_path / "backups"
    backups.mkdir()
    with DatabaseSession.create(path, "old-password", backup_directory=backups) as session:
        session.append_revision(_record())
        expected = session.database.revision(REVISION_ID)
        dialog = DatabasePasswordChangeDialog(session)
        for edit in (dialog.current_password_edit, dialog.new_password_edit, dialog.confirm_password_edit):
            assert edit.echoMode() == QLineEdit.EchoMode.Password
        dialog.current_password_edit.setText("old-password")
        dialog.new_password_edit.setText("new-password")
        dialog.confirm_password_edit.setText("new-password")
        dialog._submit()
        assert dialog.result() == QDialog.DialogCode.Accepted
        assert dialog.password_changed
        assert dialog.backup_result.succeeded
        assert session.database.revision(REVISION_ID) == expected

    with pytest.raises(Exception):
        DatabaseSession.open(path, "old-password")
    with DatabaseSession.open(path, "new-password") as reopened:
        assert reopened.database.revision(REVISION_ID) == expected
    restored = tmp_path / "restored.sqlite3"
    with DatabaseSession.restore_and_open_with_user_password(
        dialog.backup_result.record.path, restored, "new-password"
    ) as recovered:
        assert recovered.database.revision(REVISION_ID) == expected


def test_operating_password_change_invalid_cancel_and_noop_leave_files_untouched(app, tmp_path):
    path = tmp_path / "operations.sqlite3"
    backups = tmp_path / "backups"
    backups.mkdir()
    with DatabaseSession.create(path, "current-password", backup_directory=backups) as session:
        before = credential_path_for(path).read_bytes()
        backup_count = len(tuple(backups.glob("*.mealbackup")))
        for current, new, confirmation in (
            ("wrong-password", "new-password", "new-password"),
            ("wrong-password", "wrong-password", "wrong-password"),
            ("current-password", "new-password", "mismatch"),
            ("", "new-password", "new-password"),
            ("current-password", "", ""),
        ):
            dialog = DatabasePasswordChangeDialog(session)
            dialog.current_password_edit.setText(current)
            dialog.new_password_edit.setText(new)
            dialog.confirm_password_edit.setText(confirmation)
            dialog._submit()
            assert dialog.result() != QDialog.DialogCode.Accepted
            assert dialog._error.isVisible() or dialog._error.text()
            assert credential_path_for(path).read_bytes() == before
        dialog = DatabasePasswordChangeDialog(session)
        dialog.reject()
        assert credential_path_for(path).read_bytes() == before
        dialog = DatabasePasswordChangeDialog(session)
        dialog.current_password_edit.setText("current-password")
        dialog.new_password_edit.setText("current-password")
        dialog.confirm_password_edit.setText("current-password")
        dialog._submit()
        assert dialog.result() == QDialog.DialogCode.Accepted
        assert not dialog.password_changed
        assert not dialog.backup_result.attempted
        assert credential_path_for(path).read_bytes() == before
        assert len(tuple(backups.glob("*.mealbackup"))) == backup_count


def test_operating_password_backup_failure_still_changes_password(app, tmp_path, monkeypatch):
    path = tmp_path / "operations.sqlite3"
    with DatabaseSession.create(path, "old-password", backup_directory=tmp_path / "missing-backups") as session:
        dialog = DatabasePasswordChangeDialog(session)
        dialog.current_password_edit.setText("old-password")
        dialog.new_password_edit.setText("new-password")
        dialog.confirm_password_edit.setText("new-password")
        dialog._submit()
        assert dialog.result() == QDialog.DialogCode.Accepted
        assert dialog.password_changed
        assert isinstance(dialog.backup_result.error, OSError)
    with DatabaseSession.open(path, "new-password"):
        pass


def test_location_change_cancel_wrong_password_and_conflict_keep_old_path(app, tmp_path):
    source = tmp_path / "operations.sqlite3"
    destination = tmp_path / "chosen.sqlite3"
    backups = tmp_path / "backups"
    backups.mkdir()
    settings = _settings(tmp_path / "settings.ini")
    settings.save_paths(source, backups)
    with DatabaseSession.create(source, "correct-password", backup_directory=backups) as session:
        dialog = DatabaseLocationDialog(session, settings)
        dialog.destination_path_edit.setText(str(destination))
        dialog.reject()
        assert settings.database_path == source.resolve()
        assert not destination.exists()

        dialog = DatabaseLocationDialog(session, settings)
        dialog.destination_path_edit.setText(str(destination))
        dialog.password_edit.setText("wrong-password")
        dialog._submit()
        assert dialog.result() != QDialog.DialogCode.Accepted
        assert not destination.exists()
        assert settings.database_path == source.resolve()

        destination.write_bytes(b"existing file")
        with pytest.raises(FileExistsError):
            relocate_database(session, destination, "correct-password", settings)
        assert destination.read_bytes() == b"existing file"
        assert settings.database_path == source.resolve()


def test_location_setting_failure_restores_previous_path_and_removes_new_files(app, tmp_path):
    source = tmp_path / "operations.sqlite3"
    destination = tmp_path / "chosen.sqlite3"
    backups = tmp_path / "backups"
    backups.mkdir()

    class FailingSettings:
        def __init__(self):
            self.values = {"storage/database_path": str(source)}
            self.durable = self.values.copy()
            self.sync_count = 0

        def contains(self, key):
            return key in self.values

        def value(self, key, default=""):
            return self.values.get(key, default)

        def setValue(self, key, value):
            self.values[key] = value

        def remove(self, key):
            self.values.pop(key, None)

        def sync(self):
            self.sync_count += 1
            if self.sync_count != 1:
                self.durable = self.values.copy()

        def status(self):
            return QSettings.Status.AccessError if self.sync_count == 1 else QSettings.Status.NoError

    raw_settings = FailingSettings()
    settings = DesktopStorageSettings(raw_settings)
    with DatabaseSession.create(source, "correct-password", backup_directory=backups) as session:
        dialog = DatabaseLocationDialog(session, settings)
        assert dialog.current_path_edit.isReadOnly()
        assert dialog.current_path_edit.text() == str(source.resolve())
        dialog.destination_path_edit.setText(str(destination))
        dialog.password_edit.setText("correct-password")
        dialog._submit()
        assert dialog.result() != QDialog.DialogCode.Accepted
        assert settings.database_path == source.resolve()
        assert raw_settings.durable["storage/database_path"] == str(source)
        assert not destination.exists()
        assert not credential_path_for(destination).exists()
        session.append_revision(_record())
        assert session.database.revision(REVISION_ID).metadata.revision_id == REVISION_ID


def test_setup_uses_one_masked_password(app: QApplication):
    dialog = CreateDatabaseDialog()

    assert dialog.user_password_edit.echoMode() is QLineEdit.EchoMode.Password
    assert dialog.findChild(QLineEdit, "recoveryPasswordEdit") is None
    assert dialog.findChild(QLineEdit, "userPasswordConfirmationEdit") is None
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
    assert not any(button.text() == "비밀번호를 잊었어요" for button in dialog.findChildren(QPushButton))

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


def test_single_password_login_has_no_recovery_action(app: QApplication, tmp_path: Path):
    path = tmp_path / "single.sqlite3"
    DatabaseSession.create(path, "only-password").close()
    dialog = LoginDialog(path)
    assert dialog.findChild(QLineEdit, "loginPasswordEdit") is dialog.password_edit
    assert not any(button.text() == "비밀번호를 잊었어요" for button in dialog.findChildren(QPushButton))
    dialog.password_edit.setText("only-password")
    dialog._submit()
    assert dialog.result() == QDialog.DialogCode.Accepted
    dialog.session.close()


def test_single_password_restore_uses_only_saved_password(app: QApplication, tmp_path: Path):
    source = tmp_path / "source.sqlite3"
    backups = tmp_path / "backups"
    backups.mkdir()
    with DatabaseSession.create(source, "only-password", backup_directory=backups) as session:
        backup = session.backup_now()
    destination = tmp_path / "restored.sqlite3"
    dialog = RestoreBackupDialog(destination_database_path=destination)
    dialog.backup_path_edit.setText(str(backup.path))
    assert dialog.findChild(QLineEdit, "restoreNewPasswordEdit") is None
    assert not any("기관 복구" in button.text() for button in dialog.findChildren(QPushButton))
    dialog.password_edit.setText("only-password")
    dialog._submit()
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.session.credentials.recovery_slot is None
    dialog.session.close()


def test_legacy_two_password_backup_restores_with_everyday_password(
    app: QApplication,
    tmp_path: Path,
):
    source = tmp_path / "lost.sqlite3"
    backup_directory = tmp_path / "automatic-backups"
    backup_directory.mkdir()
    with DatabaseSession.create(
        source,
        "everyday-password",
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
    assert dialog.findChild(QLineEdit, "restoreNewPasswordEdit") is None
    dialog.password_edit.setText("everyday-password")
    dialog._submit()

    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.session is not None
    assert settings.database_path == restored.resolve()
    dialog.session.close()
    DatabaseSession.open(restored, "everyday-password").close()
