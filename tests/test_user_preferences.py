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


def test_db_header_mapping_round_trips(qsettings: QSettings):
    preferences = UserPreferences(qsettings=qsettings)
    mapping = {
        "식종": "급식 구분",
        "배송담당": "담당 기사",
        "연번": "배달 순서",
    }

    preferences.set_db_header_mapping(mapping)

    assert preferences.get_db_header_mapping() == mapping
    assert qsettings.value("annual_workbook/header_mapping") == mapping


def test_db_selected_year_round_trips_and_ignores_invalid_values(
    qsettings: QSettings,
):
    preferences = UserPreferences(qsettings=qsettings)

    assert preferences.get_db_selected_year() is None
    preferences.set_db_selected_year(2026)
    assert preferences.get_db_selected_year() == 2026
    assert qsettings.value("annual_workbook/selected_year") == 2026

    qsettings.setValue("annual_workbook/selected_year", "잘못된 값")
    assert preferences.get_db_selected_year() is None

    with pytest.raises(ValueError, match="1 이상의 정수"):
        preferences.set_db_selected_year(0)


def test_db_import_cache_is_scoped_by_header_structure(qsettings: QSettings):
    preferences = UserPreferences(qsettings=qsettings)
    first_headers = ("성명", "급식 구분", "담당 기사", "배달 순서")
    second_headers = ("성명", "식종", "배송담당", "연번")
    mapping = {
        "식종": "급식 구분",
        "배송담당": "담당 기사",
        "연번": "배달 순서",
    }

    assert preferences.get_cached_db_import(first_headers) is None
    preferences.set_cached_db_import(first_headers, mapping, 2026)

    assert preferences.get_cached_db_import(first_headers) == (mapping, 2026)
    assert preferences.get_cached_db_import(second_headers) is None
    assert isinstance(
        qsettings.value("annual_workbook/header_mapping_cache"),
        str,
    )


def test_db_import_cache_ignores_malformed_data(qsettings: QSettings):
    preferences = UserPreferences(qsettings=qsettings)
    headers = ("성명", "식종", "배송담당")
    qsettings.setValue("annual_workbook/header_mapping_cache", "{broken")

    assert preferences.get_cached_db_import(headers) is None


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
