"""Portable settings and real OS handle lifetime contracts; all state is temporary."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QDialog

from src.desktop_v2.portable_settings import DesktopStorageSettings, application_directory, application_lock
from src.desktop_v2.database_access import LoginDialog, CreateDatabaseDialog, RestoreBackupDialog, relocate_database
from src.desktop_v2.application import DesktopStartupController, DesktopStartupOutcome, StartupStatus
from src.storage.database_session import DatabaseSession
from src.storage.database_credentials import credential_path_for
from src.storage.database_backup import restore_backup_with_user_password
from src.storage.file_lock import ExclusiveFileLock, FileLockError, database_lock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def app():
    yield QApplication.instance() or QApplication([])


def raw_settings(path):
    return QSettings(str(path), QSettings.Format.IniFormat)


@pytest.fixture
def ini_settings_only(monkeypatch):
    import src.desktop_v2.portable_settings as module

    class IniSettingsOnly:
        Format, Status = QSettings.Format, QSettings.Status

        def __new__(cls, *args):
            assert len(args) == 2 and args[1] == QSettings.Format.IniFormat, "Native settings must never be read"
            return QSettings(*args)

    monkeypatch.setattr(module, "QSettings", IniSettingsOnly)


def test_portable_paths_are_explicit_and_relative(tmp_path, monkeypatch, ini_settings_only):
    base = tmp_path / "program"
    with application_lock(base) as lock:
        settings = DesktopStorageSettings(base_directory=base, app_lock=lock)
        assert settings.database_path is None
        assert settings.backup_directory is None
        assert settings.document_output_directory is None
        settings.save_paths(base / "records" / "db.sqlite3", tmp_path / "external-backups")
        settings.save_document_output_directory(base / "output")
        stored = raw_settings(base / "data" / "settings.ini")
        assert stored.value("storage/database_path") == "records/db.sqlite3"
        assert stored.value("documents/output_directory") == "output"
        assert stored.value("storage/backup_directory") == str(tmp_path / "external-backups")
        monkeypatch.chdir(tmp_path)
        reopened = DesktopStorageSettings(base_directory=base, app_lock=lock)
        assert reopened.database_path == base / "records" / "db.sqlite3"
        assert reopened.backup_directory == tmp_path / "external-backups"
        assert reopened.document_output_directory == base / "output"
        assert not (base / "records").exists()
        assert not (base / "output").exists()


@pytest.mark.parametrize("contents", ["", "[portable]\ninitialized=true\n", "[storage]\ndatabase_path=\n"])
def test_existing_ini_is_authoritative_even_empty(tmp_path, contents, ini_settings_only):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "settings.ini").write_text(contents, encoding="utf-8")
    settings = DesktopStorageSettings(base_directory=tmp_path)
    assert settings.database_path is None
    assert (tmp_path / "data" / "settings.ini").read_text(encoding="utf-8") == contents


@pytest.mark.parametrize("reset", ["delete_ini", "new_folder"])
def test_reset_or_relocation_without_ini_starts_fresh(tmp_path, reset, ini_settings_only):
    base = tmp_path / "program"
    with application_lock(base) as lock:
        settings = DesktopStorageSettings(base_directory=base, app_lock=lock)
        settings.save_paths(tmp_path / "chosen.sqlite3", tmp_path / "backups")
        settings.save_document_output_directory(tmp_path / "output")
    del settings
    if reset == "delete_ini":
        (base / "data" / "settings.ini").unlink()
    else:
        base = tmp_path / "relocated"
    with application_lock(base) as lock:
        settings = DesktopStorageSettings(base_directory=base, app_lock=lock)
        assert settings.database_path is None
        assert settings.backup_directory is None
        assert settings.document_output_directory is None
        # The actual startup decision must offer first setup, not the old login.
        marker = object()
        controller = DesktopStartupController(settings=settings, create_dialog_type=lambda **kwargs: marker)
        assert controller._access_dialog(None) is marker
    assert raw_settings(base / "data" / "settings.ini").contains("portable/initialized")
    assert DesktopStorageSettings(base_directory=base).database_path is None


def test_initialization_requires_matching_live_app_lock(tmp_path):
    with pytest.raises(OSError):
        DesktopStorageSettings(base_directory=tmp_path)
    assert not (tmp_path / "data" / "settings.ini").exists()


def test_frozen_base_uses_executable_not_extraction_or_cwd(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "portable" / "meal.exe"))
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "extracted"), raising=False)
    monkeypatch.chdir(tmp_path)
    assert application_directory() == tmp_path / "portable"


class FailingSettings:
    def __init__(self):
        self.values = {"storage/database_path": "old.sqlite3", "storage/backup_directory": "old-backups"}
        self.durable = self.values.copy()
        self.sync_count = 0
    def contains(self, key):
        return key in self.values
    def value(self, key, default=None):
        return self.values.get(key, default)
    def setValue(self, key, value):
        self.values[key] = value
    def remove(self, key):
        self.values.pop(key, None)
    def sync(self):
        self.sync_count += 1
        if self.sync_count > 1:
            self.durable = self.values.copy()
    def status(self):
        return QSettings.Status.AccessError if self.sync_count == 1 else QSettings.Status.NoError


@pytest.mark.parametrize("method,args", [
    ("save_paths", ("new.sqlite3", "backups")),
    ("save_database_path", ("new.sqlite3",)),
    ("save_backup_directory", ("backups",)),
    ("save_document_output_directory", ("output",)),
])
def test_every_shared_write_reports_failure_and_rolls_back(method, args):
    raw = FailingSettings()
    before = raw.values.copy()
    settings = DesktopStorageSettings(raw)
    with pytest.raises(OSError):
        getattr(settings, method)(*args)
    assert raw.values == before
    assert raw.durable == before


CHILD = """
import sys
from pathlib import Path
from src.storage.file_lock import ExclusiveFileLock
from src.desktop_v2.portable_settings import application_lock
with application_lock(Path(sys.argv[1])):
    with ExclusiveFileLock(sys.argv[2]):
        Path(sys.argv[3]).write_text('ready')
        sys.stdin.readline()
"""


@pytest.mark.parametrize("ending", ["normal", "terminate", "kill"])
def test_real_subprocess_contention_and_release(tmp_path, ending):
    db_lock = tmp_path / "shared.sqlite3.lock"
    ready = tmp_path / "ready"
    env = dict(os.environ, PYTHONPATH=str(REPO))
    child = subprocess.Popen([sys.executable, "-c", CHILD, str(tmp_path / "first-exe"), str(db_lock), str(ready)],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        deadline = time.monotonic() + 15
        while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.03)
        assert ready.exists(), child.communicate(timeout=2)
        with pytest.raises(FileLockError):
            application_lock(tmp_path / "first-exe").acquire()
        # Another executable folder can start, but it cannot access the same DB.
        with application_lock(tmp_path / "second-exe"):
            with pytest.raises(FileLockError):
                ExclusiveFileLock(db_lock).acquire()
        if ending == "normal":
            child.stdin.write(b"close\n")
            child.stdin.flush()
        else:
            getattr(child, ending)()
        child.wait(timeout=10)
        assert db_lock.exists()  # Leftover files are expected and harmless.
        with ExclusiveFileLock(db_lock), application_lock(tmp_path / "first-exe"):
            pass
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=10)


def test_lock_access_failure_does_not_fallback(tmp_path):
    with pytest.raises(FileLockError):
        ExclusiveFileLock(tmp_path / "missing" / "app.lock").acquire()
    assert not (tmp_path / "missing").exists()


def test_sessions_lock_before_password_checks_and_release_wrong_password(tmp_path):
    path = tmp_path / "db.sqlite3"
    with DatabaseSession.create(path, "secret"):
        with pytest.raises(FileLockError):
            DatabaseSession.open(path, "wrong")
        with pytest.raises(FileLockError):
            DatabaseSession.create(path, "secret")
    with pytest.raises(Exception):
        DatabaseSession.open(path, "wrong")
    with DatabaseSession.open(path, "secret") as session:
        session.close()
        session.close()
    with database_lock(path):
        pass


def test_login_reserves_during_wrong_password_and_cancel_releases(app, tmp_path):
    path = tmp_path / "db.sqlite3"
    DatabaseSession.create(path, "secret").close()
    dialog = LoginDialog(path)
    with pytest.raises(FileLockError):
        DatabaseSession.open(path, "secret")
    dialog.password_edit.setText("wrong")
    dialog._submit()
    assert dialog.session is None
    with pytest.raises(FileLockError):
        database_lock(path).acquire()
    dialog.reject()
    with DatabaseSession.open(path, "secret"):
        pass
    retry = LoginDialog(path)
    retry.password_edit.setText("secret")
    retry._submit()
    assert retry.session is not None
    with pytest.raises(FileLockError):
        database_lock(path).acquire()
    retry.session.close()


def test_constructor_failure_releases_database_handle(tmp_path, monkeypatch):
    path = tmp_path / "db.sqlite3"
    DatabaseSession.create(path, "secret").close()
    import src.storage.database_session as module
    def fail(*args, **kwargs):
        raise RuntimeError("backup initialization failure")
    monkeypatch.setattr(module, "DatabaseReplicaService", fail)
    with pytest.raises(RuntimeError):
        DatabaseSession.open(path, "secret", backup_directory=tmp_path)
    with DatabaseSession.open(path, "secret"):
        pass


def test_restore_contention_checked_before_archive_and_release_failure(tmp_path):
    path = tmp_path / "db.sqlite3"
    with database_lock(path):
        with pytest.raises(FileLockError):
            restore_backup_with_user_password(tmp_path / "absent.mealbackup", path, "secret")
        with pytest.raises(FileLockError):
            DatabaseSession.restore_and_open_with_user_password(tmp_path / "absent.mealbackup", path, "secret")
    with pytest.raises(Exception):
        DatabaseSession.restore_and_open_with_user_password(tmp_path / "absent.mealbackup", path, "secret")
    with database_lock(path):
        pass
    assert not path.exists()


def test_relocation_keeps_target_reserved_and_rolls_back_under_lock(tmp_path):
    path, target = tmp_path / "db.sqlite3", tmp_path / "new.sqlite3"
    backups = tmp_path / "backups"
    backups.mkdir()
    settings = DesktopStorageSettings(raw_settings(tmp_path / "paths.ini"))
    with DatabaseSession.create(path, "secret", backup_directory=backups) as session:
        with database_lock(target):
            with pytest.raises(FileLockError):
                relocate_database(session, target, "secret", settings)
        class FailSave:
            def save_database_path(self, value):
                with pytest.raises(FileLockError):
                    database_lock(value).acquire()
                raise OSError("cannot save")
        with pytest.raises(OSError):
            relocate_database(session, target, "secret", FailSave())
        assert not target.exists()
        assert not credential_path_for(target).exists()
        with database_lock(target):
            pass
        relocate_database(session, target, "secret", settings)
        with pytest.raises(FileLockError):
            DatabaseSession.open(target, "secret")
    with DatabaseSession.open(target, "secret"):
        pass


@pytest.mark.parametrize("stage", ["cancel", "exception"])
def test_startup_disposes_session_on_cancel_or_exception(tmp_path, stage):
    path = tmp_path / "db.sqlite3"
    session = DatabaseSession.create(path, "secret")
    class Access:
        def __init__(self, **kwargs):
            self.session = session
        def exec(self):
            if stage == "exception":
                raise RuntimeError("dialog failed")
            return QDialog.DialogCode.Rejected
    messages = []
    controller = DesktopStartupController(settings=SimpleNamespace(database_path=None),
        create_dialog_type=Access, message_box=SimpleNamespace(critical=lambda *a: messages.append(a)))
    outcome = controller.start()
    assert not outcome.ready
    assert bool(messages) == (stage == "exception")
    with database_lock(path):
        pass


@pytest.mark.parametrize("stage", ["cancel", "controller_error", "event_error", "busy"])
def test_main_keeps_app_lock_and_cleans_all_exits(app, tmp_path, monkeypatch, stage):
    import src.desktop_v2.__main__ as module
    import src.desktop_v2.output_retention as retention_module
    import src.desktop_v2.window_preferences as preferences_module
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "settings.ini").write_text("[portable]\ninitialized=true\n")
    monkeypatch.setattr(module, "application_lock", lambda: application_lock(tmp_path))
    monkeypatch.setattr(module, "DesktopStorageSettings", lambda **kwargs: DesktopStorageSettings(base_directory=tmp_path, **kwargs))
    messages = []
    monkeypatch.setattr(module, "QMessageBox", SimpleNamespace(critical=lambda *a: messages.append(a)))
    closed, executed, preferences, cleanup = [], [], [], []
    # These UI integrations own QObject parents and have separate coverage.
    # Keep this test focused on application-lock and session lifetimes.
    def install_preferences(application):
        assert application is fake_app
        preferences.append(application)
    def create_cleanup(settings, window):
        with pytest.raises(FileLockError):
            application_lock(tmp_path).acquire()
        cleanup.append(window)
        return object()
    monkeypatch.setattr(preferences_module, "install_window_preferences", install_preferences)
    monkeypatch.setattr(retention_module, "OutputCleanupScheduler", create_cleanup)
    class Controller:
        def __init__(self, settings):
            with pytest.raises(FileLockError):
                application_lock(tmp_path).acquire()
            if stage == "controller_error":
                raise RuntimeError("controller failed")
        def start(self):
            if stage == "cancel":
                return DesktopStartupOutcome(StartupStatus.CANCELLED)
            return DesktopStartupOutcome(StartupStatus.READY,
                session=SimpleNamespace(close=lambda: closed.append(True)),
                window=SimpleNamespace(show=lambda: None, close=lambda: None))
    monkeypatch.setattr(module, "DesktopStartupController", Controller)
    def execute():
        executed.append(True)
        with pytest.raises(FileLockError):
            application_lock(tmp_path).acquire()
        raise RuntimeError("event failed")
    fake_app = SimpleNamespace(exec=execute)
    monkeypatch.setattr(module, "QApplication", SimpleNamespace(instance=lambda: fake_app))
    if stage == "busy":
        with application_lock(tmp_path):
            assert module.main() == 1
            with pytest.raises(FileLockError):
                application_lock(tmp_path).acquire()
    else:
        assert module.main() == (0 if stage == "cancel" else 1)
    assert preferences == [fake_app]
    assert bool(executed) == bool(cleanup) == bool(closed) == (stage == "event_error")
    assert bool(messages) == (stage != "cancel")
    with application_lock(tmp_path):
        pass


def test_unavailable_configured_parent_fails_closed(tmp_path):
    settings = SimpleNamespace(database_path=tmp_path / "unavailable" / "db.sqlite3")
    controller = DesktopStartupController(settings=settings)
    with pytest.raises(FileNotFoundError):
        controller._access_dialog(None)


@pytest.mark.skipif(os.name != "nt", reason="Windows case-insensitive path alias")
def test_windows_aliases_share_one_database_lock(tmp_path):
    path = tmp_path / "Shared.sqlite3"
    with database_lock(path):
        with pytest.raises(FileLockError):
            database_lock(str(path).upper()).acquire()
        with pytest.raises(FileLockError):
            database_lock(path.parent / "." / path.name).acquire()


@pytest.mark.parametrize("kind", ["create", "restore"])
def test_dialog_settings_failure_reports_preserved_file_and_closes_session(app, tmp_path, kind):
    backups = tmp_path / "backups"
    backups.mkdir()
    target = tmp_path / "new.sqlite3"
    class FailSave:
        def save_paths(self, *args):
            raise OSError("save failed")
    if kind == "create":
        dialog = CreateDatabaseDialog(settings=FailSave())
        dialog.database_path_edit.setText(str(target))
        dialog.backup_directory_edit.setText(str(backups))
        dialog.user_password_edit.setText("secret")
    else:
        with DatabaseSession.create(tmp_path / "source.sqlite3", "secret", backup_directory=backups) as source:
            archive = source.backup_now().path
        dialog = RestoreBackupDialog(destination_database_path=target, backup_directory=backups, settings=FailSave())
        dialog.backup_path_edit.setText(str(archive))
        dialog.password_edit.setText("secret")
    dialog._submit()
    assert dialog.result() != QDialog.DialogCode.Accepted
    assert dialog.session is None
    assert not dialog._error.isHidden()
    assert target.is_file()
    assert credential_path_for(target).is_file()
    with DatabaseSession.open(target, "secret"):
        pass


def test_session_creation_failure_releases_lock_and_removes_partial_db(tmp_path):
    path = tmp_path / "new.sqlite3"
    with pytest.raises(Exception):
        DatabaseSession.create(path, "")
    assert not path.exists()
    assert not credential_path_for(path).exists()
    with DatabaseSession.create(path, "secret"):
        pass


@pytest.mark.parametrize("flow", ["settings", "wizard"])
def test_document_default_write_failure_is_shown_and_stops_dispatch(tmp_path, monkeypatch, flow):
    import src.desktop_v2.application as module
    from src.desktop_v2.document_hub import DocumentAction
    from src.desktop_v2.document_output import DocumentOutputMode
    from src.desktop_v2.settings_hub import SettingsAction
    messages, dispatched, save_attempts = [], [], []
    class Settings:
        document_output_directory = tmp_path
        def save_document_output_directory(self, directory):
            save_attempts.append(directory)
            raise OSError("settings unavailable")
    class Dialog:
        selected_action = DocumentAction.INVOICE
        selected_summary = object()
        selected_directory = tmp_path
        def __init__(self, *args, **kwargs):
            pass
        def exec(self):
            return QDialog.DialogCode.Accepted
        def selected_period(self):
            return object()
        def selection(self):
            return SimpleNamespace(mode=DocumentOutputMode.NEW_DEFAULT, directory=tmp_path)
    controller = DesktopStartupController(settings=Settings(), document_hub_dialog_type=Dialog,
        document_source_dialog_type=Dialog, document_period_dialog_type=Dialog,
        document_meal_dialog_type=Dialog, document_output_dialog_type=Dialog,
        settings_hub_dialog_type=Dialog, document_directory_dialog_type=Dialog,
        message_box=SimpleNamespace(critical=lambda *a: messages.append(a)))
    window = SimpleNamespace(table_model=SimpleNamespace(service_day=None, meal=None),
        document_output_selected=SimpleNamespace(emit=lambda value: dispatched.append(value)))
    session = SimpleNamespace(database=SimpleNamespace(
        revision_summaries=lambda: [SimpleNamespace(sequence=1)],
    ))
    if flow == "wizard":
        monkeypatch.setattr(controller, "_ensure_document_prerequisites", lambda *a: True)
        monkeypatch.setattr(module, "DocumentSetupRequest", lambda *a: object())
        controller._run_document_hub(session, window)
    else:
        Dialog.selected_action = SettingsAction.DOCUMENT_DIRECTORY
        controller._run_settings_hub(session, window)
    assert save_attempts == [tmp_path]
    assert len(messages) == 1
    assert messages[0][1:] == (
        "문서 저장 폴더를 저장하지 못했습니다",
        "프로그램이 자료를 처리하는 중 예상하지 못한 문제가 발생했습니다. 현재 저장 상태를 확인한 뒤 "
        "작업 창을 다시 열어 다시 시도해주세요. 같은 문제가 계속되면 수행한 작업과 이 안내를 프로그램 담당자에게 전달해주세요.",
    )
    assert not dispatched


@pytest.mark.parametrize("failure", ["sync", "publish"])
def test_failed_initialization_leaves_no_authoritative_ini_and_retries(tmp_path, monkeypatch, failure):
    import src.desktop_v2.portable_settings as module
    original_rename, original_link = module.os.rename, module.os.link
    failed = True
    class InitialSettings:
        Format, Status = QSettings.Format, QSettings.Status
        def __init__(self, *args):
            self.raw = QSettings(*args)
        def __getattr__(self, name):
            return getattr(self.raw, name)
        def status(self):
            if failed and failure == "sync":
                return QSettings.Status.AccessError
            return self.raw.status()
    def deny_publish(*args):
        raise PermissionError("initialization publish denied")
    monkeypatch.setattr(module, "QSettings", InitialSettings)
    if failure == "publish":
        monkeypatch.setattr(module.os, "rename", deny_publish)
        monkeypatch.setattr(module.os, "link", deny_publish)
    with application_lock(tmp_path) as lock:
        with pytest.raises(OSError):
            DesktopStorageSettings(base_directory=tmp_path, app_lock=lock)
        assert not (tmp_path / "data" / "settings.ini").exists()
        assert not list((tmp_path / "data").glob(".settings-*.ini"))
        failed = False
        monkeypatch.setattr(module.os, "rename", original_rename)
        monkeypatch.setattr(module.os, "link", original_link)
        settings = DesktopStorageSettings(base_directory=tmp_path, app_lock=lock)
        assert settings.database_path is None
        assert raw_settings(tmp_path / "data" / "settings.ini").contains("portable/initialized")
