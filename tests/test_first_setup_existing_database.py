"""First-run reuse opens the chosen database without recreating its contents."""
from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QDialog

from src.desktop_v2.application import DesktopStartupController
from src.desktop_v2.database_access import CreateDatabaseDialog, LoginDialog
from src.desktop_v2.portable_settings import DesktopStorageSettings
from src.storage.database_credentials import credential_path_for
from src.storage.database_session import AutomaticBackupResult, DatabaseSession
from tests.test_database_backup import REVISION_ID
from tests.test_delivery_routing import base_roster, initialize


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def existing(tmp_path):
    path = tmp_path / "existing.sqlite3"
    with DatabaseSession.create(path, "existing-password") as session:
        initialize(session, replace(base_roster(), revision_id=REVISION_ID))
        identity = session.credentials.database_id
    backup = tmp_path / "backups"
    backup.mkdir()
    settings = DesktopStorageSettings(QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat))
    return path, backup, settings, identity


def fill_existing(dialog, path, backup, password="existing-password"):
    dialog.open_existing_radio.setChecked(True)
    dialog.database_path_edit.setText(str(path))
    dialog.backup_directory_edit.setText(str(backup))
    dialog.user_password_edit.setText(password)


def test_first_run_opens_existing_roster_without_import_and_remembers_login(app, existing):
    path, backup, settings, identity = existing

    class ReuseDialog(CreateDatabaseDialog):
        def exec(self):
            fill_existing(self, path, backup)
            self.submit_button.click()
            assert self.result() == QDialog.DialogCode.Accepted, self._error.text()
            return self.result()

    def unexpected_import(*args, **kwargs):
        pytest.fail("An existing roster must not enter initial Excel import")

    errors = []
    controller = DesktopStartupController(
        settings=settings, create_dialog_type=ReuseDialog,
        guided_controller_type=unexpected_import,
        message_box=SimpleNamespace(critical=lambda *args: errors.append(args),
                                    warning=lambda *args: errors.append(args)),
    )
    outcome = controller.start()
    try:
        assert outcome.ready, errors
        assert outcome.session.path == path.resolve()
        assert outcome.session.credentials.database_id == identity
        assert outcome.session.database.latest_revision().metadata.revision_id == REVISION_ID
        assert settings.database_path == path.resolve()
        assert settings.backup_directory == backup.resolve()
        assert len(tuple(backup.glob("*.mealreplica"))) == 1
    finally:
        outcome.close()
    login = controller._access_dialog(None)
    assert isinstance(login, LoginDialog)
    login.reject()
    assert "existing-password" not in Path(settings._settings.fileName()).read_text(encoding="utf-8")


@pytest.mark.parametrize("failure", ("password", "missing_credentials", "missing_database"))
def test_failed_existing_open_keeps_settings_and_does_not_create_data(app, existing, failure):
    path, backup, settings, _ = existing
    password = "wrong-password" if failure == "password" else "existing-password"
    if failure == "missing_credentials":
        credential_path_for(path).unlink()
    if failure == "missing_database":
        path = path.with_name("absent.sqlite3")
    before = path.read_bytes() if path.exists() else None
    dialog = CreateDatabaseDialog(settings=settings)
    fill_existing(dialog, path, backup, password)
    dialog._submit()
    assert dialog.result() != QDialog.DialogCode.Accepted
    assert dialog.session is None and settings.database_path is None
    assert settings.backup_directory is None
    assert (path.read_bytes() if path.exists() else None) == before
    assert not tuple(backup.glob("*.mealreplica"))
    if failure == "missing_credentials":
        assert "비밀번호 정보 파일" in dialog._error.text()
        assert "같은 폴더" in dialog._error.text()
    dialog.reject()


def test_existing_settings_write_failure_releases_database_and_preserves_revision(app, existing, monkeypatch):
    path, backup, settings, identity = existing
    def fail(*args):
        raise PermissionError("synthetic settings failure")
    monkeypatch.setattr(settings, "save_paths", fail)
    dialog = CreateDatabaseDialog(settings=settings)
    fill_existing(dialog, path, backup)
    dialog._submit()
    assert dialog.result() != QDialog.DialogCode.Accepted
    assert dialog.session is None and settings.database_path is None
    assert "경로" in dialog._error.text() and "쓰기 권한" in dialog._error.text()
    with DatabaseSession.open(path, "existing-password") as reopened:
        assert reopened.credentials.database_id == identity
        assert reopened.database.latest_revision().metadata.revision_id == REVISION_ID
    dialog.reject()


def test_existing_backup_failure_is_separate_from_successful_open(app, existing, monkeypatch):
    path, backup, settings, _ = existing
    monkeypatch.setattr(DatabaseSession, "try_backup_now",
                        lambda self: AutomaticBackupResult(error=PermissionError("backup denied"), notify_failure=True))
    dialog = CreateDatabaseDialog(settings=settings)
    fill_existing(dialog, path, backup)
    dialog._submit()
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.backup_result.warning_required
    assert settings.database_path == path.resolve()
    assert dialog.session.database.latest_revision().metadata.revision_id == REVISION_ID
    dialog.session.close()


def test_existing_file_picker_cancel_and_mode_switch_do_not_save(app, existing, monkeypatch):
    from src.desktop_v2.database_access import QFileDialog
    path, backup, settings, _ = existing
    dialog = CreateDatabaseDialog(settings=settings)
    fill_existing(dialog, path, backup)
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: ("", ""))
    monkeypatch.setattr(QFileDialog, "getSaveFileName",
                        lambda *args: pytest.fail("Existing mode must use an open-file picker"))
    dialog._browse_database()
    assert dialog.database_path_edit.text() == str(path)
    dialog.create_new_radio.setChecked(True)
    assert dialog.database_path_edit.text() == dialog.user_password_edit.text() == ""
    assert dialog.submit_button.text() == "설정 완료하고 명단 옮기기"
    dialog.reject()
    assert settings.database_path is None and settings.backup_directory is None


def test_create_mode_never_overwrites_an_existing_database(app, existing):
    path, backup, settings, identity = existing
    dialog = CreateDatabaseDialog(settings=settings)
    dialog.database_path_edit.setText(str(path))
    dialog.backup_directory_edit.setText(str(backup))
    dialog.user_password_edit.setText("new-password")
    dialog._submit()
    assert dialog.result() != QDialog.DialogCode.Accepted
    assert dialog.session is None and settings.database_path is None
    with DatabaseSession.open(path, "existing-password") as reopened:
        assert reopened.credentials.database_id == identity
        assert reopened.database.latest_revision().metadata.revision_id == REVISION_ID
    dialog.reject()
