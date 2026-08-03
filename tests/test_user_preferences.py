"""Compatibility tests for the QSettings-backed user-preference facade."""

from __future__ import annotations

from pathlib import Path

import pytest
from PyQt6.QtCore import QSettings

from src.core.user_preferences import UserPreferences


@pytest.fixture
def qsettings(tmp_path: Path):
    previous_format = QSettings.defaultFormat()
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(
        QSettings.Format.IniFormat,
        QSettings.Scope.UserScope,
        str(tmp_path / "settings"),
    )
    settings = QSettings("분당구", "급식비 정산 프로그램")
    settings.clear()
    yield settings
    settings.clear()
    QSettings.setDefaultFormat(previous_format)


def test_meal_type_uses_existing_key_and_falls_back_for_unknown_values(qsettings: QSettings):
    qsettings.setValue("meal_type", "알수없음")
    preferences = UserPreferences(qsettings=qsettings)

    assert preferences.get_meal_type() == "급식"
    assert preferences.set_meal_type("행복도시락") == "행복도시락"
    assert qsettings.value("meal_type") == "행복도시락"


def test_shared_paths_use_existing_keys(qsettings: QSettings):
    preferences = UserPreferences(qsettings=qsettings)

    preferences.set_output_dir("C:/exports")
    preferences.set_annual_workbook_path("C:/annual.xlsx")

    assert preferences.get_output_dir() == "C:/exports"
    assert preferences.get_annual_workbook_path() == "C:/annual.xlsx"
    assert qsettings.value("output_dir") == "C:/exports"
    assert qsettings.value("annual_workbook/last_path") == "C:/annual.xlsx"


def test_invoice_mapping_round_trips_existing_key(qsettings: QSettings):
    preferences = UserPreferences(qsettings=qsettings)
    mapping = {"성명": "이름", "주소": "도로명주소"}

    preferences.set_invoice_column_mapping(mapping)

    assert preferences.get_invoice_column_mapping() == mapping
    assert qsettings.value("invoice/col_mapping") == mapping


def test_staff_order_reads_legacy_list_and_writes_json_string(qsettings: QSettings):
    qsettings.setValue("report/staff_order_조식", ["기사B", "기사A"])
    preferences = UserPreferences(qsettings=qsettings)

    assert preferences.get_staff_order("조식") == ["기사B", "기사A"]
    preferences.set_staff_order("중식", ["기사C"])

    assert preferences.get_staff_order("중식") == ["기사C"]
    assert qsettings.value("report/staff_order_중식") == '["기사C"]'
