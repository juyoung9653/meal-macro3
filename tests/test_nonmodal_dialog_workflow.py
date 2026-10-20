"""Normal Qt event-loop dialog lifetime and concurrent roster editing."""
from __future__ import annotations

import os
from dataclasses import replace
from datetime import date
from types import SimpleNamespace
from uuid import uuid4

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QDialog, QLineEdit, QWidget

from src.desktop_v2.application import DesktopStartupController
from src.desktop_v2.dialog_workflow import DialogRequest, dialog_workflow, launch
from src.desktop_v2.person_edit import PersonProfileEditDialog
from src.domain.roster import Person, PersonProfile
from src.storage.database_session import DatabaseSession
from tests.test_delivery_routing import base_roster, initialize


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_launch_returns_with_editable_main_and_accepts_once(app):
    main = QWidget()
    editor = QLineEdit(main)
    main.show()
    calls = []
    dialogs = []

    @dialog_workflow
    def work(parent):
        dialog = QDialog(parent)
        dialog.setModal(True)
        dialogs.append(dialog)
        if (yield DialogRequest(dialog)) == QDialog.DialogCode.Accepted:
            calls.append(editor.text())

    run = launch(work, main)
    dialog = dialogs[0]
    assert dialog.isVisible()
    assert dialog.windowModality() == Qt.WindowModality.NonModal
    assert QApplication.activeModalWidget() is None
    assert dialog.parentWidget() is main
    assert dialog in main.findChildren(QDialog)
    assert dialog.windowHandle().transientParent() is None
    editor.setFocus()
    editor.setText("main edit while dialog remains open")
    assert dialog.isVisible() and editor.isEnabled()
    dialog.accept()
    dialog.accept()
    assert calls == []  # Qt accept() must finish before any save continuation.
    app.processEvents()
    assert calls == ["main edit while dialog remains open"]
    assert run.closed and not main._dialog_workflows
    main.close()


def test_main_close_cancels_before_cleanup_and_can_reopen(app):
    events = []

    class Main(QWidget):
        def closeEvent(self, event):
            events.append(("cleanup", not self._dialog_workflows))
            super().closeEvent(event)

    main = Main()
    main.show()
    dialogs = []

    @dialog_workflow
    def work(parent):
        dialog = QDialog(parent)
        dialogs.append(dialog)
        try:
            if (yield DialogRequest(dialog)) == QDialog.DialogCode.Accepted:
                events.append(("saved", True))
        finally:
            events.append(("cancelled", True))

    run = launch(work, main)
    main.close()
    assert run.closed
    assert events == [("cancelled", True), ("cleanup", True)]
    assert not dialogs[0].isVisible()
    dialogs[0].accept()
    assert ("saved", True) not in events
    main.show()
    second = launch(work, main)
    dialogs[1].accept()
    app.processEvents()
    assert second.closed and ("saved", True) in events
    main.close()
    app.processEvents()


def test_failure_is_observable_and_finishes_workflow(app, monkeypatch):
    from src.desktop_v2 import dialog_workflow as module
    notices = []
    monkeypatch.setattr(module.QMessageBox, "critical", lambda *args: notices.append(args))
    main = QWidget()
    dialogs = []

    @dialog_workflow
    def work(parent):
        dialog = QDialog(parent)
        dialogs.append(dialog)
        yield DialogRequest(dialog)
        raise OSError("synthetic storage failure")

    run = launch(work, main)
    dialogs[0].accept()
    app.processEvents()
    assert isinstance(run.error, OSError)
    assert run.closed and len(notices) == 1
    assert not main._dialog_workflows
    main.close()


@pytest.fixture
def operating_window(app, tmp_path):
    session = DatabaseSession.create(tmp_path / "synthetic.sqlite3", "password", "recovery")
    roster = base_roster()
    second = Person(uuid4(), PersonProfile("second synthetic person"))
    roster = replace(roster, persons=(*roster.persons, second))
    initialize(session, roster)
    controller = DesktopStartupController(settings=SimpleNamespace())
    access = SimpleNamespace(exec=lambda: QDialog.DialogCode.Accepted, session=session)
    outcome = controller._start_with_dialog(access, None)
    window = outcome.window
    assert window is not None
    window.show()
    yield session, window, roster
    outcome.close()


def _open_person(window, person_id):
    before = set(window.findChildren(PersonProfileEditDialog))
    window.person_edit_requested.emit(person_id)
    QApplication.processEvents()
    dialog = next(item for item in window.findChildren(PersonProfileEditDialog) if item not in before)
    assert dialog.isVisible() and not dialog.isModal()
    assert QApplication.activeModalWidget() is None
    return dialog


def test_real_person_dialog_keeps_target_and_main_date_editable(app, operating_window):
    session, window, roster = operating_window
    first, second = roster.persons
    dialog = _open_person(window, first.person_id)
    dialog._editors["name"].setText("saved synthetic name")
    window._set_day(date(2026, 12, 3))
    window.restore_person_cell((second.person_id, "name"))
    assert dialog.isVisible() and window.isEnabled()
    dialog._save_button.click()
    app.processEvents()
    current = session.database.current_roster()
    assert current.person(first.person_id).profile.name == "saved synthetic name"
    assert current.person(second.person_id) == second
    assert window.table_model.service_day == date(2026, 12, 3)
    assert not window._dialog_workflows


def test_real_person_cancel_leaves_revision_unchanged(app, operating_window):
    session, window, roster = operating_window
    dialog = _open_person(window, roster.persons[0].person_id)
    dialog._editors["name"].setText("cancelled draft")
    dialog.reject()
    app.processEvents()
    assert session.database.current_roster().revision_id == roster.revision_id
    assert not window._dialog_workflows


def test_stale_person_draft_preserved_without_overwriting_main_edit(app, operating_window, monkeypatch):
    from src.desktop_v2 import dialog_workflow as module
    notices = []
    monkeypatch.setattr(module.QMessageBox, "warning", lambda *args: notices.append(args))
    session, window, roster = operating_window
    person = roster.persons[0]
    dialog = _open_person(window, person.person_id)
    dialog._editors["name"].setText("older draft name")
    window.cell_edit_requested.emit(person.person_id, "note", "new main note")
    current = session.database.current_roster()
    assert current.person(person.person_id).profile.note == "new main note"
    dialog.windowHandle().setTransientParent(window.windowHandle())
    assert dialog.windowHandle().transientParent() is window.windowHandle()
    dialog._save_button.click()
    app.processEvents()
    assert len(notices) == 1 and dialog.isVisible()
    assert dialog.windowHandle().transientParent() is None
    assert dialog._editors["name"].text() == "older draft name"
    assert session.database.current_roster().revision_id == current.revision_id
    assert session.database.current_roster().person(person.person_id).profile.name == person.profile.name
    dialog.reject()
    fresh = _open_person(window, person.person_id)
    fresh._editors["name"].setText("fresh draft name")
    fresh._save_button.click()
    app.processEvents()
    saved = session.database.current_roster().person(person.person_id).profile
    assert saved.name == "fresh draft name" and saved.note == "new main note"


def test_driver_registry_saves_refresh_main_and_cancel_preserves_newer_main_edit(app, operating_window):
    from src.desktop_v2.delivery_routing import DriverRegistryDialog
    session, window, roster = operating_window
    window.drivers_requested.emit()
    first = next(item for item in window.findChildren(DriverRegistryDialog) if item.isVisible())
    first.name.setText("synthetic registered driver")
    first.add_button.click()
    app.processEvents()
    saved_revision = session.database.current_roster().revision_id
    assert window.roster.revision_id == saved_revision
    assert any(driver.name == "synthetic registered driver" for driver in window._routing_state.drivers)
    second = next(item for item in window.findChildren(DriverRegistryDialog) if item.isVisible())
    window.cell_edit_requested.emit(roster.persons[0].person_id, "note", "newer inline edit")
    current_revision = window.roster.revision_id
    assert current_revision != saved_revision
    second.reject()
    app.processEvents()
    assert window.roster.revision_id == current_revision
    assert window.roster.person(roster.persons[0].person_id).profile.note == "newer inline edit"


def test_nested_dialog_closes_when_its_editor_finishes(app):
    main = QWidget()
    editor = QDialog(main)
    editor.show()
    dialogs, calls = [], []

    @dialog_workflow
    def child(parent):
        dialog = QDialog(parent)
        dialogs.append(dialog)
        yield DialogRequest(dialog)
        calls.append("resumed")

    run = launch(child, editor)
    editor.accept()
    assert run.closed and not dialogs[0].isVisible()
    dialogs[0].accept()
    assert calls == []
    main.close()


def test_main_close_discards_unsaved_editor_before_session_reopen(app, tmp_path):
    path = tmp_path / "reopen.sqlite3"
    session = DatabaseSession.create(path, "password", "recovery")
    roster = base_roster()
    initialize(session, roster)
    controller = DesktopStartupController(settings=SimpleNamespace())
    access = SimpleNamespace(exec=lambda: QDialog.DialogCode.Accepted, session=session)
    outcome = controller._start_with_dialog(access, None)
    window = outcome.window
    window.show()
    dialog = _open_person(window, roster.persons[0].person_id)
    dialog._editors["name"].setText("unsaved before closing main")
    outcome.close()
    assert not dialog.isVisible() and not window._dialog_workflows
    with DatabaseSession.open(path, "password") as reopened:
        current = reopened.database.current_roster()
        assert current.revision_id == roster.revision_id
        assert current.person(roster.persons[0].person_id) == roster.persons[0]


def test_two_editor_accepts_serialize_before_stale_revision_check(app, operating_window, monkeypatch):
    import time
    from src.desktop_v2 import dialog_workflow as module
    session, window, roster = operating_window
    person_id = roster.persons[0].person_id
    first, second = _open_person(window, person_id), _open_person(window, person_id)
    first._editors["name"].setText("first accepted draft")
    second._editors["name"].setText("second older draft")
    warnings, errors, saves = [], [], []
    monkeypatch.setattr(module.QMessageBox, "warning", lambda *args: warnings.append(args))
    monkeypatch.setattr(module.QMessageBox, "critical", lambda *args: errors.append(args))
    append = session.append_revision

    def slow_append(record):
        saves.append(record.metadata.revision_id)
        time.sleep(0.03)
        return append(record)

    monkeypatch.setattr(session, "append_revision", slow_append)
    first._save_button.click()
    second._save_button.click()
    app.processEvents()
    assert len(saves) == 1 and not errors
    assert len(warnings) == 1 and second.isVisible()
    assert session.database.current_roster().person(person_id).profile.name == "first accepted draft"
    second.reject()
    app.processEvents()
