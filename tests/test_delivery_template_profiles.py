"""Settings entry, profile isolation, legacy compatibility and generation routing."""
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from shutil import copyfile
from uuid import uuid4

import openpyxl
import pytest
from PySide6.QtCore import QSettings, QTimer
from PySide6.QtWidgets import QApplication, QDialog

from src.desktop_v2 import delivery_template, document_template
from src.desktop_v2.delivery_template import DeliveryTemplateDialog, DeliveryTemplateStore
from src.desktop_v2.document_template import DocumentTemplateDialog, DocumentTemplateController
from src.desktop_v2.document_generation import DocumentGenerationController
from src.desktop_v2.document_hub import DocumentAction
from src.desktop_v2.document_output import DocumentOutputDialog, next_available_document_output
from src.domain.document_templates import DocumentTemplateProfile as Profile, build_document_template_revision
from src.excel.delivery_templates import create_substitute_template
from src.excel.publication import publish_workbook
from tests.test_delivery_substitute_v2 import session, selection_for, Messages, ACTOR


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def preferences(tmp_path, monkeypatch):
    settings = QSettings(str(tmp_path / "prefs.ini"), QSettings.Format.IniFormat)
    def factory(database_id, *, profile=Profile.SCHOOL_MEAL):
        return DeliveryTemplateStore(database_id, profile=profile, settings=settings, root=tmp_path / "copies" / profile.value)
    monkeypatch.setattr(delivery_template, "DeliveryTemplateStore", factory)
    monkeypatch.setattr(document_template, "DeliveryTemplateStore", factory)
    return factory


def template_file(tmp_path, action):
    path = tmp_path / (action.value + ".xlsx")
    if action is DocumentAction.DELIVERY_SUBSTITUTE:
        create_substitute_template(path)
    else:
        copyfile("assets/report_template.xlsx", path)
    return path


@pytest.mark.parametrize("action", [DocumentAction.DELIVERY_REPORT, DocumentAction.DELIVERY_SUBSTITUTE])
def test_legacy_override_belongs_only_to_school_and_reset_masks_it(tmp_path, action):
    prefs = QSettings(str(tmp_path / "prefs.ini"), QSettings.Format.IniFormat)
    school = DeliveryTemplateStore("db", settings=prefs)
    happy = DeliveryTemplateStore("db", profile=Profile.HAPPY_LUNCHBOX, settings=prefs)
    old = template_file(tmp_path, action)
    prefs.setValue(school.legacy_key + action.value, str(old))
    assert school.selected(action) == old
    assert happy.selected(action) is None
    happy.save(action, old)
    school.save(action, None)
    assert school.selected(action) is None
    assert happy.selected(action) == old
    assert prefs.value(school.legacy_key + action.value) == str(old)
    assert DeliveryTemplateStore("db", settings=prefs).selected(action) is None


def test_settings_cards_open_correct_profile_without_switching_selection(app, preferences):
    calls = []
    class Editor(QDialog):
        def __init__(self, action, database_id, parent, *, profile):
            super().__init__(parent)
            calls.append((action, database_id, profile))
    dialog = DocumentTemplateDialog(None, delivery_dialog_type=Editor)
    dialog.set_preferences_scope("db")
    assert "문서 양식" in dialog.windowTitle()
    for (profile, action), button in dialog._delivery_buttons.items():
        button.click()
        assert calls[-1] == (action, "db", profile)
        dialog.findChildren(Editor)[-1].reject()
        app.processEvents()
    with pytest.raises(ValueError):
        _ = dialog.selected_profile
    assert all("미설정" in label.text() for (profile, _), label in dialog._delivery_status.items() if profile is Profile.HAPPY_LUNCHBOX)


def test_unsaved_radio_cancel_keeps_operating_profile_and_scope(app, session, preferences):
    before = session.database.current_roster().revision_id
    seen = []
    def factory(current, parent):
        dialog = DocumentTemplateDialog(current, parent)
        def cancel():
            seen.append(dialog._database_id)
            dialog._buttons[Profile.HAPPY_LUNCHBOX].click()
            dialog.reject()
        QTimer.singleShot(0, cancel)
        return dialog
    outcome = DocumentTemplateController(session, dialog_type=factory).run()
    assert not outcome.completed
    assert seen == [str(session.credentials.database_id)]
    assert session.database.current_roster().revision_id == before


def test_output_screen_has_no_template_edit_entry(app, session, tmp_path):
    selection = selection_for(session, tmp_path)
    dialog = DocumentOutputDialog(selection.document, tmp_path)
    assert not hasattr(dialog, "template_button")
    assert not hasattr(dialog, "set_template_scope")


def test_happy_unconfigured_manager_does_not_clone_school(tmp_path, app, preferences):
    store = preferences("db", profile=Profile.HAPPY_LUNCHBOX)
    dialog = DeliveryTemplateDialog(DocumentAction.DELIVERY_SUBSTITUTE, "db", store=store)
    assert "미설정" in dialog.path_label.text()
    with pytest.raises(ValueError, match="설정 → 문서 양식"):
        store.create(DocumentAction.DELIVERY_SUBSTITUTE)
    assert not store.root.exists()


def set_profile(session, profile):
    head = session.database.current_roster().revision_id
    prepared = build_document_template_revision(head, None, profile, actor=ACTOR,
        recorded_at=datetime.now(UTC), revision_id=uuid4())
    session.append_revision(prepared.record)


@pytest.mark.parametrize("action", [DocumentAction.DELIVERY_REPORT, DocumentAction.DELIVERY_SUBSTITUTE])
@pytest.mark.parametrize("profile", [None, Profile.SCHOOL_MEAL, Profile.HAPPY_LUNCHBOX])
def test_saved_profile_routes_frozen_template_and_manifest(app, session, preferences, tmp_path, action, profile):
    if profile is not None:
        set_profile(session, profile)
    selected_profile = profile or Profile.SCHOOL_MEAL
    chosen = template_file(tmp_path, action)
    preferences(str(session.credentials.database_id), profile=selected_profile).save(action, chosen)
    before = session.database.current_roster().revision_id
    selection = selection_for(session, tmp_path)
    if action is DocumentAction.DELIVERY_REPORT:
        document = replace(selection.document, period=replace(selection.document.period, action=action), substitute_options=None)
        selection = next_available_document_output(document, tmp_path, selection.mode)
    received = []
    def writer(*args, **kwargs):
        output = args[1] if action is DocumentAction.DELIVERY_REPORT else args[2]
        template = args[2] if action is DocumentAction.DELIVERY_REPORT else kwargs["template_path"]
        received.append(Path(template).read_bytes())
        wb = openpyxl.Workbook()
        wb.active["A1"] = "synthetic"
        return publish_workbook(wb, output, password=kwargs["output_password"], overwrite=False)
    messages = Messages()
    outcome = DocumentGenerationController(session, message_box=messages, report_writer=writer, substitute_writer=writer).run(selection)
    assert outcome.completed, messages.errors
    assert received == [chosen.read_bytes()]
    manifest = session.database.document_runs()[0].template_manifest
    assert manifest["delivery_template_profile"] == selected_profile.value
    assert manifest["template_sha256"] == sha256(chosen.read_bytes()).hexdigest()
    if profile is not None:
        assert manifest["template_setting"]["profile"] == profile.value
        assert manifest["column_mapping_setting"] is None
    assert session.database.current_roster().revision_id == before


@pytest.mark.parametrize("action", [DocumentAction.DELIVERY_REPORT, DocumentAction.DELIVERY_SUBSTITUTE])
def test_happy_missing_file_blocks_without_school_fallback(app, session, preferences, tmp_path, action):
    set_profile(session, Profile.HAPPY_LUNCHBOX)
    # Even a legacy school binding must never satisfy a happy request.
    school = preferences(str(session.credentials.database_id))
    school.settings.setValue(school.legacy_key + action.value, str(template_file(tmp_path, action)))
    selection = selection_for(session, tmp_path)
    if action is DocumentAction.DELIVERY_REPORT:
        document = replace(selection.document, period=replace(selection.document.period, action=action), substitute_options=None)
        selection = next_available_document_output(document, tmp_path, selection.mode)
    def unexpected(*args, **kwargs):
        raise AssertionError("writer must not run")
    messages = Messages()
    outcome = DocumentGenerationController(session, message_box=messages, report_writer=unexpected, substitute_writer=unexpected).run(selection)
    assert not outcome.completed
    assert "설정 → 문서 양식" in str(messages.errors)
    assert not selection.output_path.exists()
    assert session.database.document_runs() == ()


def test_actual_settings_menu_opens_scoped_document_template_dialog(app, session, preferences):
    from types import SimpleNamespace
    from src.desktop_v2.application import DesktopStartupController
    from src.desktop_v2.settings_hub import SettingsAction, SettingsHubDialog
    seen = []
    def hub_factory(parent=None):
        hub = SettingsHubDialog(parent)
        QTimer.singleShot(0, lambda: hub._choose(SettingsAction.DOCUMENT_TEMPLATE))
        return hub
    def dialog_factory(current, parent=None):
        dialog = DocumentTemplateDialog(current, parent)
        def close():
            seen.append((dialog._database_id, len(dialog._delivery_buttons)))
            dialog.reject()
        QTimer.singleShot(0, close)
        return dialog
    controller = DesktopStartupController(settings=SimpleNamespace(), settings_hub_dialog_type=hub_factory,
        document_template_controller_type=lambda current_session: DocumentTemplateController(current_session, dialog_type=dialog_factory))
    controller._run_settings_hub(session, None)
    assert SettingsAction.DOCUMENT_TEMPLATE.label == "문서 양식"
    assert seen == [(str(session.credentials.database_id), 4)]
