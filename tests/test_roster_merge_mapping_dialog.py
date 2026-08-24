"""최신 명단 Excel 열 문자 매핑 대화상자 테스트."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import call, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtWidgets import QApplication, QDialog, QLineEdit, QMessageBox

from src.excel.annual_workbook import WorkbookPasswordRequiredError
from src.services.roster_merge_service import MERGE_MEAL_ROLE, SourceLayout
from src.widgets.roster_merge_mapping_dialog import RosterMergeMappingDialog
from src.widgets.roster_merge_tab import RosterMergeTab


@pytest.fixture
def qapp():
    return QApplication.instance() or QApplication([])


def test_dialog_maps_one_row_roster_by_excel_column_letters(qapp: QApplication):
    layout = SourceLayout(
        sheet_name="신규명단",
        header_row=3,
        headers=("연번", "이름", "생년월일", "급식 유형"),
    )
    dialog = RosterMergeMappingDialog(layout)
    try:
        name_combo = dialog._combos["성명"]
        birth_combo = dialog._combos["생년월일"]
        meal_combo = dialog._combos[MERGE_MEAL_ROLE]

        assert name_combo.itemText(name_combo.findData(2)) == "B · 이름"
        assert birth_combo.currentData() == 3
        assert birth_combo.currentText() == "C · 생년월일"
        assert meal_combo.currentData() == 4
        assert meal_combo.currentText() == "D · 급식 유형"
        assert not dialog.apply_button.isEnabled()

        name_combo.setCurrentIndex(name_combo.findData(2))
        assert dialog.apply_button.isEnabled()
        dialog._accept_selection()

        assert dialog.result() == QDialog.DialogCode.Accepted
        assert dialog._result is not None
        assert dialog._result["성명"] == 2
        assert dialog._result["생년월일"] == 3
        assert dialog._result[MERGE_MEAL_ROLE] == 4
    finally:
        dialog.close()


def test_dialog_rejects_using_same_excel_column_twice(qapp: QApplication):
    layout = SourceLayout(
        sheet_name="신규명단",
        header_row=1,
        headers=("성명", "생년월일", "식종"),
    )
    dialog = RosterMergeMappingDialog(layout)
    try:
        assert dialog.apply_button.isEnabled()
        name_combo = dialog._combos["성명"]
        name_combo.setCurrentIndex(name_combo.findData(2))

        assert not dialog.apply_button.isEnabled()
        assert "서로 다른 Excel 열" in dialog.validation_label.text()
    finally:
        dialog.close()


def test_merge_window_chooses_source_with_file_explorer_then_maps_columns(
    qapp: QApplication,
    tmp_path: Path,
):
    target = tmp_path / "db.xlsx"
    source = tmp_path / "신규 명단.xlsx"
    layout = SourceLayout(
        sheet_name="명단",
        header_row=1,
        headers=("이름", "생일", "급식"),
    )
    columns = {"성명": 1, "생년월일": 2, MERGE_MEAL_ROLE: 3}
    tab = RosterMergeTab()
    try:
        tab.set_target(str(target), {"성명": "성명"})
        with (
            patch(
                "src.widgets.roster_merge_tab.QFileDialog.getOpenFileName",
                return_value=(str(source), "Excel 통합 문서"),
            ) as file_dialog,
            patch(
                "src.widgets.roster_merge_tab.read_source_layout",
                return_value=layout,
            ) as read_layout,
            patch(
                "src.widgets.roster_merge_tab.RosterMergeMappingDialog.get_selection",
                return_value=columns,
            ) as choose_columns,
        ):
            tab.source_button.click()

        file_dialog.assert_called_once()
        read_layout.assert_called_once_with(source, password="")
        choose_columns.assert_called_once_with(layout, parent=tab)
        assert tab._source_path == source
        assert tab._source_columns == columns
        assert tab.merge_button.isEnabled()
        assert "성명=이름" in tab.mapping_label.text()
    finally:
        tab.close()


def test_merge_window_prompts_masked_password_for_encrypted_source(
    qapp: QApplication,
    tmp_path: Path,
):
    target = tmp_path / "db.xlsx"
    source = tmp_path / "암호 명단.xlsx"
    layout = SourceLayout(
        sheet_name="명단",
        header_row=1,
        headers=("이름", "생일", "급식"),
    )
    columns = {"성명": 1, "생년월일": 2, MERGE_MEAL_ROLE: 3}
    tab = RosterMergeTab()
    try:
        tab.set_target(str(target), {"성명": "성명"})
        with (
            patch(
                "src.widgets.roster_merge_tab.QFileDialog.getOpenFileName",
                return_value=(str(source), "Excel 통합 문서"),
            ),
            patch(
                "src.widgets.roster_merge_tab.read_source_layout",
                side_effect=[WorkbookPasswordRequiredError(), layout],
            ) as read_layout,
            patch(
                "src.widgets.roster_merge_tab.QInputDialog.getText",
                return_value=("source-secret", True),
            ) as password_prompt,
            patch(
                "src.widgets.roster_merge_tab.RosterMergeMappingDialog.get_selection",
                return_value=columns,
            ),
        ):
            tab.source_button.click()

        assert read_layout.call_args_list == [
            call(source, password=""),
            call(source, password="source-secret"),
        ]
        assert password_prompt.call_args.args[3] is QLineEdit.EchoMode.Password
        assert tab._source_password == "source-secret"
        visible_text = tab.source_label.text() + tab.mapping_label.text()
        assert "source-secret" not in visible_text
    finally:
        tab.close()


def test_merge_confirmation_cancel_forgets_source_password(
    qapp: QApplication,
    tmp_path: Path,
):
    tab = RosterMergeTab()
    try:
        tab._target_path = tmp_path / "db.xlsx"
        tab._source_path = tmp_path / "암호 명단.xlsx"
        tab._source_layout = SourceLayout("명단", 1, ("이름", "생일", "급식"))
        tab._source_columns = {"성명": 1, "생년월일": 2, MERGE_MEAL_ROLE: 3}
        tab._source_password = "source-secret"
        tab._source_requires_password = True

        with patch(
            "src.widgets.roster_merge_tab.QMessageBox.question",
            return_value=QMessageBox.StandardButton.No,
        ):
            tab._run_merge()

        assert tab._source_password == ""
    finally:
        tab.close()
