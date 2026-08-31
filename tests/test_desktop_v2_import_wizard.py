"""Guided first-run workbook route tests."""

from __future__ import annotations

import os
from threading import Event
from datetime import date
from pathlib import Path
from uuid import UUID

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import openpyxl
import pytest
from PySide6.QtCore import QDate, QPoint, QPointF, Qt, QTimer
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QApplication, QDialog

import src.desktop_v2.import_wizard as import_wizard_module
from src.domain import Meal
from src.desktop_v2 import RosterImportWizard
from src.importing import (
    HouseholdMatchCriterion,
    HouseholdMatchingRule,
    ImportMappingProfile,
    ProfileField,
    StoredHouseholdMatchingRule,
    StoredImportProfile,
    inspect_workbook,
)


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _combined(path: Path, meal: str = "조식 / 중식") -> Path:
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.title = "기존 명단"
    worksheet.append(("성명", "생년월일", "식종", "핸드폰", "내부 메모"))
    worksheet.append(("홍길동", "20150302", meal, "010-1234-5678", "복사 금지"))
    workbook.save(path)
    workbook.close()
    return path


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
    workbook.save(path)
    workbook.close()
    return path


def test_guided_route_reaches_selection_only_after_valid_preview(
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
    wizard.provider_edit.setText("분당구 급식센터")
    wizard.password_edit.setText("temporary-only")
    wizard._go_next()

    assert wizard.page_stack.currentIndex() == 2
    assert wizard.password_edit.text() == ""
    assert wizard.preview is not None and wizard.preview.can_continue
    assert wizard.person_preview_table.rowCount() == 1
    wizard.effective_date_edit.setDate(QDate(2026, 8, 17))
    wizard._go_next()

    assert wizard.result() == QDialog.DialogCode.Accepted
    selection = wizard.selection()
    assert selection.effective_on == date(2026, 8, 17)
    assert selection.preview.observations[0].meals == (Meal.BREAKFAST, Meal.LUNCH)
    assert selection.preview.profile.provider_value == "분당구 급식센터"
    assert (
        selection.preview.observations[0].profile_values[ProfileField.PROVIDER]
        == "분당구 급식센터"
    )
    assert "E" in selection.preview.profile.ignored_columns
    assert selection.household_rule.criteria == (
        HouseholdMatchCriterion.MOBILE_PHONE,
    )
    wizard.close()


def test_preview_error_keeps_final_review_button_disabled(
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
    assert wizard.issue_list.count() == 1
    assert "알 수 없는 식종" in wizard.issue_list.item(0).text()
    wizard.close()


def test_government_headers_get_reviewable_defaults_and_private_columns_ignored(
    app: QApplication,
    tmp_path: Path,
):
    path = _survey(tmp_path / "survey.xlsx")
    wizard = RosterImportWizard()
    wizard.path_edit.setText(str(path))
    wizard.header_row_spin.setValue(2)
    wizard.data_start_row_spin.setValue(3)

    assert wizard._inspect_source()
    assert wizard.source_kind_combo.currentData() == "survey-xlsx"
    assert wizard.profile_name_edit.text() == "정부 조사표"
    assert wizard._mapping_combos[ProfileField.ROAD_ADDRESS].currentData() == "N"
    assert wizard.address_fallback_combo.currentData() == "M"
    assert wizard.breakfast_combo.currentData() == "S"
    assert wizard.lunch_combo.currentData() == "V"
    assert wizard.dinner_combo.currentData() == "T"
    checked = {
        item.data(256)
        for row in range(wizard.ignored_columns_list.count())
        if (item := wizard.ignored_columns_list.item(row)).checkState().value == 2
    }
    assert {"U", "AI"} <= checked
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
    with pytest.raises(ValueError, match="1/0 열"):
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
    assert wizard.page_stack.currentIndex() == 2
    assert wizard.busy_panel.isHidden()
    assert wizard.page_stack.isEnabled()
    wizard.close()


def test_closed_dropdown_does_not_change_with_the_mouse_wheel(app: QApplication):
    wizard = RosterImportWizard()
    combo = wizard.source_kind_combo
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
