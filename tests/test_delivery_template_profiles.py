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


def template_file(tmp_path, action, profile=Profile.SCHOOL_MEAL):
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / (action.value + ".xlsx")
    if action is DocumentAction.DELIVERY_SUBSTITUTE:
        create_substitute_template(path)
    else:
        copyfile("assets/행복도시락/석식일일식수템플릿.xlsx" if profile is Profile.HAPPY_LUNCHBOX else "assets/급식/일일식수템플릿.xlsx", path)
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
    happy_file = template_file(tmp_path / "happy", action, Profile.HAPPY_LUNCHBOX)
    happy.save(action, happy_file)
    school.save(action, None)
    assert school.selected(action) is None
    assert happy.selected(action) == happy_file
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
    assert all("기본 양식" in label.text() for (profile, _), label in dialog._delivery_status.items() if profile is Profile.HAPPY_LUNCHBOX)


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


def test_happy_default_list_is_separate_identical_template(tmp_path, app, preferences):
    store = preferences("db", profile=Profile.HAPPY_LUNCHBOX)
    dialog = DeliveryTemplateDialog(DocumentAction.DELIVERY_SUBSTITUTE, "db", store=store)
    assert "기본 양식" in dialog.path_label.text()
    path = store.create(DocumentAction.DELIVERY_SUBSTITUTE)
    assert path.read_bytes() == Path("assets/급식/배송목록템플릿.xlsx").read_bytes()
    assert path.parent == store.root


def set_profile(session, profile):
    head = session.database.current_roster().revision_id
    prepared = build_document_template_revision(head, None, profile, actor=ACTOR,
        recorded_at=datetime.now(UTC), revision_id=uuid4())
    session.append_revision(prepared.record)


def test_happy_default_report_publishes_actual_workbook_and_seals_template_hash(app, session, preferences, tmp_path):
    set_profile(session, Profile.HAPPY_LUNCHBOX)
    selected = selection_for(session, tmp_path)
    document = replace(selected.document,
        period=replace(selected.document.period, action=DocumentAction.DELIVERY_REPORT), substitute_options=None)
    selected = next_available_document_output(document, tmp_path, selected.mode)
    messages = Messages()
    outcome = DocumentGenerationController(session, message_box=messages).run(selected)
    assert outcome.completed, messages.errors
    workbook = openpyxl.load_workbook(outcome.result.output_path)
    # SQLite snapshot iteration can change which region is encountered first.
    assert {workbook.active[f"C{row}"].value: workbook.active[f"D{row}"].value
            for row in (4, 5)} == {"가동": 9, "나동": 3}
    workbook.close()
    manifest = session.database.document_runs()[0].template_manifest
    assert manifest["writer"] == "happy_delivery_report.make_happy_report_publication"
    assert set(manifest["templates"]) == {"dinner"}
    assert manifest["column_mapping"]["driver"] == "배송담당"
    assert manifest["templates"]["dinner"]["sha256"] == manifest["template_sha256"]


def test_both_happy_templates_are_frozen_and_recorded_in_one_run(app, preferences, tmp_path):
    from datetime import date
    from src.desktop_v2.document_meal import DocumentMealSelection
    from src.desktop_v2.document_period import DocumentPeriodSelection
    from src.desktop_v2.document_output import DocumentOutputMode
    from src.domain.roster import Meal
    from src.storage.database_session import DatabaseSession
    from src.generators.happy_delivery_report import make_happy_report_publication
    from tests.test_sqlite_delivery_report_service import _snapshot
    from tests.test_delivery_substitute_v2 import initial_record
    day = date(2026, 8, 17)
    backup = tmp_path / "both-backups"
    backup.mkdir()
    with DatabaseSession.create(tmp_path / "both.sqlite3", "synthetic-password", backup_directory=backup) as opened:
        snapshot, _ = _snapshot(meals=(Meal.DINNER, Meal.LUNCH))
        opened.append_revision(initial_record(snapshot))
        set_profile(opened, Profile.HAPPY_LUNCHBOX)
        store = preferences(str(opened.credentials.database_id), profile=Profile.HAPPY_LUNCHBOX)
        dinner, lunch = store.create(DocumentAction.DELIVERY_REPORT), store.create(DocumentAction.DELIVERY_REPORT, meal="중식")
        store.save(DocumentAction.DELIVERY_REPORT, dinner)
        store.save(DocumentAction.DELIVERY_REPORT, lunch, meal="중식")
        expected = {"dinner": sha256(dinner.read_bytes()).hexdigest(), "lunch": sha256(lunch.read_bytes()).hexdigest()}
        frozen_paths = []
        def writer(reports, output, paths, **kwargs):
            frozen_paths.extend(paths.values())
            assert {key: sha256(path.read_bytes()).hexdigest() for key, path in paths.items()} == expected
            # External editing after calculation must not change this publication.
            lunch.write_bytes(b"changed after capture")
            return make_happy_report_publication(reports, output, paths, **kwargs)
        period = DocumentPeriodSelection(DocumentAction.DELIVERY_REPORT, opened.database.revision_summaries()[0], day, Meal.DINNER, day, day)
        selected = next_available_document_output(DocumentMealSelection(period, (Meal.DINNER, Meal.LUNCH)), tmp_path, DocumentOutputMode.SAVED_DEFAULT)
        messages = Messages()
        result = DocumentGenerationController(opened, message_box=messages, happy_report_writer=writer).run(selected)
        assert result.completed, messages.errors
        book = openpyxl.load_workbook(result.result.output_path)
        assert book.sheetnames == ["26.08.17", "중식_26.08.17"]
        book.close()
        manifest = opened.database.document_runs()[0].template_manifest
        assert {key: item["sha256"] for key, item in manifest["templates"].items()} == expected
        assert all(not path.exists() for path in frozen_paths)


@pytest.mark.parametrize("action", [DocumentAction.DELIVERY_REPORT, DocumentAction.DELIVERY_SUBSTITUTE])
@pytest.mark.parametrize("profile", [None, Profile.SCHOOL_MEAL, Profile.HAPPY_LUNCHBOX])
def test_saved_profile_routes_frozen_template_and_manifest(app, session, preferences, tmp_path, action, profile):
    if profile is not None:
        set_profile(session, profile)
    selected_profile = profile or Profile.SCHOOL_MEAL
    chosen = template_file(tmp_path, action, selected_profile)
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
        if isinstance(template, dict):
            template = template["dinner"]
        received.append(Path(template).read_bytes())
        wb = openpyxl.Workbook()
        wb.active["A1"] = "synthetic"
        return publish_workbook(wb, output, password=kwargs["output_password"], overwrite=False)
    messages = Messages()
    outcome = DocumentGenerationController(session, message_box=messages, report_writer=writer, happy_report_writer=writer, substitute_writer=writer).run(selection)
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
def test_happy_missing_file_uses_own_backup_or_blocks_without_school_fallback(app, session, preferences, tmp_path, action):
    set_profile(session, Profile.HAPPY_LUNCHBOX)
    happy = preferences(str(session.credentials.database_id), profile=Profile.HAPPY_LUNCHBOX)
    broken = template_file(tmp_path / "broken", action, Profile.HAPPY_LUNCHBOX)
    happy.save(action, broken)
    previous = broken.read_bytes()
    broken.unlink()
    # Even a legacy school binding must never satisfy a broken happy request.
    school = preferences(str(session.credentials.database_id))
    school.settings.setValue(school.legacy_key + action.value, str(template_file(tmp_path, action)))
    selection = selection_for(session, tmp_path)
    if action is DocumentAction.DELIVERY_REPORT:
        document = replace(selection.document, period=replace(selection.document.period, action=action), substitute_options=None)
        selection = next_available_document_output(document, tmp_path, selection.mode)
    received = []
    def writer(*args, **kwargs):
        received.append(kwargs["template_path"].read_bytes())
        book = openpyxl.Workbook()
        book.active["A1"] = "synthetic"
        return publish_workbook(book, args[2], password=kwargs["output_password"], overwrite=False)
    def unexpected(*args, **kwargs):
        received.append("unexpected report call")
        raise AssertionError("report writer must not run")
    messages = Messages()
    outcome = DocumentGenerationController(session, message_box=messages, report_writer=unexpected, happy_report_writer=unexpected, substitute_writer=writer).run(selection)
    if action is DocumentAction.DELIVERY_SUBSTITUTE:
        assert outcome.completed, messages.errors
        assert messages.warnings and received == [previous]
        assert session.database.document_runs()[0].template_manifest["template_used_previous_valid"]
    else:
        assert not outcome.completed
        assert messages.errors and not received
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
