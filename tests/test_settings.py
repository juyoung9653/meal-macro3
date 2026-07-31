"""Tests for src.core.settings — SettingsManager, get/set, defaults, deep_merge."""

import sys
from pathlib import Path

import pytest

from src.core.settings import DEFAULTS, SettingsManager, get_settings

from .conftest import make_settings_manager


class TestTypedAccessors:
    """Tests for typed accessors with logic beyond simple get."""

    def test_get_app_window_size(self):
        sm = make_settings_manager()
        w, h = sm.get_app_window_size()
        assert w == 1000
        assert h == 700

    def test_get_app_window_size_after_set(self):
        sm = make_settings_manager()
        sm.set("app", "window_width", value=1200)
        sm.set("app", "window_height", value=800)
        w, h = sm.get_app_window_size()
        assert w == 1200
        assert h == 800

    def test_get_meal_type_default(self):
        sm = make_settings_manager()
        assert sm.get_meal_type() == "급식"

    def test_get_meal_type_after_set(self):
        sm = make_settings_manager()
        sm.set_meal_type("행복도시락")
        assert sm.get_meal_type() == "행복도시락"

    def test_set_meal_type_syncs_template_paths(self):
        """Template paths now resolve lazily from meal type."""
        sm = make_settings_manager()
        sm.set_meal_type("행복도시락")
        assert sm.get_invoice_template_path() == "assets/행복도시락_청구서템플릿.xlsx"
        assert sm.get_confirm_template_path() == "assets/행복도시락_확인서템플릿.xlsx"

    def test_set_meal_type_restore_to_급식(self):
        sm = make_settings_manager()
        sm.set_meal_type("행복도시락")
        sm.set_meal_type("급식")
        assert sm.get_invoice_template_path() == "assets/급식_청구서템플릿.xlsx"
        assert sm.get_confirm_template_path() == "assets/급식_확인서템플릿.xlsx"

    def test_template_paths_use_pyinstaller_resource_root(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        sm = make_settings_manager()
        monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)

        assert sm.get_invoice_template_path() == str(
            tmp_path / "assets" / "급식_청구서템플릿.xlsx"
        )
        assert sm.get_confirm_template_path() == str(
            tmp_path / "assets" / "급식_확인서템플릿.xlsx"
        )
        assert sm.get_report_template_path() == str(
            tmp_path / "assets" / "report_template.xlsx"
        )


class TestDirectGet:
    """Direct s.get("section", "key") replaces deleted typed accessors."""

    def test_app_title(self):
        sm = make_settings_manager()
        assert sm.get("app", "title") == "급식비 정산 프로그램"

    def test_app_organization(self):
        sm = make_settings_manager()
        assert sm.get("app", "organization") == "분당구"

    def test_invoice_template_path(self):
        sm = make_settings_manager()
        assert sm.get_invoice_template_path() == "assets/급식_청구서템플릿.xlsx"

    def test_confirm_template_path(self):
        sm = make_settings_manager()
        assert sm.get_confirm_template_path() == "assets/급식_확인서템플릿.xlsx"

    def test_template_sheet_name(self):
        sm = make_settings_manager()
        assert sm.get("templates", "sheet_name") == "학기_총괄"

    def test_data_start_row(self):
        sm = make_settings_manager()
        assert sm.get("templates", "data_start_row") == 4

    def test_data_columns(self):
        sm = make_settings_manager()
        assert sm.get("templates", "data_columns") == 11

    def test_confirm_sheet_names(self):
        sm = make_settings_manager()
        assert sm.get("templates", "confirm_sheet_names") == [
            "1명",
            "2명",
            "3명",
            "4명",
            "5명",
        ]

    def test_confirm_title_suffix(self):
        sm = make_settings_manager()
        assert sm.get("confirm", "title_suffix") == "분당구 급식확인서"

    def test_confirm_output_filename_format(self):
        sm = make_settings_manager()
        fmt = sm.get("confirm", "output_filename_format")
        assert "{year:04d}" in fmt
        assert "{month:02d}" in fmt

    def test_invoice_output_filename_format(self):
        sm = make_settings_manager()
        assert "{year:04d}" in sm.get("invoice", "output_filename_format")

    def test_invoice_unit_price_defaults_are_canonical(self):
        sm = make_settings_manager()
        assert sm.get("invoice", "unit_prices") == {
            "조식": 9500,
            "중식": 9500,
            "석식": 9500,
        }
        assert "기타" not in sm.get("invoice", "unit_prices")

    def test_semantic_columns(self):
        sm = make_settings_manager()
        cols = sm.get("semantic_columns")
        assert "__calc_breakfast" in cols
        assert "__calc_total" in cols

    def test_annual_workbook_defaults(self):
        sm = make_settings_manager()
        assert sm.get("annual_workbook", "path") == ""
        assert sm.get("annual_workbook", "password") == ""
        assert "db" not in DEFAULTS
        assert "db_path" not in sm.get("delivery")

    def test_direct_get_overridden_value(self):
        sm = make_settings_manager()
        sm.set("app", "title", value="커스텀 타이틀")
        assert sm.get("app", "title") == "커스텀 타이틀"

    def test_direct_get_default_when_missing(self):
        sm = make_settings_manager()
        sm._data.pop("app", None)
        assert (
            sm.get("app", "title", default="급식비 정산 프로그램")
            == "급식비 정산 프로그램"
        )


class TestSettingsDefaults:
    def test_defaults_are_complete(self):
        """DEFAULTS has expected top-level keys."""
        assert "app" in DEFAULTS
        assert "templates" in DEFAULTS
        assert "confirm" in DEFAULTS
        assert "invoice" in DEFAULTS
        assert "annual_workbook" in DEFAULTS
        assert "semantic_columns" in DEFAULTS
        assert "db" not in DEFAULTS

    def test_default_app_title(self):
        assert DEFAULTS["app"]["title"] == "급식비 정산 프로그램"

    def test_default_meal_type(self):
        assert DEFAULTS["app"]["meal_type"] == "급식"

    def test_default_app_keys(self):
        """DEFAULTS['app'] has all required keys."""
        app = DEFAULTS["app"]
        for key in (
            "title",
            "organization",
            "window_width",
            "window_height",
            "meal_type",
        ):
            assert key in app, f"Missing key in DEFAULTS['app']: {key}"


class TestSettingsManagerGet:
    def test_get_single_key(self):
        sm = make_settings_manager()
        assert sm.get("app") is not None
        assert isinstance(sm.get("app"), dict)

    def test_get_nested_key(self):
        sm = make_settings_manager()
        assert sm.get("app", "title") == "급식비 정산 프로그램"

    def test_get_with_default(self):
        sm = make_settings_manager()
        assert sm.get("nonexistent", default="fallback") == "fallback"
        assert sm.get("app", "nonexistent", default=42) == 42

    def test_get_missing_key(self):
        sm = make_settings_manager()
        assert sm.get("nonexistent") is None

    def test_get_missing_nested(self):
        sm = make_settings_manager()
        assert sm.get("app", "nonexistent") is None


class TestSettingsManagerSet:
    def test_set_top_level(self):
        sm = make_settings_manager()
        sm.set("custom_key", value="custom_value")
        assert sm.get("custom_key") == "custom_value"

    def test_set_nested(self):
        sm = make_settings_manager()
        sm.set("app", "title", value="Custom Title")
        assert sm.get("app", "title") == "Custom Title"
        # Other keys unchanged
        assert sm.get("app", "organization") == "분당구"

    def test_set_creates_missing_path(self):
        sm = make_settings_manager()
        sm.set("new", "nested", "key", value="deep_value")
        assert sm.get("new", "nested", "key") == "deep_value"

    def test_set_overwrites_existing(self):
        sm = make_settings_manager()
        sm.set("app", "window_width", value=9999)
        assert sm.get("app", "window_width") == 9999


class TestSettingsSingleton:
    def test_get_settings_returns_same_instance(self):
        """get_settings() returns the same instance on repeated calls."""
        # The singleton is enforced via SettingsManager._instance
        SettingsManager._instance = None
        s1 = get_settings()
        s2 = get_settings()
        assert s1 is s2
        SettingsManager._instance = None  # cleanup

    def test_new_instance_is_different_from_singleton(self):
        """Direct SettingsManager() creates distinct instances."""
        SettingsManager._instance = None
        singleton = get_settings()
        fresh = SettingsManager()
        assert singleton is not fresh
        SettingsManager._instance = None

    def test_singleton_isolation(self):
        """Each test gets a fresh singleton due to conftest autouse reset."""
        sm1 = make_settings_manager()
        sm1.set("test_key", value="value1")
        sm2 = make_settings_manager()
        sm2.set("test_key", value="value2")
        assert sm1.get("test_key") == "value1"
        assert sm2.get("test_key") == "value2"


class TestSettingsSave:
    def test_no_persistence_methods(self):
        """save/load methods removed entirely."""
        sm = make_settings_manager()
        assert not hasattr(sm, "save")
        assert not hasattr(sm, "load")
