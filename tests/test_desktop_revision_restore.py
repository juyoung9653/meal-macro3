"""GUI confirmation and switch contracts for revision restoration."""
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox, QMessageBox

from src.desktop_v2.revision_restore import RevisionRestoreController, RevisionRestoreDialog, RevisionRestoreSelection
from src.services.revision_restore import RevisionRestoreResult, RevisionBranchResult
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
        return RevisionBranchResult(choice.destination, uuid4(), uuid4(), backup or AutomaticBackupResult())
    def restore(*args):
        calls.append(("restore", args))
        return RevisionRestoreResult(uuid4(), backup or AutomaticBackupResult())
    messages = Messages(answers)
    instance = RevisionRestoreController(session, SimpleNamespace(save_database_path=save), lambda: restarts.append(True),
        dialog_type=Dialog, message_box=messages, actor_provider=lambda: "actor", branch_service=branch, restore_service=restore)
    return instance, calls, saved, restarts, messages


def test_dialog_defaults_to_branch_masks_password_and_validates_confirmation(app):
    dialog = RevisionRestoreDialog(Path("source.sqlite3"), "20261001-001")
    assert dialog.branch_radio.isChecked()
    assert dialog.password_edit.echoMode() == dialog.password_edit.EchoMode.Password
    dialog.password_edit.setText("password")
    dialog.confirm_edit.setText("wrong")
    dialog._submit()
    assert dialog.result() != QDialog.DialogCode.Accepted
    assert dialog.error_label.text()
    dialog.confirm_edit.setText("password")
    assert dialog.selection().branch
    dialog.restore_radio.setChecked(True)
    assert not dialog.selection().branch
    assert not dialog.branch_fields.isEnabled()
    dialog.close()


@pytest.mark.parametrize("accepted, answers", [(False, ()), (True, (QMessageBox.StandardButton.Cancel,))])
def test_cancel_at_either_stage_never_writes_or_switches(accepted, answers):
    instance, calls, saved, restarts, messages = controller(RevisionRestoreSelection(False), answers, accepted=accepted)
    instance.run(uuid4())
    assert not calls and not saved and not restarts


def test_create_branch_keeps_current_file_until_user_chooses_switch():
    choice = RevisionRestoreSelection(True, Path("branch.sqlite3"), "password", "분기")
    instance, calls, saved, restarts, messages = controller(choice, (QMessageBox.StandardButton.Yes, QMessageBox.StandardButton.No))
    instance.run(uuid4())
    assert calls[0][0] == "branch"
    assert not saved and not restarts


def test_branch_switch_saves_verified_path_then_restarts():
    choice = RevisionRestoreSelection(True, Path("branch.sqlite3"), "password", "분기")
    instance, calls, saved, restarts, messages = controller(choice, (QMessageBox.StandardButton.Yes, QMessageBox.StandardButton.Yes))
    instance.run(uuid4())
    assert saved == [choice.destination]
    assert restarts == [True]


def test_failed_switch_keeps_created_branch_and_current_window():
    choice = RevisionRestoreSelection(True, Path("branch.sqlite3"), "password", "분기")
    instance, calls, saved, restarts, messages = controller(choice, (QMessageBox.StandardButton.Yes, QMessageBox.StandardButton.Yes), save_error=True)
    instance.run(uuid4())
    assert calls[0][0] == "branch"
    assert not saved and not restarts
    assert messages.events[-1][0] == "warning"


def test_restore_backup_failure_still_reports_committed_restore_and_restarts():
    instance, calls, saved, restarts, messages = controller(RevisionRestoreSelection(False), (QMessageBox.StandardButton.Yes,), backup=AutomaticBackupResult(error=OSError("backup offline")))
    instance.run(uuid4())
    assert calls[0][0] == "restore"
    assert not saved and restarts == [True]
    assert any(event[0] == "warning" for event in messages.events)
    assert messages.events[-1][0] == "information"


def test_restore_no_change_never_restarts():
    instance, calls, saved, restarts, messages = controller(RevisionRestoreSelection(False), (QMessageBox.StandardButton.Yes,))
    instance.restore_service = lambda *args: RevisionRestoreResult(None, AutomaticBackupResult())
    instance.run(uuid4())
    assert not restarts and not saved
    assert messages.events[-1][0] == "information"
