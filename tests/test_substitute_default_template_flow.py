"""Opening default delivery templates uses asset settings, not storage paths."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QWidget

from src.desktop_v2.application import DesktopStartupController
from src.desktop_v2.delivery_substitute import DeliverySubstituteDialog
from src.desktop_v2.dialog_workflow import launch
from src.desktop_v2.document_hub import DocumentAction
from src.desktop_v2.document_output import DocumentOutputDialog
from src.desktop_v2.portable_settings import DesktopStorageSettings
from src.domain.document_templates import (
    DOCUMENT_TEMPLATE_SETTING_KIND, DOCUMENT_TEMPLATE_SETTING_ID,
    DocumentTemplateProfile, DocumentTemplateSetting, document_template_setting_state,
)
from src.domain.revisions import EntityRef, HistoricalSnapshot
from src.domain.roster import Meal
from tests.test_delivery_substitute_v2 import source
from tests.test_desktop_v2_document_meal import _period


@pytest.mark.parametrize("profile", tuple(DocumentTemplateProfile))
def test_default_template_opens_driver_options_and_output(monkeypatch, tmp_path, profile):
    import src.desktop_v2.application as application
    import src.desktop_v2.delivery_template as templates
    import src.desktop_v2.delivery_substitute as substitute
    import src.desktop_v2.substitute_preferences as preferences
    import src.core.settings as core
    app = QApplication.instance() or QApplication([])
    window = QWidget()
    snapshot, roster = source(sizes=(1,), meal=Meal.DINNER)
    states = dict(snapshot.entities)
    states[EntityRef(DOCUMENT_TEMPLATE_SETTING_KIND, DOCUMENT_TEMPLATE_SETTING_ID)] = (
        document_template_setting_state(DocumentTemplateSetting(1, profile, snapshot.revision_id)))
    snapshot = HistoricalSnapshot(snapshot.revision_id, states)
    period = _period(DocumentAction.DELIVERY_SUBSTITUTE)
    summary = replace(period.source, revision_id=snapshot.revision_id)
    window.table_model = SimpleNamespace(service_day=period.anchor_day, meal=Meal.DINNER)
    session = SimpleNamespace(credentials=SimpleNamespace(database_id=uuid4()), database=SimpleNamespace(
        revision_summaries=lambda: (summary,), snapshot=lambda _: snapshot, current_roster=lambda: roster))
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    storage = DesktopStorageSettings(settings=settings, base_directory=tmp_path)
    real_store = templates.DeliveryTemplateStore
    monkeypatch.setattr(templates, "DeliveryTemplateStore", lambda db, **kwargs: real_store(
        db, **kwargs, settings=settings, root=tmp_path / "templates"))
    monkeypatch.setattr(preferences, "preferences", lambda _: (settings, "substitute-test"))
    monkeypatch.setattr(substitute, "preferences", lambda *_: (settings, "substitute-test"))
    requested = []
    def template_path(selected):
        requested.append(selected)
        return str(Path("assets/행복도시락/배송목록템플릿.xlsx").resolve())
    monkeypatch.setattr(core, "get_settings", lambda: SimpleNamespace(get_substitute_template_path=template_path))
    monkeypatch.setattr(application, "run_progress_task", lambda work, **kwargs: work())
    class Messages:
        @staticmethod
        def critical(parent, title, message):
            pytest.fail(title + ": " + message)
        information = critical
        warning = critical
    controller = DesktopStartupController(settings=storage, message_box=Messages)
    monkeypatch.setattr(controller, "_ensure_document_prerequisites", lambda *_: True)
    run = launch(controller._run_document_hub, session, window)
    def settle():
        app.processEvents()
        app.processEvents()
        assert run.error is None
    try:
        run.request.dialog._cards[DocumentAction.DELIVERY_SUBSTITUTE].click()
        settle()
        run.request.dialog.accept()  # Period
        settle()
        run.request.dialog.accept()  # Meal
        settle()
        dialog = run.request.dialog
        assert isinstance(dialog, DeliverySubstituteDialog)
        assert requested == [profile.value]
        assert dialog._placeholder_panel is not None
        assert dialog.accept_button.isEnabled()
        dialog.accept()
        settle()
        assert isinstance(run.request.dialog, DocumentOutputDialog)
        run.request.dialog.reject()
        settle()
        assert run.closed
    finally:
        window.close()
