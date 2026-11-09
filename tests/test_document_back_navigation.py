"""Real back buttons retain document steps and never generate on navigation."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from types import SimpleNamespace
import pytest
from PySide6.QtWidgets import QApplication, QDialogButtonBox, QWidget
from src.desktop_v2.application import DesktopStartupController
from src.desktop_v2.dialog_workflow import launch
from src.desktop_v2.document_hub import DocumentAction
from src.desktop_v2.document_meal import DocumentMealDialog
from src.desktop_v2.document_output import DocumentOutputDialog
from src.desktop_v2.document_period import DocumentPeriodDialog
from src.domain.document_templates import DocumentTemplateProfile
from src.domain.roster import Meal
from src.domain.daily_report_choices import DailyReportChoices, ReportRemarkOptions
from tests.test_desktop_v2_document_meal import _period
from tests.test_delivery_report_forms import _snapshot


@pytest.mark.parametrize("profile", (None, DocumentTemplateProfile.HAPPY_LUNCHBOX))
def test_real_back_buttons_reopen_same_steps_with_unsaved_choices(monkeypatch, tmp_path, profile):
    app = QApplication.instance() or QApplication([])
    window = QWidget()
    period_value = _period(DocumentAction.DELIVERY_REPORT)
    window.table_model = SimpleNamespace(service_day=period_value.anchor_day, meal=period_value.anchor_meal)
    emitted = []
    window.document_output_selected = SimpleNamespace(emit=emitted.append)
    snapshot, _ = _snapshot()
    session = SimpleNamespace(database=SimpleNamespace(revision_summaries=lambda: (period_value.source,), snapshot=lambda _: snapshot))
    import src.desktop_v2.application as module
    monkeypatch.setattr(module, "load_daily_report_setup", lambda _: (DailyReportChoices(), profile))
    monkeypatch.setattr(module, "save_daily_report_choices", lambda *_args, **_kwargs: (None, None))
    controller = DesktopStartupController(settings=SimpleNamespace(document_output_directory=tmp_path))
    monkeypatch.setattr(controller, "_ensure_document_prerequisites", lambda *_: True)
    monkeypatch.setattr(controller, "_replace_roster_if_changed", lambda *_: None)
    window.show()
    run = launch(controller._run_document_hub, session, window)
    hub = run.request.dialog
    def settle():
        app.processEvents()
        app.processEvents()
        assert run.error is None
    def back(dialog):
        dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Cancel).click()
        settle()
    hub._cards[DocumentAction.DELIVERY_REPORT].click()
    settle()
    period = run.request.dialog
    assert isinstance(period, DocumentPeriodDialog)
    period.accept()
    settle()
    meal = run.request.dialog
    assert isinstance(meal, DocumentMealDialog)
    assert meal.selected_meals() == (
        (Meal.BREAKFAST, Meal.DINNER)
        if profile is DocumentTemplateProfile.HAPPY_LUNCHBOX else (Meal.DINNER,)
    )
    meal._buttons_by_meal[Meal.BREAKFAST].setChecked(False)
    meal._buttons_by_meal[Meal.LUNCH].setChecked(True)
    selected_meals = meal.selected_meals()
    options = ReportRemarkOptions(("note",), "below", ("BOX",), False)
    meal._remark_options = options
    meal._extra_settings_toggle.setChecked(True)
    back(meal)
    assert run.request.dialog is period and period.isVisible()
    period.accept()
    settle()
    assert run.request.dialog is meal and meal._remark_options == options
    assert meal._extra_settings_toggle.isChecked()
    assert meal.selected_meals() == selected_meals
    meal.accept()
    settle()
    output = run.request.dialog
    assert isinstance(output, DocumentOutputDialog)
    back(output)
    assert run.request.dialog is meal
    meal.accept()
    settle()
    assert run.request.dialog is output
    back(output)
    back(meal)
    back(period)
    assert run.request.dialog is hub and hub.isVisible()
    hub._cards[DocumentAction.DELIVERY_REPORT].click()
    settle()
    assert run.request.dialog is period
    period.reject()
    settle()
    assert run.closed and emitted == []
    window.close()


@pytest.mark.parametrize("profile", tuple(DocumentTemplateProfile))
def test_substitute_meal_screen_uses_current_profile_and_keeps_choice_on_back(monkeypatch, tmp_path, profile):
    from uuid import uuid4
    from src.domain.revisions import HistoricalSnapshot, EntityRef
    from src.domain.document_templates import (
        DOCUMENT_TEMPLATE_SETTING_KIND, DOCUMENT_TEMPLATE_SETTING_ID,
        DocumentTemplateSetting, document_template_setting_state,
    )
    app = QApplication.instance() or QApplication([])
    window = QWidget()
    period_value = _period(DocumentAction.DELIVERY_SUBSTITUTE)
    window.table_model = SimpleNamespace(service_day=period_value.anchor_day, meal=period_value.anchor_meal)
    snapshot, _ = _snapshot()
    current_id = uuid4()
    current = HistoricalSnapshot(current_id, {
        EntityRef(DOCUMENT_TEMPLATE_SETTING_KIND, DOCUMENT_TEMPLATE_SETTING_ID):
            document_template_setting_state(DocumentTemplateSetting(1, profile, current_id)),
    })
    session = SimpleNamespace(database=SimpleNamespace(
        revision_summaries=lambda: (period_value.source,),
        current_roster=lambda: SimpleNamespace(revision_id=current_id),
        snapshot=lambda revision: current if revision == current_id else snapshot,
    ))
    controller = DesktopStartupController(settings=SimpleNamespace(document_output_directory=tmp_path))
    monkeypatch.setattr(controller, "_ensure_document_prerequisites", lambda *_: True)
    run = launch(controller._run_document_hub, session, window)
    def settle():
        app.processEvents()
        app.processEvents()
        assert run.error is None
    try:
        run.request.dialog._cards[DocumentAction.DELIVERY_SUBSTITUTE].click()
        settle()
        period = run.request.dialog
        period.accept()
        settle()
        meal = run.request.dialog
        assert isinstance(meal, DocumentMealDialog)
        happy = profile is DocumentTemplateProfile.HAPPY_LUNCHBOX
        assert meal._include_breakfast.isVisible() == happy
        assert len(meal.selection().meals) == (2 if happy else 1)
        meal._include_breakfast.setChecked(False)
        meal.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Cancel).click()
        settle()
        assert run.request.dialog is period
        period.accept()
        settle()
        assert run.request.dialog is meal
        assert len(meal.selection().meals) == 1
        meal.reject()
        settle()
    finally:
        window.close()
