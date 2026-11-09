
"""Per-column alignment survives UI edits, preferences and Excel processing."""
from dataclasses import replace
from datetime import date, timedelta

import openpyxl
import pytest
from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QPushButton
from openpyxl.styles import Alignment

from src.desktop_v2.document_hub import DocumentAction
from src.desktop_v2.document_meal import DocumentMealDialog
from src.domain.daily_report_choices import DailyReportChoices
from src.domain.document_templates import DocumentTemplateProfile
from src.domain.roster import Meal
from src.generators import substitute_generator
from tests.test_desktop_v2_document_meal import _period
from tests.test_substitute_column_selection import make_dialog
from tests.test_delivery_substitute_v2 import app, session


@pytest.mark.parametrize("action", (DocumentAction.DELIVERY_REPORT, DocumentAction.DELIVERY_SUBSTITUTE))
@pytest.mark.parametrize("weekday", range(7))
@pytest.mark.parametrize("profile", (None, DocumentTemplateProfile.SCHOOL_MEAL, DocumentTemplateProfile.HAPPY_LUNCHBOX))
def test_weekday_defaults_survive_loading_saved_report_choices(app, action, weekday, profile):
    day = date(2026, 10, 26) + timedelta(days=weekday)
    period = replace(_period(action), starts_on=day, ends_on=day, anchor_meal=Meal.LUNCH)
    dialog = DocumentMealDialog(period)
    dialog.set_available_delivery_forms((None, "도시락"))
    dialog.set_report_choices(DailyReportChoices((Meal.BREAKFAST, Meal.LUNCH), (None,)), profile=profile)
    if action is DocumentAction.DELIVERY_SUBSTITUTE:
        dialog.set_substitute_profile(profile)
    expected = Meal.DINNER if weekday < 4 else Meal.BREAKFAST if weekday == 4 else Meal.LUNCH
    happy_report = action is DocumentAction.DELIVERY_REPORT and profile is DocumentTemplateProfile.HAPPY_LUNCHBOX
    expected_meals = (Meal.BREAKFAST, Meal.DINNER) if happy_report else (expected,)
    if action is DocumentAction.DELIVERY_SUBSTITUTE and profile is DocumentTemplateProfile.HAPPY_LUNCHBOX:
        expected_meals += (Meal.DINNER if expected is Meal.BREAKFAST else Meal.BREAKFAST,)
    assert dialog.selected_meals() == expected_meals
    if happy_report:
        assert "석식·조식을 기본 선택" in dialog._meal_help.text()
        assert "중식도 필요하면 체크" in dialog._meal_help.text()
    if action is DocumentAction.DELIVERY_REPORT:
        assert dialog.selected_delivery_forms() == ("도시락",)
        dialog._buttons_by_meal[Meal.BREAKFAST].setChecked(True)
        assert Meal.BREAKFAST in dialog.selected_meals()
    dialog.reject()


def test_alignment_tracks_columns_and_persists_hidden_settings_only_on_accept(app, session, tmp_path):
    settings = QSettings(str(tmp_path / "alignment.ini"), QSettings.Format.IniFormat)
    dialog, _ = make_dialog(session, tmp_path)
    dialog.set_preferences_scope("alignment", settings=settings)
    for axis, value in (("horizontal", "right"), ("vertical", "top")):
        combo = dialog._alignment_combos[axis]["road_address"]
        combo.setCurrentIndex(combo.findData(value))
        assert not combo.itemIcon(combo.currentIndex()).isNull()
    dialog._move_column("road_address", 0)
    assert dialog.options().horizontal_alignments["road_address"] == "right"
    dialog.set_visible_columns({"name"})
    assert "road_address" not in dialog.options().horizontal_alignments
    assert not any(button.text() in ("위로 ↑", "아래로 ↓") for button in dialog.findChildren(QPushButton))
    dialog.accept()
    restored, _ = make_dialog(session, tmp_path)
    restored.set_preferences_scope("alignment", settings=settings)
    assert restored.options().horizontal_alignments["road_address"] == "right"
    assert restored.options().vertical_alignments["road_address"] == "top"
    assert restored.options().horizontal_alignments["name"] == "left"
    item = restored.columns.topLevelItem(restored._column_keys().index("name"))
    item.setCheckState(0, Qt.CheckState.Unchecked)
    assert not restored._alignment_combos["horizontal"]["name"].isEnabled()
    restored._alignment_combos["horizontal"]["road_address"].setCurrentIndex(0)
    restored.reject()
    reopened, _ = make_dialog(session, tmp_path)
    reopened.set_preferences_scope("alignment", settings=settings)
    assert reopened.options().horizontal_alignments["road_address"] == "right"
    reopened.reject()


@pytest.mark.parametrize("horizontal,vertical", (("left", "top"), ("center", "center"), ("right", "bottom")))
def test_selected_alignment_reaches_saved_workbook_after_excel_overwrites_it(
    app, session, tmp_path, monkeypatch, horizontal, vertical
):
    dialog, data = make_dialog(session, tmp_path)
    selected = replace(dialog.options(), columns=("road_address", "name"),
                       horizontal_alignments={"road_address": horizontal, "name": "center"},
                       vertical_alignments={"road_address": vertical, "name": "top"})
    def measure(workbook, columns, layout, options, **kwargs):
        for row in workbook.active:
            for cell in row:
                cell.alignment = Alignment(vertical="bottom", wrap_text=True)
    monkeypatch.setattr(substitute_generator, "apply_excel_metrics", measure)
    wb = substitute_generator.build_substitute_workbook(data, selected)
    path = tmp_path / "aligned.xlsx"
    wb.save(path)
    wb.close()
    actual = openpyxl.load_workbook(path)
    ws = actual.active
    for address in ("A2", "A3"):
        assert ws[address].alignment.horizontal == horizontal
        assert ws[address].alignment.vertical == vertical
        assert ws[address].alignment.wrap_text
    assert ws["B3"].alignment.horizontal == "center"
    assert ws["B3"].alignment.vertical == "top"
    assert ws["A1"].alignment.horizontal is None
    assert ws.cell(len(data.rows) + 3, 1).alignment.horizontal is None
    assert "A3:A4" in {str(merged) for merged in ws.merged_cells.ranges}
    actual.close()
    dialog.reject()
