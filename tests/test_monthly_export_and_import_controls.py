from dataclasses import replace
from datetime import date
from types import SimpleNamespace
from uuid import uuid4
import json
from hashlib import sha256

import pytest
from openpyxl import Workbook
from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QApplication, QDialog

from src.desktop_v2.current_sheet_export import build_current_sheet_workbook
from src.desktop_v2.workspace_tools import MonthlyOverviewDialog
from src.desktop_v2.import_wizard import RosterImportWizard
from src.domain.roster import InclusiveDateRange, Meal
from src.domain.status import MealStatus
from src.importing.revision import ImportPurpose
from src.services.monthly_meal_history import MonthlyMealHistory, MonthlyMealHistoryEntry
from src.services.monthly_meal_statistics import MonthlyMealStatistics, MonthlyMealDay
from tests.test_import_database_columns import roster, source, wizard_for


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_monthly_export_captures_basis_totals_filtered_sort_and_literal_notes(app):
    revision = uuid4()
    days = (MonthlyMealDay(date(2026, 11, 1), 1, 3, 0),)
    service = (MonthlyMealDay(date(2026, 11, 1), 1, 1, 0),)
    statistics = MonthlyMealStatistics(revision, 2026, 11, days, 1, 3, 0, delivery_days=days, service_days=service)
    entry = MonthlyMealHistoryEntry(uuid4(), "가", None, None, Meal.DINNER, MealStatus.STOPPED,
                                   InclusiveDateRange(date(2026, 11, 1)), "담당", "20261101-001", "전화", "=1+2")
    history = MonthlyMealHistory(revision, 2026, 11, (entry, replace(entry, name="나", revision_code="20261101-002")))
    dialog = MonthlyOverviewDialog(date(2026, 11, 1), lambda *args: statistics, lambda *args: history)
    exported = []
    dialog.export_requested.connect(exported.append)
    dialog.export_button.click()
    assert exported[0].sheets[0].rows[-1] == ("월 합계", "1", "3", "0", "4")
    dialog.statistics.basis.setCurrentIndex(1)
    assert "식사일 기준" in dialog.export_snapshot().sheet_name
    assert dialog.export_snapshot().sheets[0].rows[-1][-1] == "2"
    dialog.tabs.setCurrentIndex(1)
    assert [row[0] for row in dialog.export_snapshot().sheets[0].rows] == ["나", "가"]
    dialog.history._set_column_values("name", frozenset({"가"}))
    captured = dialog.export_snapshot()
    assert len(captured.sheets[0].rows) == 1
    workbook = build_current_sheet_workbook(captured)
    assert workbook.active["F2"].value == "=1+2"
    assert workbook.active["F2"].data_type == "s"
    workbook.close()
    dialog.history._set_column_values("name", frozenset())
    assert dialog.export_snapshot().sheets[0].rows == ()
    dialog.month.setCurrentIndex(11)  # old-month result must never be exported as December
    assert dialog.export_snapshot() is None
    assert not dialog.export_button.isEnabled()
    dialog.close()


def test_lunch_controls_hide_unused_labels_and_preserve_values(app):
    wizard = RosterImportWizard(allow_vacation_lunch=True)
    wizard.purpose_combo.setCurrentIndex(wizard.purpose_combo.findData(ImportPurpose.VACATION_LUNCH))
    form = wizard.lunch_filter_panel.layout()
    assert wizard.lunch_filter_column.isHidden()
    assert wizard.lunch_filter_value.isHidden()
    assert form.labelForField(wizard.lunch_filter_column).isHidden()
    assert not hasattr(wizard, "lunch_duplicate_combo")
    wizard.lunch_filter_mode.setCurrentIndex(2)
    wizard.lunch_filter_value.setText("대상")
    assert not wizard.lunch_filter_column.isHidden()
    assert not wizard.lunch_filter_value.isHidden()
    wizard.lunch_filter_mode.setCurrentIndex(1)
    assert not wizard.lunch_filter_column.isHidden()
    assert wizard.lunch_filter_value.isHidden()
    assert form.labelForField(wizard.lunch_filter_value).isHidden()
    wizard.lunch_filter_mode.setCurrentIndex(2)
    assert wizard.lunch_filter_value.text() == "대상"
    wizard.close()


def test_household_badge_and_required_mapping_include_custom_ids(app, tmp_path):
    current = roster()
    wizard = wizard_for(source(tmp_path / "source.xlsx"), current)
    custom = "custom:" + str(current.custom_fields[0].definition_id)
    wizard.set_household_grouping_columns(("name", custom))
    assert not wizard.destination_table.cards[custom].household_badge.isHidden()
    assert wizard.destination_table.cards[custom].household_badge.text() == "(가구 묶기 기준)"
    wizard.mapping_dialog.accept()
    assert wizard.mapping_dialog.result() != QDialog.DialogCode.Accepted
    assert "관리번호" in wizard.destination_table.error.text()
    wizard.destination_table.assign_source("D", custom)
    wizard.mapping_dialog.accept()
    assert wizard._mapping_review_complete()
    wizard.destination_table.assign_source("D", "ignore")
    assert not wizard._mapping_review_complete()
    with pytest.raises(ValueError, match="관리번호"):
        wizard._validate_household_mapping()
    wizard.close()


def test_delivery_placeholder_saved_primary_fallback_and_path_scope(app, tmp_path, monkeypatch):
    from src.desktop_v2 import import_document_usage as usage
    from src.domain.document_templates import DocumentTemplateProfile
    from src.desktop_v2.substitute_preferences import preferences
    current = roster()
    template = tmp_path / "delivery.xlsx"
    book = Workbook()
    book.active.append(["{성명}", "{추가}", "{관할동 합치기}"])
    book.save(template)
    book.close()
    settings = QSettings(str(tmp_path / "preferences.ini"), QSettings.Format.IniFormat)
    store, key = preferences("synthetic", settings)
    custom = "custom:" + str(current.custom_fields[0].definition_id)
    scope = sha256(str(template.resolve()).encode("utf-8")).hexdigest()
    store.setValue(key, json.dumps({"placeholder_templates": {
        scope: {"template_mapping": {"성명": custom, "추가": "school_name"}, "template_fallbacks": {"추가": "mobile_phone", "삭제": "note"}},
        "another-template": {"template_mapping": {"추가": "note"}}}}))
    monkeypatch.setattr(usage, "document_template_setting_from_snapshot",
                        lambda snapshot: SimpleNamespace(profile=DocumentTemplateProfile.SCHOOL_MEAL))
    captured = []
    def capture(snapshot, roster, **kwargs):
        captured.append(kwargs["substitute_sources"])
        from src.services.import_column_priority import key_import_targets
        return key_import_targets(kwargs["substitute_sources"]), ()
    monkeypatch.setattr(usage, "document_import_targets", capture)
    targets, notices = usage.import_document_targets(object(), current, "synthetic",
        settings=SimpleNamespace(get_substitute_template_path=lambda profile: str(template)), preferences_store=settings)
    assert not notices
    assert targets == {custom, "profile:school_name", "profile:mobile_phone", "profile:jurisdiction"}
    assert "profile:name" not in targets and "profile:note" not in targets
