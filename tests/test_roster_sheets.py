"""Named roster sheets are local layouts over one live roster model."""

from dataclasses import replace
from datetime import date
import json
from uuid import uuid4

import pytest
from PySide6.QtCore import QItemSelectionModel, QSettings, Qt
from PySide6.QtWidgets import QApplication, QToolButton

from src.desktop_v2.column_layout import RosterColumnLayout
from src.desktop_v2.roster_window import RosterWindow
from src.domain import CustomFieldDefinition, CustomFieldType
from tests.test_desktop_v2_roster import _roster


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


def _settings(path):
    return QSettings(str(path), QSettings.Format.IniFormat)


def _column(window, key):
    return next(index for index, column in enumerate(window.table_model.columns) if column.key == key)


def _visible(window, key):
    return not window._table.isColumnHidden(_column(window, key))


def test_add_sheet_button_tracks_tabs_after_rename_delete_and_resize(app, tmp_path):
    window = RosterWindow(_roster(), column_settings=_settings(tmp_path / "sheets.ini"))
    add_button = window.findChild(QToolButton, "addRosterSheet")
    assert add_button is not None
    window.show()

    def assert_adjacent():
        app.processEvents()
        tabs = window._sheet_tabs
        assert add_button.isVisible()
        assert tabs.geometry().right() < add_button.geometry().left()
        assert add_button.geometry().left() - tabs.geometry().right() <= 12
        if tabs.sizeHint().width() <= tabs.width():
            last_tab_right = tabs.mapTo(tabs.parentWidget(), tabs.tabRect(tabs.count() - 1).topRight()).x()
            assert add_button.geometry().left() - last_tab_right <= 12
        assert add_button.mapTo(window, add_button.rect().bottomRight()).x() < window.width()

    for size in ((1080, 680), (1440, 860)):
        window.resize(*size)
        assert_adjacent()
        window._add_sheet("연락", {"name", "address"})
        assert_adjacent()
        window._rename_sheet(1, "연락처 및 상세 주소")
        assert_adjacent()
        window._delete_sheet(1)
        assert_adjacent()

    for number in range(8):
        window._add_sheet(f"매우 긴 시트 이름 {number}", {"name"})
    window.resize(1080, 680)
    assert_adjacent()
    assert window._sheet_tabs.sizeHint().width() > window._sheet_tabs.width()
    window._sheet_tabs.setCurrentIndex(window._sheet_tabs.count() - 1)
    assert_adjacent()
    window.close()


def test_sheets_share_model_and_column_can_appear_twice(app, tmp_path):
    roster = _roster()
    window = RosterWindow(roster, column_settings=_settings(tmp_path / "sheets.ini"), view_settings_scope="db-a")
    model = window.table_model
    window._add_sheet("배송", {"name", "address", "status:lunch"})
    assert window._table.model() is model
    assert _visible(window, "address") and not _visible(window, "birth_date")
    window._sheet_tabs.setCurrentIndex(0)
    assert _visible(window, "address") and _visible(window, "birth_date")
    window._sheet_tabs.setCurrentIndex(1)
    updated = replace(roster, revision_id=uuid4())
    window.replace_roster(updated)
    assert window.roster is updated and window.table_model is model
    assert window._sheet_tabs.tabText(1) == "배송"
    assert _visible(window, "address") and not _visible(window, "birth_date")
    window.close()


def test_sheet_layouts_and_active_tab_persist_with_database_scope(app, tmp_path):
    path = tmp_path / "sheets.ini"
    roster = _roster()
    first = RosterWindow(roster, column_settings=_settings(path), view_settings_scope="db-a")
    first._add_sheet("연락", {"name", "address"})
    header = first._table.horizontalHeader()
    address = _column(first, "address")
    header.moveSection(header.visualIndex(address), 0)
    header.resizeSection(address, 307)
    first._rename_sheet(1, "주소")
    with pytest.raises(ValueError, match="같은 이름"):
        first._rename_sheet(1, "명단")
    first.close()

    restored = RosterWindow(roster, column_settings=_settings(path), view_settings_scope="db-a")
    assert restored._sheet_tabs.currentIndex() == 1
    assert restored._sheet_tabs.tabText(1) == "주소"
    assert restored._table.horizontalHeader().logicalIndex(0) == _column(restored, "address")
    assert restored._table.horizontalHeader().sectionSize(_column(restored, "address")) == 307
    assert not _visible(restored, "birth_date")
    restored._sheet_tabs.setCurrentIndex(0)
    assert _visible(restored, "birth_date")
    restored.close()

    other = RosterWindow(roster, column_settings=_settings(path), view_settings_scope="db-b")
    assert other._sheet_tabs.count() == 1
    assert other._sheet_tabs.tabText(0) == "명단"
    other.close()


def test_new_custom_column_goes_to_main_only_and_special_width_stays_per_sheet(app, tmp_path):
    path = tmp_path / "sheets.ini"
    roster = _roster()
    window = RosterWindow(roster, column_settings=_settings(path), view_settings_scope="db-a")
    window._add_sheet("연락", {"name", "address"})
    custom = CustomFieldDefinition(uuid4(), "보조 항목", CustomFieldType.TEXT, len(roster.custom_fields))
    updated = replace(roster, revision_id=uuid4(), custom_fields=roster.custom_fields + (custom,))
    window.replace_roster(updated)
    key = f"custom:{custom.definition_id}"
    assert not _visible(window, key)
    window._sheet_tabs.setCurrentIndex(0)
    assert _visible(window, key)
    window._special_only_filter.setChecked(True)
    window._table.horizontalHeader().resizeSection(_column(window, "delivery_settings"), 301)
    window._sheet_tabs.setCurrentIndex(1)
    window._table.horizontalHeader().resizeSection(_column(window, "delivery_settings"), 365)
    window._special_only_filter.setChecked(False)
    window._special_only_filter.setChecked(True)
    assert window._table.horizontalHeader().sectionSize(_column(window, "delivery_settings")) == 365
    window._sheet_tabs.setCurrentIndex(0)
    assert window._table.horizontalHeader().sectionSize(_column(window, "delivery_settings")) == 301
    window.close()


def test_switch_clears_batch_cell_targets_and_delete_only_removes_view(app, tmp_path):
    roster = _roster()
    window = RosterWindow(roster, column_settings=_settings(tmp_path / "sheets.ini"))
    window._set_day(date(2026, 10, 16))
    lunch = _column(window, "status:lunch")
    selection = window._table.selectionModel()
    selection.setCurrentIndex(
        window.table_model.index(0, lunch), QItemSelectionModel.SelectionFlag.ClearAndSelect
    )
    assert window.selected_meal_targets()
    window._add_sheet("연락", {"name", "address"})
    assert window.selected_meal_targets() == ()
    assert window._selected_index().isValid()
    assert window._model.sort_criteria == ()
    window._delete_sheet(1)
    assert window._sheet_tabs.count() == 1
    assert window.roster is roster
    with pytest.raises(ValueError, match="마지막"):
        window._delete_sheet(0)
    window.close()


def test_reordered_tabs_and_malformed_preferences(app, tmp_path):
    path = tmp_path / "sheets.ini"
    roster = _roster()
    window = RosterWindow(roster, column_settings=_settings(path), view_settings_scope="db-a")
    window._add_sheet("주소 확인", {"name", "address"})
    window._sheet_tabs.moveTab(1, 0)
    assert window._sheet_tabs.tabText(0) == "주소 확인"
    assert window._active_sheet_id == window._sheets[0][0]
    key = window._sheet_settings_key
    window.close()

    reopened = RosterWindow(roster, column_settings=_settings(path), view_settings_scope="db-a")
    assert reopened._sheet_tabs.tabText(0) == "주소 확인"
    assert reopened._sheet_tabs.currentIndex() == 0
    reopened.close()

    store = _settings(path)
    store.setValue(key, json.dumps({"sheets": [{"id": "../bad", "name": "오류"}], "active": "../bad"}))
    store.sync()
    recovered = RosterWindow(roster, column_settings=_settings(path), view_settings_scope="db-a")
    assert recovered._sheet_tabs.count() == 1
    assert recovered._sheet_tabs.tabText(0) == "명단"
    recovered.close()


def test_initial_sheet_imports_legacy_layout_without_removing_it(app, tmp_path):
    path = tmp_path / "legacy.ini"
    store = _settings(path)
    store.setValue(
        RosterColumnLayout.SETTINGS_KEY,
        json.dumps({"order": ["address", "name"], "hidden": ["school"], "widths": {"address": 289}}),
    )
    store.sync()
    window = RosterWindow(_roster(), column_settings=_settings(path), view_settings_scope="db-a")
    assert window._table.horizontalHeader().logicalIndex(0) == _column(window, "address")
    assert window._table.isColumnHidden(_column(window, "school"))
    assert window._table.horizontalHeader().sectionSize(_column(window, "address")) == 289
    assert _settings(path).contains(RosterColumnLayout.SETTINGS_KEY)
    assert _settings(path).contains(window._sheet_layout_key("main"))
    window.close()


def test_deleted_main_transfers_new_custom_column_default_across_reorder_and_reopen(app, tmp_path):
    path = tmp_path / "sheets.ini"
    roster = _roster()
    window = RosterWindow(roster, column_settings=_settings(path), view_settings_scope="db-a")
    window._add_sheet("주소", {"name", "address"})
    window._add_sheet("학교", {"name", "school"})
    window._delete_sheet(0)
    first = CustomFieldDefinition(uuid4(), "새 항목 1", CustomFieldType.TEXT, len(roster.custom_fields))
    updated = replace(roster, revision_id=uuid4(), custom_fields=roster.custom_fields + (first,))
    window.replace_roster(updated)
    key1 = f"custom:{first.definition_id}"
    window._sheet_tabs.setCurrentIndex(0)
    assert _visible(window, key1)
    window._sheet_tabs.setCurrentIndex(1)
    assert not _visible(window, key1)

    window._sheet_tabs.moveTab(1, 0)
    second = CustomFieldDefinition(uuid4(), "새 항목 2", CustomFieldType.TEXT, len(updated.custom_fields))
    updated = replace(updated, revision_id=uuid4(), custom_fields=updated.custom_fields + (second,))
    window.replace_roster(updated)
    key2 = f"custom:{second.definition_id}"
    assert _visible(window, key2)
    window._sheet_tabs.setCurrentIndex(1)
    assert not _visible(window, key2)
    window.close()

    reopened = RosterWindow(updated, column_settings=_settings(path), view_settings_scope="db-a")
    assert reopened._sheet_tabs.tabText(0) == "학교"
    reopened._sheet_tabs.setCurrentIndex(0)
    third = CustomFieldDefinition(uuid4(), "새 항목 3", CustomFieldType.TEXT, len(updated.custom_fields))
    updated = replace(updated, revision_id=uuid4(), custom_fields=updated.custom_fields + (third,))
    reopened.replace_roster(updated)
    key3 = f"custom:{third.definition_id}"
    assert _visible(reopened, key3)
    reopened._sheet_tabs.setCurrentIndex(1)
    assert not _visible(reopened, key3)
    reopened.close()
