"""Qt review, cancellation and commit semantics for initial coverage correction."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from dataclasses import replace
from datetime import date
from types import SimpleNamespace
from uuid import uuid4

import pytest
from PySide6.QtCore import QDate, QSettings
from PySide6.QtWidgets import QApplication, QDialog, QWidget

from src.desktop_v2.application import DesktopStartupController
from src.desktop_v2.initial_roster_date import InitialRosterDateController, InitialRosterDateDialog
from src.desktop_v2.import_controller import GuidedRosterImportController
from src.desktop_v2.import_wizard import RosterImportWizard
from src.desktop_v2.settings_hub import SettingsAction
from src.services.initial_roster_date import load_initial_roster_date_context
from src.storage.database_session import AutomaticBackupResult, DatabaseSession
from tests.test_initial_roster_date import ACTOR, FIRST, START, append_roster, imported_session


@pytest.fixture
def app(tmp_path):
    application = QApplication.instance() or QApplication([])
    original_format = QSettings.defaultFormat()
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / 'settings'))
    yield application
    QSettings.setDefaultFormat(original_format)


class Messages:
    def __init__(self):
        self.errors, self.warnings = [], []
    def critical(self, _parent, title, text):
        self.errors.append((title, text))
    def warning(self, _parent, title, text):
        self.warnings.append((title, text))


def dialog_type(accepted=True, hook=None):
    class Dialog:
        def __init__(self, context, parent=None):
            self.context = context
        def exec(self):
            if hook:
                hook()
            return QDialog.DialogCode.Accepted if accepted else QDialog.DialogCode.Rejected
        def effective_on(self):
            return FIRST
    return Dialog


def test_dialog_previews_date_count_and_disables_no_change(app, tmp_path):
    session, _ = imported_session(tmp_path)
    try:
        context = load_initial_roster_date_context(session.database)
        dialog = InitialRosterDateDialog(context)
        assert dialog.effective_on() == FIRST
        assert '2명' in dialog.preview_label.text()
        assert dialog.save_button.isEnabled()
        dialog.date_edit.setDate(QDate(START.year, START.month, START.day))
        assert not dialog.save_button.isEnabled()
        dialog.close()
    finally:
        session.close()


@pytest.mark.parametrize('accepted', [False, True])
def test_controller_cancel_save_and_repeat(app, tmp_path, accepted):
    session, initial = imported_session(tmp_path)
    messages = Messages()
    try:
        controller = InitialRosterDateController(session, dialog_type=dialog_type(accepted), message_box=messages, actor_provider=lambda: ACTOR)
        outcome = controller.run()
        assert outcome.completed is accepted
        if accepted:
            assert outcome.roster.revision_id == session.database.latest_revision().metadata.revision_id
            assert outcome.roster.meals_for(outcome.roster.persons[0].person_id, on=FIRST)
            revision_id = outcome.roster.revision_id
            assert not controller.run().completed
            assert session.database.latest_revision().metadata.revision_id == revision_id
        else:
            assert session.database.latest_revision().metadata.revision_id == initial.metadata.revision_id
        assert messages.errors == []
    finally:
        session.close()


def test_controller_save_failure_and_backup_only_failure(app, tmp_path, monkeypatch):
    session, initial = imported_session(tmp_path)
    messages = Messages()
    try:
        original_append = session.append_revision
        def failure(record):
            raise OSError('save failed')
        monkeypatch.setattr(session, 'append_revision', failure)
        controller = InitialRosterDateController(session, dialog_type=dialog_type(), message_box=messages, actor_provider=lambda: ACTOR)
        assert not controller.run().completed
        assert session.database.latest_revision().metadata.revision_id == initial.metadata.revision_id
        assert len(messages.errors) == 1
        def backup_failure(record):
            original_append(record)
            return AutomaticBackupResult(error=OSError('backup failed'))
        monkeypatch.setattr(session, 'append_revision', backup_failure)
        assert controller.run().completed
        assert len(messages.warnings) == 1
        assert '저장됐지만' in messages.warnings[0][0]
    finally:
        session.close()


def test_stale_preview_does_not_overwrite_new_revision(app, tmp_path):
    session, initial = imported_session(tmp_path)
    messages = Messages()
    try:
        def concurrent_change():
            roster = session.database.current_roster()
            person = roster.persons[0]
            changed = replace(person, profile=replace(person.profile, note='new edit'))
            append_roster(session, replace(roster, persons=(changed,) + roster.persons[1:]))
        controller = InitialRosterDateController(session, dialog_type=dialog_type(hook=concurrent_change),
                                                message_box=messages, actor_provider=lambda: ACTOR)
        assert not controller.run().completed
        assert messages.errors
        roster = session.database.current_roster()
        assert roster.persons[0].profile.note == 'new edit'
        assert roster.meals_for(roster.persons[0].person_id, on=FIRST) == ()
    finally:
        session.close()


def test_settings_action_routes_and_refreshes_only_completed_result(monkeypatch):
    session, window, roster = object(), object(), object()
    calls = []
    class Hub:
        selected_action = SettingsAction.INITIAL_ROSTER_DATE
        def __init__(self, parent=None):
            assert parent is window
        def exec(self):
            return QDialog.DialogCode.Accepted
    class Controller:
        def __init__(self, value):
            assert value is session
        def run(self, parent=None):
            assert parent is window
            return SimpleNamespace(completed=True, roster=roster)
    startup = DesktopStartupController(settings=object(), settings_hub_dialog_type=Hub, initial_roster_date_controller_type=Controller)
    monkeypatch.setattr(startup, '_replace_roster_if_changed', lambda *args: calls.append(args))
    startup._run_settings_hub(session, window)
    assert calls == [(session, window, roster)]


def test_first_import_defaults_once_from_actual_empty_db_and_later_import_keeps_today(app, tmp_path, monkeypatch):
    import src.desktop_v2.import_wizard as module
    class OctoberDate(QDate):
        @staticmethod
        def currentDate():
            return QDate(2026, 10, 19)
    monkeypatch.setattr(module, 'QDate', OctoberDate)
    observed = []
    class Wizard(RosterImportWizard):
        def exec(self):
            observed.append(self.effective_date_edit.date().toPython())
            if len(observed) == 1:
                self.effective_date_edit.setDate(QDate(2026, 10, 7))
                self.set_initial_import_defaults()
                assert self.effective_date_edit.date().toPython() == date(2026, 10, 7)
            return QDialog.DialogCode.Rejected
    with DatabaseSession.create(tmp_path / 'empty.sqlite3', 'test-password') as empty:
        GuidedRosterImportController(empty, wizard_type=Wizard).run()
    folder = tmp_path / 'existing'
    folder.mkdir()
    session, _ = imported_session(folder)
    try:
        GuidedRosterImportController(session, wizard_type=Wizard).run()
    finally:
        session.close()
    assert observed == [FIRST, START]
