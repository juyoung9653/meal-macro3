"""GUI confirmation and switch contracts for revision restoration."""
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox, QMessageBox

from src.desktop_v2.revision_restore import RevisionRestoreController, RevisionRestoreDialog, RevisionRestoreSelection
from src.services.revision_restore import RevisionRestoreResult
from src.storage.encrypted_database import RevisionConflictError
from src.storage.database_session import AutomaticBackupResult


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


class Messages:
    def __init__(self, answers=()):
        self.answers = iter(answers)
        self.events = []
    def question(self, *args):
        self.events.append(("question", args))
        return next(self.answers)
    def information(self, *args):
        self.events.append(("information", args))
    def warning(self, *args):
        self.events.append(("warning", args))
    def critical(self, *args):
        self.events.append(("critical", args))


def controller(choice, answers=(), *, accepted=True, save_error=False, backup=None):
    calls, restarts, saved = [], [], []
    head = uuid4()
    session = SimpleNamespace(path=Path("source.sqlite3"), database=SimpleNamespace(
        latest_revision=lambda: SimpleNamespace(metadata=SimpleNamespace(revision_id=head)),
        revision_display_code=lambda revision: "20261001-001"))
    class Dialog:
        def __init__(self, *args):
            pass
        def exec(self):
            return QDialog.DialogCode.Accepted if accepted else QDialog.DialogCode.Rejected
        def selection(self):
            return choice
    def save(path):
        if save_error:
            raise OSError("cannot save settings")
        saved.append(path)
    def branch(*args, **kwargs):
        calls.append(("branch", args, kwargs))
        return SimpleNamespace(revision_id=uuid4(), branch=SimpleNamespace(name=choice.name), backup=backup or AutomaticBackupResult())
    def restore(*args):
        calls.append(("restore", args))
        return RevisionRestoreResult(uuid4(), backup or AutomaticBackupResult())
    messages = Messages(answers)
    instance = RevisionRestoreController(session, SimpleNamespace(save_database_path=save), lambda: restarts.append(True),
        dialog_type=Dialog, message_box=messages, actor_provider=lambda: "actor", branch_service=branch, restore_service=restore)
    return instance, calls, saved, restarts, messages


def test_dialog_defaults_to_same_database_branch_without_file_or_password_fields(app):
    dialog = RevisionRestoreDialog(Path("source.sqlite3"), "20261001-001")
    assert dialog.branch_radio.isChecked()
    assert not hasattr(dialog, "path_edit")
    assert not hasattr(dialog, "password_edit")
    dialog.name_edit.clear()
    dialog._submit()
    assert dialog.result() != QDialog.DialogCode.Accepted
    assert dialog.error_label.text()
    dialog.name_edit.setText("  시험 분기  ")
    assert dialog.selection() == RevisionRestoreSelection(True, "시험 분기")
    dialog.restore_radio.setChecked(True)
    assert not dialog.selection().branch
    assert not dialog.branch_fields.isEnabled()
    dialog.close()


@pytest.mark.parametrize("accepted, answers", [(False, ()), (True, (QMessageBox.StandardButton.Cancel,))])
def test_cancel_at_either_stage_never_writes_or_switches(accepted, answers):
    instance, calls, saved, restarts, messages = controller(RevisionRestoreSelection(False), answers, accepted=accepted)
    instance.run(uuid4())
    assert not calls and not saved and not restarts


def test_create_branch_uses_same_session_and_restarts_without_path_change():
    choice = RevisionRestoreSelection(True, "분기")
    instance, calls, saved, restarts, messages = controller(choice, (QMessageBox.StandardButton.Yes,))
    revision_id = uuid4()
    instance.run(revision_id)
    assert calls[0][0] == "branch"
    assert calls[0][1][0] is instance.session
    assert calls[0][1][1] == revision_id
    assert len(calls[0][1]) == 4
    assert calls[0][2] == {"name": "분기"}
    assert not saved and restarts == [True]
    assert len([event for event in messages.events if event[0] == "question"]) == 1


@pytest.mark.parametrize("error", [OSError("secret payload"), RevisionConflictError("secret payload")])
def test_failed_create_keeps_window_and_shows_safe_observable_error(error):
    instance, calls, saved, restarts, messages = controller(
        RevisionRestoreSelection(True, "분기"), (QMessageBox.StandardButton.Yes,),
    )
    def fail(*_args, **_kwargs):
        raise error
    instance.branch_service = fail
    instance.run(uuid4())
    assert not calls and not saved and not restarts
    assert messages.events[-1][0] == ("warning" if isinstance(error, RevisionConflictError) else "critical")
    assert "secret payload" not in str(messages.events)


@pytest.mark.parametrize("answer", [QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Yes])
def test_checkout_keeps_database_path_and_guards_head(answer):
    instance, calls, saved, restarts, messages = controller(RevisionRestoreSelection(False), (answer,))
    branch_id = uuid4()
    def checkout(*args):
        calls.append(("checkout", args))
        return RevisionRestoreResult(uuid4(), AutomaticBackupResult())
    instance.checkout_service = checkout
    instance.checkout(branch_id, "main")
    assert not saved
    if answer == QMessageBox.StandardButton.Cancel:
        assert not calls and not restarts
    else:
        assert calls[0][1] == (instance.session, branch_id, instance._head(), "actor")
        assert restarts == [True]


@pytest.mark.parametrize("error", [OSError("secret payload"), RevisionConflictError("secret payload")])
def test_checkout_failure_does_not_restart_or_echo_payload(error):
    instance, calls, saved, restarts, messages = controller(RevisionRestoreSelection(False), (QMessageBox.StandardButton.Yes,))
    def fail(*_args):
        raise error
    instance.checkout_service = fail
    instance.checkout(uuid4(), "main")
    assert not saved and not restarts
    assert messages.events[-1][0] == ("warning" if isinstance(error, RevisionConflictError) else "critical")
    assert "secret payload" not in str(messages.events)


def test_restore_backup_failure_still_reports_committed_restore_and_restarts():
    instance, calls, saved, restarts, messages = controller(RevisionRestoreSelection(False), (QMessageBox.StandardButton.Yes,), backup=AutomaticBackupResult(error=OSError("backup offline")))
    instance.run(uuid4())
    assert calls[0][0] == "restore"
    assert not saved and restarts == [True]
    assert not any(event[0] == "warning" for event in messages.events)
    assert messages.events[-1][0] == "information"


def test_restore_no_change_never_restarts():
    instance, calls, saved, restarts, messages = controller(RevisionRestoreSelection(False), (QMessageBox.StandardButton.Yes,))
    instance.restore_service = lambda *args: RevisionRestoreResult(None, AutomaticBackupResult())
    instance.run(uuid4())
    assert not restarts and not saved
    assert messages.events[-1][0] == "information"


@pytest.mark.parametrize("branch", [False, True])
@pytest.mark.parametrize("fail", [False, True])
def test_restore_progress_keeps_ui_responsive_and_closes_before_result(app, branch, fail):
    from threading import Event, get_ident
    from PySide6.QtCore import QTimer
    from src.desktop_v2.startup_loading import StartupLoadingDialog

    choice = RevisionRestoreSelection(branch, "새 분기")
    instance, calls, saved, restarts, messages = controller(
        choice, (QMessageBox.StandardButton.Yes, QMessageBox.StandardButton.No),
    )
    original = instance.branch_service if branch else instance.restore_service
    ui_thread = get_ident()
    entered, released = Event(), Event()
    observations = []

    def service(*args, **kwargs):
        assert get_ident() != ui_thread
        entered.set()
        assert released.wait(5), "UI did not respond during restore"
        if fail:
            raise OSError("private stored payload")
        return original(*args, **kwargs)

    if branch:
        instance.branch_service = service
    else:
        instance.restore_service = service

    def heartbeat():
        if not entered.is_set() or released.is_set():
            return
        dialogs = [w for w in app.topLevelWidgets()
                   if isinstance(w, StartupLoadingDialog) and w.isVisible()]
        if dialogs:
            observations.append(get_ident())
            assert all(kind == "question" for kind, _ in messages.events)
            released.set()

    timer = QTimer()
    timer.timeout.connect(heartbeat)
    timer.start(10)
    try:
        instance.run(uuid4())
    finally:
        released.set()
        timer.stop()
    assert observations == [ui_thread]
    assert not any(isinstance(w, StartupLoadingDialog) and w.isVisible()
                   for w in app.topLevelWidgets())
    if fail:
        assert not calls and not saved and not restarts
        assert messages.events[-1][0] == "critical"
        assert "private stored payload" not in str(messages.events)
    else:
        assert len(calls) == 1
        assert restarts == [True]


@pytest.mark.parametrize("fail", [False, True])
def test_open_existing_checks_off_thread_and_releases_session(app, monkeypatch, fail):
    from threading import get_ident
    import src.desktop_v2.revision_restore as module

    ui_thread = get_ident()
    closed, checked = [], []

    class Opened:
        @property
        def database(self):
            return self
        def check_integrity(self):
            checked.append(get_ident())
            if fail:
                raise OSError("private payload")
        def close(self):
            closed.append(True)
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            self.close()

    def open_file(*_args):
        assert get_ident() != ui_thread
        return Opened()

    monkeypatch.setattr(module.DatabaseSession, "open", open_file)
    monkeypatch.setattr(module.QFileDialog, "getOpenFileName", lambda *_: ("other.sqlite3", ""))
    monkeypatch.setattr(module.QInputDialog, "getText", lambda *_: ("password", True))
    instance, calls, saved, restarts, messages = controller(
        RevisionRestoreSelection(False), (QMessageBox.StandardButton.Cancel,),
    )
    instance.open_existing()
    assert len(checked) == 1 and checked[0] != ui_thread
    assert closed == [True]
    assert not calls and not saved and not restarts
    assert messages.events[-1][0] == ("critical" if fail else "question")
    assert "private payload" not in str(messages.events)


@pytest.mark.parametrize("branch", [False, True])
def test_committed_branch_or_restore_backup_failure_still_restarts(branch):
    instance, calls, saved, restarts, messages = controller(
        RevisionRestoreSelection(branch, "검토"), (QMessageBox.StandardButton.Yes,),
        backup=AutomaticBackupResult(error=OSError("backup offline")),
    )
    instance.run(uuid4())
    assert restarts == [True] and not saved
    assert [kind for kind, _ in messages.events] == ["question", "information"]


def test_checkout_no_change_never_restarts():
    instance, calls, saved, restarts, messages = controller(RevisionRestoreSelection(False), (QMessageBox.StandardButton.Yes,))
    instance.checkout_service = lambda *_: RevisionRestoreResult(None, AutomaticBackupResult())
    instance.checkout(uuid4(), "main")
    assert not restarts and not saved
    assert messages.events[-1][0] == "information"
