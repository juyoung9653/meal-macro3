"""Tests for src.core.settings — SettingsManager, get/set, defaults, deep_merge."""

import os
import sys

from src.core.settings import DEFAULTS, SettingsManager, get_settings

from .conftest import make_settings_manager


def test_settings_path_for_frozen_executable(monkeypatch, tmp_path):
    executable = tmp_path / "app.exe"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))
    assert SettingsManager()._get_path() == os.path.join(tmp_path, ".settings.json")


class TestTypedAccessors:
    def test_get_app_title(self):
        sm = make_settings_manager()
        assert sm.get_app_title() == "급식비 정산 프로그램"

    def test_get_app_organization(self):
        sm = make_settings_manager()
        assert sm.get_app_organization() == "분당구"

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

    def test_get_invoice_template_path(self):
        sm = make_settings_manager()
        assert sm.get_invoice_template_path() == "assets/청구서템플릿.xlsx"

    def test_get_confirm_template_path(self):
        sm = make_settings_manager()
        assert sm.get_confirm_template_path() == "assets/확인서템플릿.xlsx"

    def test_get_daily_delivery_template_path(self):
        sm = make_settings_manager()
        assert sm.get_daily_delivery_template_path() == "assets/일일배송템플릿.xlsx"

    def test_get_daily_delivery_lunch_template_path(self):
        sm = make_settings_manager()
        assert (
            sm.get_daily_delivery_lunch_template_path()
            == "assets/중식일일배송템플릿.xlsx"
        )

    def test_get_template_sheet_name(self):
        sm = make_settings_manager()
        assert sm.get_template_sheet_name() == "학기_총괄"

    def test_get_data_start_row(self):
        sm = make_settings_manager()
        assert sm.get_data_start_row() == 4

    def test_get_data_columns(self):
        sm = make_settings_manager()
        assert sm.get_data_columns() == 11

    def test_get_confirm_sheet_names(self):
        sm = make_settings_manager()
        assert sm.get_confirm_sheet_names() == ["1명", "2명", "3명", "4명", "5명"]

    def test_get_schedule_days_breakfast(self):
        sm = make_settings_manager()
        assert sm.get_schedule_days("breakfast") == {4, 5, 6}

    def test_get_schedule_days_dinner(self):
        sm = make_settings_manager()
        assert sm.get_schedule_days("dinner") == {0, 1, 2, 3}

    def test_get_schedule_days_lunch(self):
        sm = make_settings_manager()
        assert sm.get_schedule_days("lunch") == {0, 1, 2, 3, 4}

    def test_get_schedule_days_with_days_suffix(self):
        sm = make_settings_manager()
        assert sm.get_schedule_days("breakfast_days") == {4, 5, 6}

    def test_get_roster_name_keywords(self):
        sm = make_settings_manager()
        assert "성명" in sm.get_roster_name_keywords()
        assert "Name" in sm.get_roster_name_keywords()

    def test_get_flag_fields(self):
        sm = make_settings_manager()
        fields = sm.get_flag_fields()
        assert "__breakfast" in fields
        assert "__dinner" in fields

    def test_get_flag_search_terms(self):
        sm = make_settings_manager()
        terms = sm.get_flag_search_terms()
        assert "__breakfast" in terms
        assert terms["__breakfast"] == ["조식"]

    def test_get_confirm_title_suffix(self):
        sm = make_settings_manager()
        assert sm.get_confirm_title_suffix() == "분당구 급식확인서"

    def test_get_confirm_output_filename_format(self):
        sm = make_settings_manager()
        assert "{year:04d}" in sm.get_confirm_output_filename_format()
        assert "{month:02d}" in sm.get_confirm_output_filename_format()

    def test_get_confirm_col_widths(self):
        sm = make_settings_manager()
        cw = sm.get_confirm_col_widths()
        assert cw["G"] == 12.5
        assert cw["H"] == 12.5
        assert cw["I"] == 12.5
        assert cw["J"] == 12.5

    def test_get_invoice_output_filename_format(self):
        sm = make_settings_manager()
        assert "{year:04d}" in sm.get_invoice_output_filename_format()

    def test_get_daily_delivery_output_filename_format(self):
        sm = make_settings_manager()
        fmt = sm.get_daily_delivery_output_filename_format()
        assert "{start:%Y%m%d}" in fmt
        assert "{end:%Y%m%d}" in fmt

    def test_get_semantic_columns(self):
        sm = make_settings_manager()
        cols = sm.get_semantic_columns()
        assert "__calc_breakfast" in cols
        assert "__calc_total" in cols

    def test_accessor_overridden_value(self):
        sm = make_settings_manager()
        sm.set("app", "title", value="커스텀 타이틀")
        assert sm.get_app_title() == "커스텀 타이틀"

    def test_accessor_default_when_missing(self):
        sm = make_settings_manager()
        sm._data.pop("app", None)
        assert sm.get_app_title() == "급식비 정산 프로그램"


class TestSettingsDefaults:
    def test_defaults_are_complete(self):
        """DEFAULTS has expected top-level keys."""
        assert "app" in DEFAULTS
        assert "schedule" in DEFAULTS
        assert "templates" in DEFAULTS
        assert "confirm" in DEFAULTS
        assert "invoice" in DEFAULTS
        assert "daily_delivery" in DEFAULTS
        assert "roster" in DEFAULTS
        assert "semantic_columns" in DEFAULTS
        assert "flag_fields" in DEFAULTS
        assert "flag_search_terms" in DEFAULTS

    def test_default_app_title(self):
        assert DEFAULTS["app"]["title"] == "급식비 정산 프로그램"

    def test_default_schedule_keys(self):
        assert "breakfast_days" in DEFAULTS["schedule"]
        assert "dinner_days" in DEFAULTS["schedule"]
        assert "lunch_days" in DEFAULTS["schedule"]


class TestSettingsManagerGet:
    def test_get_single_key(self):
        sm = make_settings_manager()
        assert sm.get("app") is not None
        assert isinstance(sm.get("app"), dict)

    def test_get_nested_key(self):
        sm = make_settings_manager()
        assert sm.get("app", "title") == "급식비 정산 프로그램"

    def test_get_deep_nested(self):
        sm = make_settings_manager()
        assert sm.get("schedule", "breakfast_days") == [4, 5, 6]

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


class TestSettingsManagerGetAll:
    def test_get_all_returns_copy(self):
        sm = make_settings_manager()
        all_data = sm.get_all()
        assert all_data == sm._data
        # Mutating the copy should not affect the original
        all_data["app"]["title"] = "Hacked"
        assert sm.get("app", "title") != "Hacked"

    def test_get_all_contains_defaults(self):
        sm = make_settings_manager()
        all_data = sm.get_all()
        assert all_data["app"]["title"] == "급식비 정산 프로그램"


class TestSettingsManagerReset:
    def test_reset_to_defaults(self):
        sm = make_settings_manager()
        sm.set("app", "title", value="Custom")
        assert sm.get("app", "title") == "Custom"
        sm.reset_to_defaults()
        assert sm.get("app", "title") == "급식비 정산 프로그램"

    def test_reset_strips_custom_keys(self):
        sm = make_settings_manager()
        sm.set("custom_key", value="custom_value")
        sm.reset_to_defaults()
        assert sm.get("custom_key") is None


class TestDeepMerge:
    def test_merge_simple_key(self):
        sm = make_settings_manager()
        sm._deep_merge(sm._data, {"app": {"title": "Override"}})
        assert sm.get("app", "title") == "Override"

    def test_merge_preserves_unmentioned(self):
        sm = make_settings_manager()
        sm._deep_merge(sm._data, {"app": {"title": "Override"}})
        assert sm.get("app", "organization") == "분당구"

    def test_merge_adds_new_key(self):
        sm = make_settings_manager()
        sm._deep_merge(sm._data, {"new_section": {"key": "val"}})
        assert sm.get("new_section", "key") == "val"

    def test_merge_overwrites_non_dict(self):
        sm = make_settings_manager()
        merged = {"schedule": "not_a_dict"}
        # Before merge: schedule is a dict
        assert isinstance(sm._data["schedule"], dict)
        # After merge: schedule becomes the override value
        sm._deep_merge(sm._data, merged)
        assert sm._data["schedule"] == "not_a_dict"


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
    def test_save_and_load_roundtrip(self, tmp_path):
        """SettingsManager can save to a path and load back."""
        import os

        sm = make_settings_manager()
        sm.set("app", "title", value="Roundtrip Test")

        # Save to temp path by overriding _get_path
        test_path = os.path.join(tmp_path, ".settings.json")
        original_path = sm._path
        try:
            sm._path = test_path
            sm.save()
            assert os.path.exists(test_path)

            # Create a new manager and load from the same path
            sm2 = make_settings_manager()
            sm2._path = test_path
            sm2.load()
            assert sm2.get("app", "title") == "Roundtrip Test"
            # Other defaults preserved
            assert sm2.get("app", "organization") == "분당구"
        finally:
            sm._path = original_path
