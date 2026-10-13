"""Main roster view choices survive a fresh window and roster refresh."""

from dataclasses import replace
from uuid import uuid4

import pytest

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QApplication

from src.desktop_v2.column_search import ColumnValuePicker
import src.desktop_v2.inactive_roster as inactive_roster_module
from src.desktop_v2.roster_window import RosterWindow
from src.domain import CustomFieldDefinition, CustomFieldType
from tests.test_desktop_v2_roster import _roster, SERVICE_DAY


@pytest.fixture(autouse=True)
def isolated_archive_preference(monkeypatch, tmp_path):
    settings = QSettings(str(tmp_path / "archive.ini"), QSettings.Format.IniFormat)
    monkeypatch.setattr(inactive_roster_module, "QSettings", lambda *_args, **_kwargs: settings)


def _settings(path):
    return QSettings(str(path), QSettings.Format.IniFormat)


def _column(window, key):
    return next(index for index, column in enumerate(window.table_model.columns) if column.key == key)


def test_fresh_window_restores_width_and_combined_searches(tmp_path):
    app = QApplication.instance() or QApplication([])
    path = tmp_path / "view.ini"
    roster = _roster()
    window = RosterWindow(roster, column_settings=_settings(path), view_settings_scope="db-a")
    window.show()
    app.processEvents()
    name_column = _column(window, "name")
    window._table.horizontalHeader().resizeSection(name_column, 273)
    window._search_input.setText("홍")
    window._include_hidden_filter.setChecked(True)
    window._column_search_header.open_value_filter("school")
    picker = window._column_search_header._menu.findChild(ColumnValuePicker)
    picker.search.setText("샘플")
    picker.apply.click()
    assert window.table_model.rowCount() == 1
    window.close()

    reopened = RosterWindow(roster, column_settings=_settings(path), view_settings_scope="db-a")
    reopened.show()
    app.processEvents()
    try:
        assert reopened._table.horizontalHeader().sectionSize(_column(reopened, "name")) == 273
        assert reopened._search_input.text() == "홍"
        assert reopened._include_hidden_filter.isChecked()
        assert reopened.table_model.selected_column_search("school") == "샘플"
        assert reopened._column_search_header.buttons["school"].property("filterActive") is True
        assert reopened.table_model.rowCount() == 1
        reopened.table_model.set_archive_filter(frozenset({roster.persons[0].person_id}), False)
        assert reopened.table_model.rowCount() == 1  # restored hidden-row search still applies
        reopened._search_input.clear()
        assert reopened.table_model.rowCount() == 1
        reopened._search_input.setText("홍")
        assert reopened.table_model.rowCount() == 1
        updated = replace(roster, revision_id=uuid4(), custom_fields=(
            *roster.custom_fields,
            CustomFieldDefinition(uuid4(), "새 항목", CustomFieldType.TEXT, len(roster.custom_fields)),
        ))
        reopened.replace_roster(updated)
        assert reopened._table.horizontalHeader().sectionSize(_column(reopened, "name")) == 273
        assert reopened.table_model.rowCount() == 1
    finally:
        reopened.close()


def test_value_selection_none_all_and_clear_survive_reopen(tmp_path):
    app = QApplication.instance() or QApplication([])
    path = tmp_path / "values.ini"
    roster = _roster()
    window = RosterWindow(roster, column_settings=_settings(path), view_settings_scope="db-a")
    window._column_search_header.open_value_filter("school")
    picker = window._column_search_header._menu.findChild(ColumnValuePicker)
    picker.select_none.click()
    picker.apply.click()
    assert window.table_model.rowCount() == 0
    assert _settings(path).value(window._search_settings_key) is not None
    window.close()

    reopened = RosterWindow(roster, column_settings=_settings(path), view_settings_scope="db-a")
    assert reopened.table_model.selected_column_values("school") == frozenset()
    assert reopened.table_model.rowCount() == 0
    reopened._column_search_header.open_value_filter("school")
    picker = reopened._column_search_header._menu.findChild(ColumnValuePicker)
    picker.select_all.click()
    picker.apply.click()
    assert reopened.table_model.selected_column_values("school") is None
    reopened.close()

    cleared = RosterWindow(roster, column_settings=_settings(path), view_settings_scope="db-a")
    try:
        assert cleared.table_model.selected_column_values("school") is None
        assert cleared.table_model.rowCount() == 2
        assert not _settings(path).contains(cleared._search_settings_key)
    finally:
        cleared.close()


def test_search_is_scoped_and_missing_selected_values_stay_explicit(tmp_path):
    app = QApplication.instance() or QApplication([])
    path = tmp_path / "scope.ini"
    roster = _roster()
    first = RosterWindow(roster, column_settings=_settings(path), view_settings_scope="db-a")
    first._model.set_column_values("school", frozenset({"새 학교"}))
    first._save_search_filters()
    first.close()

    other = RosterWindow(roster, column_settings=_settings(path), view_settings_scope="db-b")
    assert other.table_model.rowCount() == 2
    other.close()

    restored = RosterWindow(roster, column_settings=_settings(path), view_settings_scope="db-a")
    try:
        assert restored.table_model.selected_column_values("school") == frozenset({"새 학교"})
        assert restored.table_model.rowCount() == 0
        restored._column_search_header.open_value_filter("school")
        picker = restored._column_search_header._menu.findChild(ColumnValuePicker)
        assert "새 학교" in [picker.list.item(i).text() for i in range(picker.list.count())]
    finally:
        restored.close()


def test_optional_delivery_settings_column_width_survives_toggle_and_relaunch(tmp_path):
    app = QApplication.instance() or QApplication([])
    path = tmp_path / "optional.ini"
    roster = _roster()
    window = RosterWindow(roster, column_settings=_settings(path))
    window._special_only_filter.setChecked(True)
    window._table.horizontalHeader().resizeSection(_column(window, "delivery_settings"), 319)
    window._special_only_filter.setChecked(False)
    window.close()

    reopened = RosterWindow(roster, column_settings=_settings(path))
    try:
        reopened._special_only_filter.setChecked(True)
        assert reopened._table.horizontalHeader().sectionSize(_column(reopened, "delivery_settings")) == 319
    finally:
        reopened.close()


def test_optional_column_search_restores_with_its_visibility_and_clears_on_toggle(tmp_path):
    app = QApplication.instance() or QApplication([])
    path = tmp_path / "optional-filter.ini"
    roster = _roster()
    window = RosterWindow(roster, column_settings=_settings(path), view_settings_scope="db-a")
    window._special_only_filter.setChecked(True)
    window._column_search_header.open_value_filter("delivery_settings")
    picker = window._column_search_header._menu.findChild(ColumnValuePicker)
    picker.search.setText("개인")
    picker.apply.click()
    window.close()

    reopened = RosterWindow(roster, column_settings=_settings(path), view_settings_scope="db-a")
    assert reopened._special_only_filter.isChecked()
    assert reopened.table_model.selected_column_search("delivery_settings") == "개인"
    assert reopened._column_search_header.buttons["delivery_settings"].property("filterActive") is True
    reopened._special_only_filter.setChecked(False)
    reopened.close()

    cleared = RosterWindow(roster, column_settings=_settings(path), view_settings_scope="db-a")
    try:
        assert not cleared._special_only_filter.isChecked()
        assert cleared.table_model.selected_column_search("delivery_settings") == ""
        assert "delivery_settings" not in cleared._column_search_header.buttons
    finally:
        cleared.close()


def test_restored_column_filters_show_reason_and_reset_only_columns(tmp_path):
    app = QApplication.instance() or QApplication([])
    path = tmp_path / "filtered.ini"
    roster = _roster()
    archived_id = roster.persons[0].person_id
    first = RosterWindow(roster, column_settings=_settings(path), view_settings_scope="db-a")
    first._search_input.setText("김")
    first._on_column_search_changed("name", "김")
    first._on_column_values_changed("school", frozenset({"샘플초"}))
    first.close()

    restored = RosterWindow(roster, column_settings=_settings(path), view_settings_scope="db-a")
    restored._set_day(SERVICE_DAY)
    restored.table_model.set_archive_filter(frozenset({archived_id}), False)
    name_index = _column(restored, "name")
    restored.table_model.sort(name_index, Qt.SortOrder.DescendingOrder)
    try:
        assert restored.table_model.rowCount() == 0
        assert not restored._column_filter_strip.isHidden()
        assert "성명" in restored._column_filter_label.text()
        assert "학교" in restored._column_filter_label.text()
        assert "표시 0명" in restored._column_filter_label.text()
        restored._clear_column_filters_button.click()
        app.processEvents()
        assert restored.table_model.rowCount() == 1
        assert restored.table_model.person_at(0).person_id != archived_id
        assert restored._column_filter_strip.isHidden()
        assert restored._search_input.text() == "김"
        assert restored.table_model.service_day == SERVICE_DAY
        assert restored.table_model._sort_column == name_index
        assert restored.table_model._sort_order == Qt.SortOrder.DescendingOrder
        assert restored.table_model.selected_column_search("name") == ""
        assert restored.table_model.selected_column_values("school") is None
        assert restored._column_search_header.buttons["name"].property("filterActive") is False
    finally:
        restored.close()

    reopened = RosterWindow(roster, column_settings=_settings(path), view_settings_scope="db-a")
    try:
        reopened.table_model.set_archive_filter(frozenset({archived_id}), False)
        assert reopened.table_model.rowCount() == 1
        assert reopened._search_input.text() == "김"
        assert reopened._column_filter_strip.isHidden()
        assert reopened.table_model.selected_column_values("school") is None
    finally:
        reopened.close()


def test_empty_value_selection_is_visible_and_one_click_clearable(tmp_path):
    app = QApplication.instance() or QApplication([])
    path = tmp_path / "none.ini"
    roster = _roster()
    first = RosterWindow(roster, column_settings=_settings(path), view_settings_scope="db-a")
    first._on_column_values_changed("school", frozenset())
    first.close()

    restored = RosterWindow(roster, column_settings=_settings(path), view_settings_scope="db-a")
    try:
        assert restored.table_model.rowCount() == 0
        assert not restored._column_filter_strip.isHidden()
        assert "학교" in restored._column_filter_label.text()
        restored._clear_column_filters_button.click()
        app.processEvents()
        assert restored.table_model.rowCount() == 2
        assert restored.table_model.selected_column_values("school") is None
        assert restored._column_filter_strip.isHidden()
    finally:
        restored.close()
