"""Guided first-run workbook route tests."""

from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import replace
from threading import Event
from datetime import date
from pathlib import Path
from uuid import UUID

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import openpyxl
from openpyxl.worksheet.table import Table
import pytest
from PySide6.QtCore import QDate, QPoint, QPointF, Qt, QTimer
from PySide6.QtGui import QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog

import src.desktop_v2.import_wizard as import_wizard_module
import src.importing.workbook as workbook_module
from src.domain import Meal
from src.desktop_v2 import RosterImportWizard
from src.importing import (
    ImportPurpose,
    HouseholdMatchCriterion,
    HouseholdMatchingRule,
    ImportMappingProfile,
    ProfileField,
    StoredHouseholdMatchingRule,
    StoredImportProfile,
    WorkbookImportPasswordRequiredError,
    inspect_workbook,
    parse_workbook,
)


def _save_with_table(workbook: openpyxl.Workbook, path: Path) -> None:
    worksheet = workbook.active
    header_row = 2 if worksheet.title == "조사표" else 1
    if worksheet.title == "조사표":
        for column in range(1, worksheet.max_column + 1):
            cell = worksheet.cell(header_row, column)
            if cell.value is None:
                cell.value = f"미사용 {cell.column_letter}"
    worksheet.add_table(Table(displayName="명단", ref=f"A{header_row}:{worksheet.cell(worksheet.max_row, worksheet.max_column).coordinate}"))
    workbook.save(path)


def test_vacation_lunch_wizard_accepts_source_without_meal_column(app, tmp_path):
    path = tmp_path / "lunch-only.xlsx"
    workbook = openpyxl.Workbook()
    workbook.active.append(("성명", "생년월일"))
    workbook.active.append(("방학아동", "20150302"))
    _save_with_table(workbook, path)
    workbook.close()

    wizard = RosterImportWizard(allow_vacation_lunch=True)
    wizard.purpose_combo.setCurrentIndex(
        wizard.purpose_combo.findData(ImportPurpose.VACATION_LUNCH)
    )
    wizard.path_edit.setText(str(path))
    assert wizard._inspect_source()
    wizard._go_next()
    wizard._go_next()
    assert wizard.preview is not None and wizard.preview.can_continue, wizard.error_label.text()
    assert wizard.preview.observations[0].meals == (Meal.LUNCH,)
    assert wizard.preview.profile.default_meals == (Meal.LUNCH,)
    wizard._go_next()
    assert wizard.selection().purpose is ImportPurpose.VACATION_LUNCH
    wizard.close()


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _combined(path: Path, meal: str = "조식 / 중식") -> Path:
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.title = "기존 명단"
    worksheet.append(("성명", "생년월일", "식종", "핸드폰", "내부 메모"))
    worksheet.append(("홍길동", "20150302", meal, "010-1234-5678", "복사 금지"))
    _save_with_table(workbook, path)
    workbook.close()
    return path


def test_unchecked_additional_column_survives_saved_profile(app, tmp_path):
    path = _combined(tmp_path / "additional.xlsx")
    wizard = RosterImportWizard()
    wizard.path_edit.setText(str(path))
    assert wizard._inspect_source()
    extra = wizard.ignored_columns_list.item(4)
    assert extra.checkState() == Qt.CheckState.Checked
    extra.setCheckState(Qt.CheckState.Unchecked)
    profile = wizard._build_profile()
    assert profile == wizard._build_profile()
    assert tuple(profile.custom_field_columns.values()) == ("E",)
    assert tuple(profile.custom_field_names.values()) == ("내부 메모",)
    preview = parse_workbook(path, profile, table_name="명단")
    definition_id = next(iter(profile.custom_field_columns))
    assert preview.observations[0].custom_values == {definition_id: "복사 금지"}
    stored = StoredImportProfile(profile, preview.header_fingerprint)
    wizard.close()

    reused = RosterImportWizard(existing_profiles=(stored,))
    reused.path_edit.setText(str(path))
    assert reused._inspect_source()
    assert reused.ignored_columns_list.item(4).checkState() == Qt.CheckState.Unchecked
    assert reused._build_profile() == profile
    # Rebuilding widgets for the same table must not reset the saved selection.
    assert reused._inspect_source()
    assert reused._build_profile() == profile
    reused.close()


def test_only_active_meal_controls_reserve_source_columns(app, tmp_path):
    path = _combined(tmp_path / "dormant.xlsx")
    wizard = RosterImportWizard(allow_vacation_lunch=True)
    wizard.path_edit.setText(str(path))
    assert wizard._inspect_source()
    # A remembered flag control is inactive while combined mode is selected.
    wizard._set_combo(wizard.breakfast_combo, "E")
    extra = wizard.ignored_columns_list.item(4)
    assert extra.flags() & Qt.ItemFlag.ItemIsEnabled
    extra.setCheckState(Qt.CheckState.Unchecked)
    assert tuple(wizard._build_profile().custom_field_columns.values()) == ("E",)
    wizard.meal_mode_combo.setCurrentIndex(wizard.meal_mode_combo.findData("flags"))
    assert not extra.flags() & Qt.ItemFlag.ItemIsEnabled
    profile = wizard._build_profile()
    assert profile.meal_flag_columns == {Meal.BREAKFAST: "E"}
    assert not profile.custom_field_columns
    # Vacation import has no active meal source column.
    wizard.purpose_combo.setCurrentIndex(wizard.purpose_combo.findData(ImportPurpose.VACATION_LUNCH))
    assert extra.flags() & Qt.ItemFlag.ItemIsEnabled
    extra.setCheckState(Qt.CheckState.Unchecked)
    profile = wizard._build_profile()
    assert profile.default_meals == (Meal.LUNCH,)
    assert tuple(profile.custom_field_columns.values()) == ("E",)
    wizard.close()


def test_saved_header_name_custom_mapping_restores_values_and_id(app, tmp_path):
    path = _combined(tmp_path / "saved-header.xlsx")
    initial = RosterImportWizard()
    initial.path_edit.setText(str(path))
    assert initial._inspect_source()
    initial.ignored_columns_list.item(4).setCheckState(Qt.CheckState.Unchecked)
    profile = initial._build_profile()
    definition_id = next(iter(profile.custom_field_columns))
    profile = replace(profile, custom_field_columns={definition_id: "내부 메모"}, custom_field_names={})
    stored = StoredImportProfile(profile, initial._inspection.header_fingerprint)
    initial.close()
    restored = RosterImportWizard(existing_profiles=(stored,))
    restored.path_edit.setText(str(path))
    assert restored._inspect_source()
    assert restored.ignored_columns_list.item(4).checkState() == Qt.CheckState.Unchecked
    selected = restored._build_profile()
    assert selected.custom_field_columns == {definition_id: "E"}
    assert selected == restored._build_profile()
    assert parse_workbook(path, selected).observations[0].custom_values == {definition_id: "복사 금지"}
    restored.close()


def _survey(path: Path) -> Path:
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.title = "조사표"
    worksheet["A1"] = "제목"
    for column, header in {
        "A": "원본 연번",
        "G": "성명",
        "H": "생년월일",
        "M": "기존 주소",
        "N": "변경주소",
        "O": "핸드폰",
        "S": "연중 조식",
        "T": "연중 석식",
        "U": "토공휴일 중식",
        "V": "방학 중식",
        "AI": "토공휴일 석식",
    }.items():
        worksheet[f"{column}2"] = header
    for column, value in {
        "A": 1,
        "G": "홍길동",
        "H": "20150302",
        "M": "이전 주소",
        "O": "01012345678",
        "S": 1,
        "T": 0,
        "V": 1,
    }.items():
        worksheet[f"{column}3"] = value
    _save_with_table(workbook, path)
    workbook.close()
    return path


def test_wizard_requires_table_and_clears_old_preview_on_file_change(app, tmp_path):
    table_path = _combined(tmp_path / "with-table.xlsx")
    plain_path = tmp_path / "without-table.xlsx"
    workbook = openpyxl.Workbook()
    workbook.active.append(("성명", "식종"))
    workbook.active.append(("표 밖 아동", "중식"))
    workbook.save(plain_path)
    workbook.close()

    wizard = RosterImportWizard()
    assert wizard.purpose_combo.parent() is None
    wizard.path_edit.setText(str(table_path))
    assert wizard._inspect_source()
    assert wizard.raw_table.rowCount() == 1
    wizard.path_edit.setText(str(plain_path))
    assert wizard.inspection is None
    assert wizard.raw_table.rowCount() == 0
    assert not wizard._inspect_source()
    assert "삽입 → 표" in wizard.error_label.text()
    assert wizard.table_combo.count() == 0
    wizard.close()


def test_wizard_lists_tables_across_sheets_and_clears_corrections_on_switch(app, tmp_path):
    path = tmp_path / "two-tables.xlsx"
    workbook = openpyxl.Workbook()
    first = workbook.active
    first.title = "첫 시트"
    first.append(("성명", "생년월일", "식종"))
    first.append(("첫 아동", "20150302", "조식"))
    first.add_table(Table(displayName="기타", ref="A1:C2"))
    second = workbook.create_sheet("둘째 시트")
    second["D7"] = "성명"
    second["E7"] = "생년월일"
    second["F7"] = "식종"
    second["D8"] = "둘째 아동"
    second["E8"] = "20160403"
    second["F8"] = "중식"
    second["D9"] = "표 밖 아동"
    second.add_table(Table(displayName="명단", ref="D7:F8"))
    workbook.save(path)
    workbook.close()

    wizard = RosterImportWizard()
    wizard.path_edit.setText(str(path))
    assert wizard._inspect_source()
    assert wizard.table_combo.count() == 2
    assert wizard.table_combo.currentData() == "명단"
    assert wizard.table_combo.currentText() == "명단"
    assert wizard.raw_table.rowCount() == 1
    assert wizard.raw_table.item(0, 0).text() == "둘째 아동"
    wizard._go_next()
    wizard._go_next()
    assert wizard.preview is not None and wizard.preview.can_continue
    wizard._go_back()
    wizard._go_back()
    wizard._cell_overrides[(8, "F")] = "조식"
    wizard.table_combo.setCurrentIndex(wizard.table_combo.findData("기타"))
    assert wizard.inspection is not None and wizard.inspection.selected_table == "기타"
    assert wizard.preview is None
    assert wizard._cell_overrides == {}
    assert wizard.raw_table.item(0, 0).text() == "첫 아동"
    wizard.close()


def test_wizard_can_select_valid_table_after_unsupported_default(app, tmp_path, monkeypatch):
    path = tmp_path / "unsupported-default.xlsx"
    workbook = openpyxl.Workbook()
    first = workbook.active
    first.append(("데이터",))
    first.append(("값",))
    first.add_table(Table(displayName="머리글없음", ref="A1:A2", headerRowCount=0))
    second = workbook.create_sheet("명단 시트")
    second.append(("성명", "식종"))
    second.append(("아동", "중식"))
    second.add_table(Table(displayName="다른표", ref="A1:B2"))
    workbook.save(path)
    workbook.close()

    original_open = workbook_module._open_workbook
    open_count = 0

    @contextmanager
    def counted_open(*args, **kwargs):
        nonlocal open_count
        open_count += 1
        with original_open(*args, **kwargs) as opened:
            yield opened

    monkeypatch.setattr(workbook_module, "_open_workbook", counted_open)

    wizard = RosterImportWizard()
    wizard.path_edit.setText(str(path))
    assert not wizard._inspect_source()
    assert open_count == 1
    assert "머리글이 없습니다" in wizard.error_label.text()
    assert wizard.table_combo.count() == 2
    wizard.table_combo.setCurrentIndex(wizard.table_combo.findData("다른표"))
    assert wizard.inspection is not None
    assert wizard.inspection.selected_table == "다른표"
    wizard.close()


def test_wizard_keeps_named_default_selected_when_its_layout_is_unsupported(app, tmp_path):
    path = tmp_path / "unsupported-named.xlsx"
    workbook = openpyxl.Workbook()
    first = workbook.active
    first.append(("성명", "식종"))
    first.append(("아동", "중식"))
    first.add_table(Table(displayName="다른표", ref="A1:B2"))
    second = workbook.create_sheet("머리글 없는 시트")
    second["A1"] = "첫 값"
    second["A2"] = "둘째 값"
    second.add_table(Table(displayName="명단", ref="A1:A2", headerRowCount=0))
    workbook.save(path)
    workbook.close()

    wizard = RosterImportWizard()
    wizard.path_edit.setText(str(path))
    assert not wizard._inspect_source()
    assert wizard.table_combo.currentData() == "명단"
    assert "머리글이 없습니다" in wizard.error_label.text()
    wizard.table_combo.setCurrentIndex(wizard.table_combo.findData("다른표"))
    assert wizard.inspection is not None and wizard.inspection.selected_table == "다른표"
    wizard.close()


def test_clean_import_validates_and_accepts_without_preview(
    app: QApplication,
    tmp_path: Path,
):
    path = _combined(tmp_path / "existing.xlsx")
    wizard = RosterImportWizard()
    wizard.path_edit.setText(str(path))

    assert wizard._inspect_source()
    assert wizard.inspection is not None
    assert wizard.next_button.isEnabled()
    assert wizard._mapping_combos[ProfileField.NAME].currentData() == "A"
    assert wizard._mapping_combos[ProfileField.BIRTH_DATE].currentData() == "B"
    assert ProfileField.CLASS_NAME not in wizard._mapping_combos
    assert ProfileField.PROVIDER not in wizard._mapping_combos
    assert wizard.meal_mode_combo.currentData() == "combined"
    assert wizard.combined_meal_combo.currentData() == "C"
    assert wizard._household_criterion_checkboxes[
        HouseholdMatchCriterion.MOBILE_PHONE
    ].isChecked()
    assert sum(
        checkbox.isChecked()
        for checkbox in wizard._household_criterion_checkboxes.values()
    ) == 1
    selected_household_button = wizard._household_criterion_checkboxes[
        HouseholdMatchCriterion.MOBILE_PHONE
    ]
    assert selected_household_button.text() == "핸드폰"
    assert not selected_household_button.icon().isNull()

    wizard._go_next()
    assert wizard.page_stack.currentIndex() == 1
    assert not hasattr(wizard, "provider_edit")
    wizard.password_edit.setText("temporary-only")
    wizard.effective_date_edit.setDate(QDate(2026, 8, 17))
    assert wizard.next_button.text() == "가져오기"
    assert wizard.page_stack.widget(1).isAncestorOf(wizard.effective_date_edit)
    wizard._go_next()

    assert wizard.page_stack.currentIndex() == 1
    assert wizard.password_edit.text() == ""
    assert wizard.preview is not None and wizard.preview.can_continue
    assert wizard.person_preview_table.rowCount() == 0
    accepted_selection = wizard.selection()
    wizard._go_next()
    assert wizard.selection() is accepted_selection

    assert wizard.result() == QDialog.DialogCode.Accepted
    selection = wizard.selection()
    assert selection.effective_on == date(2026, 8, 17)
    assert selection.preview.observations[0].meals == (Meal.BREAKFAST, Meal.LUNCH)
    assert selection.preview.profile.provider_value is None
    assert (
        selection.preview.observations[0].profile_values.get(ProfileField.PROVIDER)
        is None
    )
    assert "E" in selection.preview.profile.ignored_columns
    assert selection.household_rule.criteria == (
        HouseholdMatchCriterion.MOBILE_PHONE,
    )
    wizard.close()


def test_preview_error_can_be_fixed_and_rechecked_inside_the_wizard(
    app: QApplication,
    tmp_path: Path,
):
    path = _combined(tmp_path / "invalid.xlsx", meal="야식")
    wizard = RosterImportWizard()
    wizard.path_edit.setText(str(path))
    assert wizard._inspect_source()
    wizard._go_next()
    wizard._go_next()

    assert wizard.page_stack.currentIndex() == 2
    assert wizard.preview is not None and not wizard.preview.can_continue
    assert not wizard.next_button.isEnabled()
    assert not wizard.correction_launch_panel.isHidden()
    assert wizard.correction_table.rowCount() == 1
    assert wizard.correction_table.item(0, 3).text() == "야식"
    wizard._open_correction_dialog()
    app.processEvents()
    assert wizard.correction_dialog.isVisible()
    editor = wizard._correction_editors[(2, "C")]
    editor.setCurrentText("조식 / 중식")

    wizard.correction_apply_button.click()

    assert wizard.preview is not None and wizard.preview.can_continue
    assert wizard.next_button.isEnabled()
    assert wizard.preview.corrected_cells == ((2, "C"),)
    assert wizard.preview.observations[0].meals == (
        Meal.BREAKFAST,
        Meal.LUNCH,
    )
    assert wizard.correction_launch_panel.isHidden()
    assert not wizard.correction_dialog.isVisible()
    assert not wizard.correction_applied_panel.isHidden()
    unchanged = openpyxl.load_workbook(path, data_only=False)
    try:
        assert unchanged.active["C2"].value == "야식"
    finally:
        unchanged.close()

    assert wizard.result() == QDialog.DialogCode.Accepted
    assert wizard.selection().preview.corrected_cells == ((2, "C"),)
    assert wizard._cell_overrides == {}
    wizard.close()


def test_correction_dialog_fits_a_compact_screen_without_clipping_actions(
    app: QApplication,
    tmp_path: Path,
):
    path = _combined(tmp_path / "compact-invalid.xlsx", meal="야식")
    wizard = RosterImportWizard()
    wizard.resize(800, 540)
    wizard.show()
    wizard.path_edit.setText(str(path))

    assert wizard._inspect_source()
    wizard._go_next()
    wizard._go_next()
    app.processEvents()

    assert wizard.navigation_rail.isHidden()
    assert wizard.correction_dialog.isVisible()
    screen = wizard.correction_dialog.screen()
    assert screen is not None
    available = screen.availableGeometry()
    assert wizard.correction_dialog.width() <= available.width()
    assert wizard.correction_dialog.height() <= available.height()
    assert wizard.correction_table.horizontalScrollBar().maximum() == 0
    assert wizard.correction_apply_button.isVisible()
    button_top_left = wizard.correction_apply_button.mapTo(
        wizard.correction_dialog,
        QPoint(0, 0),
    )
    button_bottom_right = wizard.correction_apply_button.mapTo(
        wizard.correction_dialog,
        wizard.correction_apply_button.rect().bottomRight(),
    )
    assert wizard.correction_dialog.rect().contains(button_top_left)
    assert wizard.correction_dialog.rect().contains(button_bottom_right)
    wizard.close()


def test_correction_value_column_expands_for_long_values(
    app: QApplication,
    tmp_path: Path,
):
    path = tmp_path / "long-correction-value.xlsx"
    long_value = "잘못된생년월일값" * 8
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.append(("성명", "생년월일", "식종"))
    worksheet.append(("긴값아동", long_value, "조식"))
    _save_with_table(workbook, path)
    workbook.close()
    wizard = RosterImportWizard()
    wizard.show()
    wizard.path_edit.setText(str(path))

    assert wizard._inspect_source()
    wizard._go_next()
    wizard._go_next()
    app.processEvents()

    assert wizard.preview is not None and not wizard.preview.can_continue
    assert wizard.correction_dialog.isVisible()
    assert wizard.correction_table.columnWidth(5) > 190
    editor = wizard._correction_editors[(2, "B")]
    assert editor.width() > 180
    assert editor.text() == long_value
    wizard.close()


def test_unmapped_optional_birth_date_does_not_show_preview(
    app: QApplication,
    tmp_path: Path,
):
    path = tmp_path / "optional-birth-date.xlsx"
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.append(("성명", "식종"))
    worksheet.append(("생일미입력", "조식"))
    _save_with_table(workbook, path)
    workbook.close()
    wizard = RosterImportWizard()
    wizard.path_edit.setText(str(path))

    assert wizard._inspect_source()
    assert wizard._mapping_combos[ProfileField.BIRTH_DATE].currentData() is None
    wizard._go_next()
    wizard._go_next()

    assert wizard.page_stack.currentIndex() == 1
    assert wizard.preview is not None and wizard.preview.can_continue
    assert wizard.result() == QDialog.DialogCode.Accepted
    assert wizard.person_preview_table.rowCount() == 0
    assert wizard.selection().preview.observations[0].birth_date is None
    wizard.close()


def test_preview_explains_later_row_delivery_history_resolution(
    app: QApplication,
    tmp_path: Path,
):
    path = tmp_path / "move-history.xlsx"
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.append(("성명", "생년월일", "주소", "식종", "담당", "순서"))
    worksheet.append(("이사아동", "20150302", "이전 주소", "조식", "기사A", "001"))
    worksheet.append(("이사아동", "20150302", "현재 주소", "조식", "기사B", "009"))
    _save_with_table(workbook, path)
    workbook.close()
    profile = ImportMappingProfile(
        UUID("83000000-0000-0000-0000-000000000099"),
        1,
        "이사 전후 명단",
        "roster-combined-xlsx",
        {
            ProfileField.NAME: "A",
            ProfileField.BIRTH_DATE: "B",
            ProfileField.ROAD_ADDRESS: "C",
        },
        combined_meals_column="D",
        driver_column="E",
        sequence_column="F",
    )
    preview = parse_workbook(path, profile)
    wizard = RosterImportWizard()

    wizard._render_preview(preview)

    items = tuple(
        wizard.issue_list.item(row)
        for row in range(wizard.issue_list.count())
    )
    automatic = next(item for item in items if item.text().startswith("자동 정리"))
    assert "이사아동" in automatic.text()
    assert "3행을 현재값으로 사용" in automatic.text()
    assert "도로명주소" in automatic.toolTip()
    assert "조식 배송 담당" in automatic.toolTip()
    assert "조식 배송 순서" in automatic.toolTip()
    assert "적용일은 미상" in automatic.toolTip()
    wizard.close()


@pytest.mark.parametrize("purpose", [ImportPurpose.DATABASE, ImportPurpose.VACATION_LUNCH])
def test_excluded_blank_name_blocks_only_vacation_until_corrected(
    app: QApplication,
    tmp_path: Path,
    purpose: ImportPurpose,
):
    path = tmp_path / "blank-name.xlsx"
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.append(("성명", "생년월일", "식종"))
    worksheet.append((None, "20150302", "조식"))
    worksheet.append(("정상대상자", "20160403", "중식"))
    _save_with_table(workbook, path)
    workbook.close()
    wizard = RosterImportWizard(allow_vacation_lunch=True)
    wizard.purpose_combo.setCurrentIndex(wizard.purpose_combo.findData(purpose))
    wizard.path_edit.setText(str(path))

    assert wizard._inspect_source()
    wizard._go_next()
    wizard._go_next()

    assert wizard.preview is not None and wizard.preview.can_continue
    if purpose is ImportPurpose.DATABASE:
        assert wizard.result() == QDialog.DialogCode.Accepted
        assert wizard.page_stack.currentIndex() == 1
        assert len(wizard.selection().preview.observations) == 1
        wizard.close()
        return
    assert wizard.person_preview_table.rowCount() == 1
    assert wizard.page_stack.currentIndex() == 2
    assert not wizard.next_button.isEnabled()
    assert wizard.result() != QDialog.DialogCode.Accepted
    assert "오류" in wizard.correction_table.item(0, 0).text()
    editor = wizard._correction_editors[(2, "A")]
    editor.setText("추가대상자")
    wizard.correction_apply_button.click()

    assert wizard.preview is not None and wizard.preview.can_continue
    assert tuple(
        observation.name for observation in wizard.preview.observations
    ) == ("추가대상자", "정상대상자")
    assert wizard.preview.corrected_cells == ((2, "A"),)
    assert wizard.result() == QDialog.DialogCode.Accepted
    wizard.close()


def test_government_headers_get_reviewable_defaults_and_private_columns_ignored(
    app: QApplication,
    tmp_path: Path,
):
    path = _survey(tmp_path / "survey.xlsx")
    wizard = RosterImportWizard()
    wizard.path_edit.setText(str(path))

    assert wizard._inspect_source()
    assert wizard._build_profile().source_kind == "survey-xlsx"
    assert wizard.profile_name_edit.text() == "정부 조사표"
    assert wizard._mapping_combos[ProfileField.ROAD_ADDRESS].currentData() == "N"
    assert wizard.address_fallback_combo.currentData() == "M"
    assert wizard.breakfast_combo.currentData() == "S"
    assert wizard.lunch_combo.currentData() == "V"
    assert wizard.dinner_combo.currentData() == "T"
    assert wizard.breakfast_combo.isHidden()
    assert wizard.lunch_combo.isHidden()
    assert wizard.dinner_combo.isHidden()
    assert wizard.meal_mode_combo.isHidden()
    assert "자동 연결됨" in wizard.meal_detection_status.text()
    assert "조식 S열" in wizard.meal_detection_status.text()
    assert "조식: S · 연중 조식" in wizard.meal_detection_status.toolTip()
    assert wizard._build_profile().meal_flag_columns == {
        Meal.BREAKFAST: "S",
        Meal.LUNCH: "V",
        Meal.DINNER: "T",
    }
    wizard.meal_mapping_toggle.click()
    assert not wizard.breakfast_combo.isHidden()
    assert not wizard.meal_mode_combo.isHidden()
    checked = {
        item.data(256)
        for row in range(wizard.ignored_columns_list.count())
        if (item := wizard.ignored_columns_list.item(row)).checkState().value == 2
    }
    assert {"U", "AI"} <= checked
    wizard.close()


def test_password_entry_retries_the_protected_workbook_without_reload_click(
    app: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    path = _combined(tmp_path / "password-gated.xlsx")
    calls: list[str | None] = []

    def password_gate(source, *, password=None, **kwargs):
        calls.append(password)
        if not password:
            raise WorkbookImportPasswordRequiredError(
                "암호로 보호된 Excel입니다. 파일 비밀번호를 입력해주세요."
            )
        return inspect_workbook(source, **kwargs)

    monkeypatch.setattr(import_wizard_module, "inspect_workbook", password_gate)
    wizard = RosterImportWizard()
    wizard.path_edit.setText(str(path))

    assert not wizard._inspect_source()
    assert wizard.inspection is None
    assert "Enter" in wizard.file_status.text()
    assert wizard.inspect_button.text() == "비밀번호 확인하고 불러오기"

    wizard.password_edit.setText("excel-password")
    wizard.password_edit.editingFinished.emit()

    assert calls == ["", "excel-password"]
    assert wizard.inspection is not None
    assert wizard.inspect_button.text() == "표 미리보기"
    assert wizard.error_label.isHidden()
    wizard.close()


def test_password_enter_does_not_reopen_the_file_picker(
    app: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    path = _combined(tmp_path / "password-enter.xlsx")
    picker_calls: list[bool] = []

    def password_gate(source, *, password=None, **kwargs):
        if not password:
            raise WorkbookImportPasswordRequiredError(
                "암호로 보호된 Excel입니다. 파일 비밀번호를 입력해주세요."
            )
        return inspect_workbook(source, **kwargs)

    def unexpected_picker(*_args, **_kwargs):
        picker_calls.append(True)
        return "", ""

    monkeypatch.setattr(import_wizard_module, "inspect_workbook", password_gate)
    monkeypatch.setattr(
        import_wizard_module.QFileDialog,
        "getOpenFileName",
        unexpected_picker,
    )
    wizard = RosterImportWizard()
    wizard.show()
    wizard.path_edit.setText(str(path))

    assert not wizard._inspect_source()
    assert wizard.inspection is None
    wizard.password_edit.setText("excel-password")
    wizard.password_edit.setFocus()

    QTest.keyClick(wizard.password_edit, Qt.Key.Key_Return)
    app.processEvents()

    assert picker_calls == []
    assert wizard.inspection is not None
    assert wizard.page_stack.currentIndex() == 0
    wizard.close()


def test_back_from_preview_reuses_password_for_the_same_workbook(
    app: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    path = _combined(tmp_path / "password-gated-back.xlsx", meal="야식")
    password = "excel-password"
    parse_passwords: list[str | None] = []
    original_parse = import_wizard_module.parse_workbook

    def password_gate(source, *, password=None, **kwargs):
        if password != "excel-password":
            raise WorkbookImportPasswordRequiredError(
                "암호로 보호된 Excel입니다. 파일 비밀번호를 입력해주세요."
            )
        return inspect_workbook(source, **kwargs)

    def parse_gate(source, profile, *, password=None, **kwargs):
        parse_passwords.append(password)
        if password != "excel-password":
            raise WorkbookImportPasswordRequiredError(
                "암호로 보호된 Excel입니다. 파일 비밀번호를 입력해주세요."
            )
        return original_parse(source, profile, **kwargs)

    monkeypatch.setattr(import_wizard_module, "inspect_workbook", password_gate)
    monkeypatch.setattr(import_wizard_module, "parse_workbook", parse_gate)
    wizard = RosterImportWizard()
    wizard.path_edit.setText(str(path))
    wizard.password_edit.setText(password)

    assert wizard._inspect_source()
    wizard._go_next()
    wizard._go_next()
    assert wizard.page_stack.currentIndex() == 2
    assert wizard.password_edit.text() == ""

    wizard._go_back()
    assert wizard.page_stack.currentIndex() == 1
    wizard._go_next()

    assert wizard.page_stack.currentIndex() == 2
    assert parse_passwords == [password, password]
    assert wizard.error_label.isHidden()
    wizard.close()


def test_editing_a_proposed_profile_creates_next_version(
    app: QApplication,
    tmp_path: Path,
):
    path = _combined(tmp_path / "same-layout.xlsx")
    inspection = inspect_workbook(path)
    original = ImportMappingProfile(
        UUID("83000000-0000-0000-0000-000000000001"),
        4,
        "저장된 설정",
        "roster-xlsx",
        {
            ProfileField.NAME: "A",
            ProfileField.BIRTH_DATE: "B",
            ProfileField.MOBILE_PHONE: "D",
        },
        combined_meals_column="C",
        ignored_columns=("E",),
    )
    wizard = RosterImportWizard(
        existing_profiles=(StoredImportProfile(original, inspection.header_fingerprint),)
    )
    wizard.path_edit.setText(str(path))
    assert wizard._inspect_source()

    assert wizard.profile_name_edit.text() == "저장된 설정"
    assert wizard._build_profile().version == 4
    wizard.profile_name_edit.setText("저장된 설정 수정")
    assert wizard._build_profile().version == 5
    wizard.close()


def test_household_criteria_are_multi_select_and_existing_rule_is_versioned(
    app: QApplication,
):
    stored = StoredHouseholdMatchingRule(
        HouseholdMatchingRule(
            UUID("83000000-0000-0000-0000-000000000010"),
            3,
            (
                HouseholdMatchCriterion.MOBILE_PHONE,
                HouseholdMatchCriterion.HOUSEHOLD_HEAD_NAME,
            ),
        )
    )
    wizard = RosterImportWizard(existing_household_rule=stored)

    assert wizard._build_household_rule() == stored.rule
    wizard._household_criterion_checkboxes[
        HouseholdMatchCriterion.ADDRESS
    ].setChecked(True)
    changed = wizard._build_household_rule()

    assert changed.rule_id == stored.rule_id
    assert changed.version == 4
    assert changed.criteria == (
        HouseholdMatchCriterion.MOBILE_PHONE,
        HouseholdMatchCriterion.HOUSEHOLD_HEAD_NAME,
        HouseholdMatchCriterion.ADDRESS,
    )
    wizard.close()


def test_at_least_one_household_criterion_is_required(app: QApplication):
    wizard = RosterImportWizard()
    for checkbox in wizard._household_criterion_checkboxes.values():
        checkbox.setChecked(False)

    with pytest.raises(ValueError, match="하나 이상"):
        wizard._build_household_rule()
    wizard.close()


def test_meal_format_choice_only_shows_fields_for_the_selected_excel_shape(
    app: QApplication,
):
    wizard = RosterImportWizard()

    assert wizard.meal_mode_combo.currentData() == "flags"
    assert not wizard.breakfast_combo.isHidden()
    assert not wizard.true_values_edit.isHidden()
    assert wizard.combined_meal_combo.isHidden()
    with pytest.raises(ValueError, match="자동으로 찾지 못했습니다"):
        wizard._build_profile()

    wizard.meal_mode_combo.setCurrentIndex(
        wizard.meal_mode_combo.findData("combined")
    )
    app.processEvents()

    assert wizard.breakfast_combo.isHidden()
    assert wizard.true_values_edit.isHidden()
    assert not wizard.combined_meal_combo.isHidden()
    with pytest.raises(ValueError, match="조식 / 중식"):
        wizard._build_profile()
    wizard.close()


def test_workbook_loading_banner_keeps_the_window_responsive(
    app: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    path = _combined(tmp_path / "slow.xlsx")
    started = Event()
    release = Event()
    observed: list[tuple[bool, bool, str, bool]] = []
    original_inspect = inspect_workbook

    def delayed_inspect(*args, **kwargs):
        started.set()
        if not release.wait(2):
            raise AssertionError("UI timer did not run while the workbook was loading")
        return original_inspect(*args, **kwargs)

    monkeypatch.setattr(import_wizard_module, "inspect_workbook", delayed_inspect)
    wizard = RosterImportWizard()
    wizard.path_edit.setText(str(path))

    def observe_and_release() -> None:
        observed.append(
            (
                started.is_set(),
                not wizard.busy_panel.isHidden(),
                wizard.busy_title.text(),
                wizard.page_stack.isEnabled(),
            )
        )
        release.set()

    QTimer.singleShot(50, observe_and_release)

    assert wizard._inspect_source()
    assert observed == [(True, True, "Excel 파일을 여는 중입니다", False)]
    assert wizard.busy_panel.isHidden()
    assert wizard.page_stack.isEnabled()
    wizard.close()


def test_preview_loading_uses_its_own_clear_busy_message(
    app: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    path = _combined(tmp_path / "slow-preview.xlsx")
    wizard = RosterImportWizard()
    wizard.path_edit.setText(str(path))
    assert wizard._inspect_source()
    wizard._go_next()
    assert wizard.page_stack.currentIndex() == 1

    started = Event()
    release = Event()
    observed: list[tuple[bool, bool, str, bool]] = []
    original_parse = import_wizard_module.parse_workbook

    def delayed_parse(*args, **kwargs):
        started.set()
        if not release.wait(2):
            raise AssertionError("UI timer did not run while the preview was loading")
        return original_parse(*args, **kwargs)

    monkeypatch.setattr(import_wizard_module, "parse_workbook", delayed_parse)

    def observe_and_release() -> None:
        observed.append(
            (
                started.is_set(),
                not wizard.busy_panel.isHidden(),
                wizard.busy_title.text(),
                wizard.page_stack.isEnabled(),
            )
        )
        release.set()

    QTimer.singleShot(50, observe_and_release)
    wizard._go_next()

    assert observed == [(True, True, "가져올 명단을 만드는 중입니다", False)]
    assert wizard.page_stack.currentIndex() == 1
    assert wizard.result() == QDialog.DialogCode.Accepted
    assert wizard.busy_panel.isHidden()
    assert wizard.page_stack.isEnabled()
    wizard.close()


def test_closed_dropdown_does_not_change_with_the_mouse_wheel(app: QApplication):
    wizard = RosterImportWizard()
    combo = wizard.date_normalization_combo
    combo.setCurrentIndex(0)
    wheel = QWheelEvent(
        QPointF(5, 5),
        QPointF(5, 5),
        QPoint(),
        QPoint(0, -120),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.ScrollUpdate,
        False,
    )

    QApplication.sendEvent(combo, wheel)

    assert combo.currentIndex() == 0
    assert wheel.isAccepted()
    wizard.close()
