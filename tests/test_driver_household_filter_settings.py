"""Persistence contract for driver-household header filters."""

import json
import os
from dataclasses import replace
from datetime import date
from uuid import uuid4

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QApplication

from src.desktop_v2.driver_households import DriverHouseholdsDialog
from src.desktop_v2.roster_window import RosterWindow
from src.domain.delivery_routing import Driver
from src.domain.roster import Meal
from src.services.driver_households import DriverHouseholdRow
from tests.test_desktop_v2_roster import _roster


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _settings(path):
    return QSettings(str(path), QSettings.Format.IniFormat)


def _rows():
    first, second = Driver(uuid4(), "기사A"), Driver(uuid4(), "기사B")
    household = uuid4()
    dinner = DriverHouseholdRow(
        household, first.driver_id, first.name, (uuid4(),), ("저녁 가구",),
        (Meal.DINNER,), "판교", ("010-1",), ("002",),
    )
    breakfast = replace(
        dinner,
        member_ids=(uuid4(),),
        member_names=("아침 가구",),
        meals=(Meal.BREAKFAST,),
        contacts=("010-2",),
        sequences=("001",),
    )
    merged = replace(
        dinner,
        member_ids=dinner.member_ids + breakfast.member_ids,
        member_names=dinner.member_names + breakfast.member_names,
        meals=(Meal.DINNER, Meal.BREAKFAST),
        contacts=dinner.contacts + breakfast.contacts,
        sequences=("002", "001"),
        meal_rows=(dinner, breakfast),
    )
    other = replace(
        dinner,
        household_key=uuid4(),
        driver_id=second.driver_id,
        driver_name=second.name,
        member_names=("다른 가구",),
        address="정자",
        sequences=("10",),
    )
    return (merged, other), (first, second)


def _dialog(path, key="driver_households/search_v1/db-a", **kwargs):
    rows, drivers = _rows()
    return DriverHouseholdsDialog(
        rows,
        service_day=date(2026, 10, 15),
        drivers=drivers,
        settings=_settings(path),
        settings_key=key,
        **kwargs,
    )


def test_reopen_restores_split_search_exact_values_and_explicit_empty(app, tmp_path):
    path = tmp_path / "filters.ini"
    first = _dialog(path)
    first.split_button.click()
    first._set_column_search("members", "아침")
    first._set_column_values("meals", {"조식"})
    first._set_column_values("contacts", set())
    first._set_column_values("address", {"현재 없는 주소"})
    first.close()

    restored = _dialog(path)
    try:
        assert restored._split_by_meal is True
        assert restored.split_button.text() == "분리 해제"
        assert restored._column_searches == {"members": "아침"}
        assert restored._column_values == {
            "meals": frozenset({"조식"}),
            "contacts": frozenset(),
            "address": frozenset({"현재 없는 주소"}),
        }
        assert restored.table.rowCount() == 0
        assert restored.clear_filters_button.isEnabled()
        assert restored._column_search_header.buttons["contacts"].property(
            "filterActive"
        ) is True
    finally:
        restored.close()


def test_clear_all_persists_and_preserves_split_sort_hidden_and_fixed_scope(app, tmp_path):
    path = tmp_path / "clear.ini"
    rows, drivers = _rows()
    dialog = DriverHouseholdsDialog(
        rows,
        service_day=date(2026, 10, 15),
        drivers=drivers,
        selected_driver_id=drivers[0].driver_id,
        rows_provider=lambda _include_hidden: rows,
        settings=_settings(path),
        settings_key="driver_households/search_v1/db-a",
    )
    dialog.split_button.click()
    dialog.table.sortItems(3, Qt.SortOrder.DescendingOrder)
    dialog.include_hidden_checkbox.setChecked(True)
    dialog._set_column_search("members", "없는 값")
    dialog._set_column_values("contacts", set())
    assert dialog.table.rowCount() == 0

    dialog.clear_filters_button.click()
    try:
        assert dialog._column_searches == {}
        assert dialog._column_values == {}
        assert dialog._split_by_meal is True
        assert dialog.include_hidden_checkbox.isChecked()
        assert dialog._selected_driver_id == drivers[0].driver_id
        assert dialog.table.horizontalHeader().sortIndicatorOrder() == Qt.SortOrder.DescendingOrder
        assert dialog.table.rowCount() == 2
        assert not dialog.clear_filters_button.isEnabled()
        assert all(
            button.property("filterActive") is False
            for button in dialog._column_search_header.buttons.values()
        )
    finally:
        dialog.close()

    restored = _dialog(path)
    try:
        assert restored._split_by_meal is True
        assert restored._column_searches == {}
        assert restored._column_values == {}
        assert not restored.include_hidden_checkbox.isChecked()
        assert restored.table.rowCount() == 3
    finally:
        restored.close()


def test_malformed_unknown_state_is_ignored_without_losing_valid_fields(app, tmp_path):
    path = tmp_path / "malformed.ini"
    key = "driver_households/search_v1/db-a"
    settings = _settings(path)
    settings.setValue(key, json.dumps({
        "split_by_meal": "yes",
        "columns": {"members": 7, "unknown": "x", "address": "  판교  "},
        "values": {"contacts": "bad", "unknown": [], "meals": ["조식", 3]},
        "include_hidden": True,
    }, ensure_ascii=False))
    settings.sync()

    dialog = _dialog(path, key=key)
    try:
        assert dialog._split_by_meal is False
        assert dialog._column_searches == {"address": "판교"}
        assert dialog._column_values == {}
        assert not dialog.include_hidden_checkbox.isChecked()
        assert dialog.table.rowCount() == 1
    finally:
        dialog.close()

    settings.setValue(key, "{broken")
    settings.sync()
    broken = _dialog(path, key=key)
    try:
        assert broken._column_searches == {}
        assert broken._column_values == {}
        assert broken.table.rowCount() == 2
    finally:
        broken.close()


def test_standalone_dialog_does_not_persist_filters(app):
    rows, drivers = _rows()
    first = DriverHouseholdsDialog(
        rows, service_day=date(2026, 10, 15), drivers=drivers
    )
    first.split_button.click()
    first._set_column_values("members", set())
    first.close()

    reopened = DriverHouseholdsDialog(
        rows, service_day=date(2026, 10, 15), drivers=drivers
    )
    try:
        assert reopened._split_by_meal is False
        assert reopened._column_values == {}
        assert reopened.table.rowCount() == 2
    finally:
        reopened.close()


def test_household_and_main_settings_are_sibling_scopes(app, tmp_path, monkeypatch):
    path = tmp_path / "scopes.ini"
    store = _settings(path)
    window = RosterWindow(
        _roster(), column_settings=store, view_settings_scope="db-a"
    )
    other = RosterWindow(
        _roster(), column_settings=_settings(path), view_settings_scope="db-b"
    )
    captured = []
    monkeypatch.setattr(
        DriverHouseholdsDialog,
        "exec",
        lambda dialog: captured.append(dialog),
    )
    try:
        assert window._driver_household_search_settings_key.startswith(
            "driver_households/search_v1/"
        )
        assert window._driver_household_search_settings_key != window._search_settings_key
        assert (
            window._driver_household_search_settings_key
            != other._driver_household_search_settings_key
        )

        window._show_driver_households()
        dialog = captured.pop()
        assert dialog._filter_settings is window._search_settings
        assert dialog._filter_settings_key == window._driver_household_search_settings_key

        store.setValue(window._search_settings_key, json.dumps({"global": "메인"}))
        dialog._set_column_search("members", "가구")
        dialog.clear_filters_button.click()
        assert store.contains(window._search_settings_key)
        assert not store.contains(window._driver_household_search_settings_key)

        dialog._set_column_values("members", set())
        window._save_search_filters()
        assert not store.contains(window._search_settings_key)
        assert store.contains(window._driver_household_search_settings_key)
    finally:
        for dialog in captured:
            dialog.close()
        window.close()
        other.close()
