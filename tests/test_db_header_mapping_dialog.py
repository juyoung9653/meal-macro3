"""db 필수 헤더 수동 매핑 대화상자 테스트."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtWidgets import QApplication, QDialog

from src.excel.annual_workbook import (
    HEADER_MAPPING_ROLES,
    PROFILE_HEADER_ROLES,
    REQUIRED_HEADER_ROLES,
)
from src.widgets.db_header_mapping_dialog import DbHeaderMappingDialog


@pytest.fixture
def qapp():
    return QApplication.instance() or QApplication([])


def test_dialog_selects_custom_headers_by_clickable_combos(qapp: QApplication):
    mapping = {
        "식종": "급식 구분",
        "배송담당": "담당 기사",
        "연번": "배달 순서",
    }
    dialog = DbHeaderMappingDialog(
        [
            *PROFILE_HEADER_ROLES,
            "급식 구분",
            "담당 기사",
            "배달 순서",
            "20260728",
        ],
        current_mapping=mapping,
    )
    try:
        assert dialog.save_button.isEnabled()
        assert dialog.year_combo.currentData() == 2026
        assert all(
            dialog._combos[role].currentData() == header
            for role, header in mapping.items()
        )
        assert dialog._combos["식종"].findData("20260728") == -1

        dialog._on_save()

        assert dialog.result() == QDialog.DialogCode.Accepted
        assert all(
            dialog.selected_mapping[role] == header
            for role, header in mapping.items()
        )
        assert dialog.selected_mapping["성명"] == "성명"
        assert set(dialog.selected_mapping) == set(HEADER_MAPPING_ROLES)
        assert dialog.selected_year == 2026
    finally:
        dialog.close()


def test_dialog_auto_selects_unique_partial_meal_header(qapp: QApplication):
    dialog = DbHeaderMappingDialog(
        [
            *PROFILE_HEADER_ROLES,
            "2026년 식종 구분",
            "배송담당",
            "20260728",
        ]
    )
    try:
        assert dialog._combos["식종"].currentData() == "2026년 식종 구분"
        assert dialog.save_button.isEnabled()
    finally:
        dialog.close()


def test_dialog_does_not_guess_ambiguous_partial_meal_header(
    qapp: QApplication,
):
    dialog = DbHeaderMappingDialog(
        [
            *PROFILE_HEADER_ROLES,
            "평일 식종",
            "방학 식종",
            "배송담당",
            "20260728",
        ]
    )
    try:
        assert dialog._combos["식종"].currentData() == ""
        assert not dialog.save_button.isEnabled()
        assert "식종" in dialog.validation_label.text()
    finally:
        dialog.close()


def test_dialog_allows_separated_reordered_headers_but_requires_distinct_choices(
    qapp: QApplication,
):
    dialog = DbHeaderMappingDialog(
        [
            "배달 순서",
            *PROFILE_HEADER_ROLES,
            "급식 구분",
            "담당 기사",
            "20260728",
        ]
    )
    try:
        for role, header in {
            "식종": "급식 구분",
            "배송담당": "담당 기사",
            "연번": "배달 순서",
        }.items():
            combo = dialog._combos[role]
            combo.setCurrentIndex(combo.findData(header))

        assert dialog.save_button.isEnabled()
        assert "적용할 수 있습니다" in dialog.validation_label.text()
        assert dialog._combos["연번"].currentData() == "배달 순서"

        dialog._combos["연번"].setCurrentIndex(0)
        assert dialog.save_button.isEnabled()

        dialog._combos["배송담당"].setCurrentIndex(
            dialog._combos["배송담당"].findData("급식 구분")
        )
        assert not dialog.save_button.isEnabled()
        assert "서로 다른" in dialog.validation_label.text()
    finally:
        dialog.close()


def test_dialog_requires_year_choice_when_db_contains_multiple_years(
    qapp: QApplication,
):
    dialog = DbHeaderMappingDialog(
        [*PROFILE_HEADER_ROLES, "식종", "배송담당", "20251231", "20260101"]
    )
    try:
        assert dialog.year_combo.currentData() is None
        assert not dialog.save_button.isEnabled()
        assert "2025, 2026" in dialog.validation_label.text()

        dialog.year_combo.setCurrentIndex(dialog.year_combo.findData(2026))

        assert dialog.save_button.isEnabled()
        dialog._on_save()
        assert dialog.selected_year == 2026
        assert dialog.selected_mapping["연번"] == ""
    finally:
        dialog.close()


def test_dialog_maps_required_profile_fields_to_custom_headers(
    qapp: QApplication,
):
    dialog = DbHeaderMappingDialog(
        [
            *PROFILE_HEADER_ROLES,
            "아동 이름",
            "재학 학교",
            "급식",
            "기사",
            "20260728",
        ],
        current_mapping={
            "식종": "급식",
            "배송담당": "기사",
            "연번": "",
            "성명": "아동 이름",
            "학교명": "재학 학교",
        },
    )
    try:
        assert dialog.save_button.isEnabled()
        assert dialog._combos["성명"].currentData() == "아동 이름"
        assert dialog._combos["학교명"].currentData() == "재학 학교"
        assert dialog._combos["연번"].currentData() == ""
    finally:
        dialog.close()


def test_dialog_requires_all_invoice_and_confirmation_profile_mappings(
    qapp: QApplication,
):
    dialog = DbHeaderMappingDialog(
        [*PROFILE_HEADER_ROLES, "식종", "배송담당", "20260728"]
    )
    try:
        assert set(REQUIRED_HEADER_ROLES) == {
            "식종",
            "배송담당",
            *PROFILE_HEADER_ROLES,
        }
        assert dialog.save_button.isEnabled()
        assert dialog._combos["연번"].currentData() == ""

        dialog._combos["성명"].setCurrentIndex(0)

        assert not dialog.save_button.isEnabled()
        assert "성명" in dialog.validation_label.text()
        assert "필수 필드" in dialog.validation_label.text()
    finally:
        dialog.close()
