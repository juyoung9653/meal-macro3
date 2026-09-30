"""Column searches combine correctly and stay aligned with moved headers."""
from dataclasses import replace

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QApplication

from src.desktop_v2.roster_window import RosterTableModel, RosterWindow
from src.desktop_v2.column_search import ColumnValuePicker
from tests.test_desktop_v2_roster import (
    _roster, CUSTOM_FIELD_ID, FIRST_PERSON_ID, SERVICE_DAY,
)
from src.domain import Meal


def test_column_search_combines_and_clears():
    model = RosterTableModel(_roster())
    model.set_context(SERVICE_DAY, Meal.LUNCH)
    model.set_column_search("name", "  홍  ")
    assert model.rowCount() == 1
    assert model.person_at(0).person_id == FIRST_PERSON_ID
    model.set_column_search("school", "없는학교")
    assert model.rowCount() == 0
    model.set_column_search("school", "")
    assert model.rowCount() == 1
    model.set_search("김아침")
    assert model.rowCount() == 0
    model.set_search("")
    model.set_column_search("name", "")
    assert model.rowCount() == 2
    model.set_column_search(f"custom:{CUSTOM_FIELD_ID}", "a구역")
    assert model.rowCount() == 1
    model.set_column_search("birth_date", "2015-03")
    assert model.rowCount() == 1
    model.sort(0, Qt.SortOrder.DescendingOrder)
    assert model.person_at(0).person_id == FIRST_PERSON_ID


def test_removed_custom_column_drops_search():
    roster = _roster()
    model = RosterTableModel(roster)
    model.set_column_search(f"custom:{CUSTOM_FIELD_ID}", "없는구역")
    assert model.rowCount() == 0
    people = tuple(
        replace(person, profile=replace(person.profile, custom_values={}))
        for person in roster.persons
    )
    model.replace_roster(replace(roster, persons=people, custom_fields=()))
    assert model.rowCount() == 2


def test_header_buttons_follow_layout_and_keep_focus(tmp_path):
    app = QApplication.instance() or QApplication([])
    window = RosterWindow(
        _roster(), column_settings=QSettings(str(tmp_path / "columns.ini"), QSettings.IniFormat)
    )
    window.show()
    app.processEvents()
    header = window._column_search_header
    name = header.buttons["name"]
    name.setFocus()
    header.open_value_filter("name")
    picker = header._menu.findChild(ColumnValuePicker)
    picker.search.setText("홍")
    picker.apply.click()
    app.processEvents()
    assert window.table_model.rowCount() == 1
    assert name.toolTip().startswith("필터 적용")
    index = next(i for i, col in enumerate(window.table_model.columns) if col.key == "name")
    header.moveSection(header.visualIndex(index), 3)
    header.resizeSection(index, 170)
    app.processEvents()
    assert name.x() == header.sectionViewportPosition(index) + 170 - header.BUTTON_WIDTH - 2
    assert name.width() == header.BUTTON_WIDTH
    window._table.horizontalScrollBar().setValue(100)
    app.processEvents()
    assert name.x() == header.sectionViewportPosition(index) + 170 - header.BUTTON_WIDTH - 2
    school_index = next(i for i, col in enumerate(window.table_model.columns) if col.key == "school")
    window._table.setColumnHidden(school_index, True)
    app.processEvents()
    assert not header.buttons["school"].isVisible()
    window._table.setColumnHidden(school_index, False)
    app.processEvents()
    assert header.buttons["school"].isVisible()
    window.replace_roster(_roster())
    assert header.buttons["name"] is name
    header.open_value_filter("name")
    assert header._menu.findChild(ColumnValuePicker).search.text() == "홍"
    window.close()


def test_exact_value_filters_include_blanks_and_combine():
    model = RosterTableModel(_roster())
    assert set(model.column_filter_values("school")) == {"", "샘플초"}
    model.set_column_values("school", frozenset({""}))
    assert model.rowCount() == 1
    assert model.person_at(0).profile.name == "김아침"
    # Choices remain available even when a filter hides their rows.
    assert set(model.column_filter_values("school")) == {"", "샘플초"}
    model.set_column_search("name", "홍")
    assert model.rowCount() == 0
    model.set_column_values("school", None)
    assert model.rowCount() == 1
    model.set_column_search("name", "")
    model.set_column_values("name", frozenset({"홍"}))
    assert model.rowCount() == 0  # selected values use equality, not substring search
    model.set_column_values("name", frozenset({"홍길동", "김아침"}))
    assert model.rowCount() == 2
    model.set_column_values("school", frozenset())
    assert model.rowCount() == 0
    model.set_column_values("school", None)
    assert model.rowCount() == 2


def test_value_picker_apply_cancel_reset_and_reopen(tmp_path):
    from src.desktop_v2.column_search import ColumnValuePicker
    app = QApplication.instance() or QApplication([])
    window = RosterWindow(
        _roster(), column_settings=QSettings(str(tmp_path / "values.ini"), QSettings.IniFormat)
    )
    window.show()
    app.processEvents()
    header = window._column_search_header
    header.buttons["school"].click()
    app.processEvents()
    picker = header._menu.findChild(ColumnValuePicker)
    picker.select_none.click()
    picker.list.item(0).setCheckState(Qt.CheckState.Checked)
    picker.apply.click()
    assert window.table_model.rowCount() == 1
    assert window.table_model.selected_column_values("school") == frozenset({""})
    header.open_value_filter("school")
    picker = header._menu.findChild(ColumnValuePicker)
    assert picker.list.item(0).checkState() == Qt.CheckState.Checked
    assert picker.list.item(1).checkState() == Qt.CheckState.Unchecked
    picker.select_all.click()
    picker.cancel.click()
    assert window.table_model.rowCount() == 1
    header.open_value_filter("school")
    picker = header._menu.findChild(ColumnValuePicker)
    picker.search.setText("샘플")
    assert picker.list.item(0).isHidden()
    assert not picker.list.item(1).isHidden()
    picker.reset.click()
    assert window.table_model.rowCount() == 2
    assert window.table_model.selected_column_values("school") is None
    window.close()


def test_header_filter_button_keeps_sort_and_cancel_discards_search(tmp_path):
    app = QApplication.instance() or QApplication([])
    window = RosterWindow(
        _roster(), column_settings=QSettings(str(tmp_path / "filter-button.ini"), QSettings.IniFormat)
    )
    window.show()
    app.processEvents()
    header = window._column_search_header
    button = header.buttons["name"]
    button.click()
    assert header.sortIndicatorSection() == -1
    picker = header._menu.findChild(ColumnValuePicker)
    picker.search.setText("홍")
    picker.cancel.click()
    assert window.table_model.rowCount() == 2
    button.click()
    picker = header._menu.findChild(ColumnValuePicker)
    assert picker.search.text() == ""
    picker.search.setText("홍")
    picker.apply.click()
    assert window.table_model.rowCount() == 1
    assert button.property("filterActive") is True
    button.click()
    picker = header._menu.findChild(ColumnValuePicker)
    assert picker.search.text() == "홍"
    picker.select_none.click()
    picker.apply.click()
    assert window.table_model.rowCount() == 0
    button.click()
    header._menu.findChild(ColumnValuePicker).reset.click()
    assert window.table_model.rowCount() == 2
    assert button.property("filterActive") is False
    window.close()


def test_picker_value_search_uses_column_whitespace_rules():
    app = QApplication.instance() or QApplication([])
    picker = ColumnValuePicker(("석식: 1\n조식: 3", "중식: 2"), None)
    picker.search.setText("석식: 1  조식: 3")
    app.processEvents()
    assert not picker.list.item(0).isHidden()
    assert picker.list.item(1).isHidden()


def test_removed_custom_column_drops_value_selection():
    roster = _roster()
    model = RosterTableModel(roster)
    model.set_column_values(f"custom:{CUSTOM_FIELD_ID}", frozenset())
    people = tuple(
        replace(person, profile=replace(person.profile, custom_values={}))
        for person in roster.persons
    )
    model.replace_roster(replace(roster, persons=people, custom_fields=()))
    assert model.rowCount() == 2


def test_header_mouse_click_toggles_sort_with_filters(tmp_path):
    from PySide6.QtCore import QPoint
    from PySide6.QtTest import QTest
    app = QApplication.instance() or QApplication([])
    window = RosterWindow(
        _roster(), column_settings=QSettings(str(tmp_path / "sort.ini"), QSettings.IniFormat)
    )
    window.show()
    app.processEvents()
    header = window._column_search_header
    model = window.table_model
    model.set_column_values("name", frozenset({"홍길동", "김아침"}))
    index = next(i for i, column in enumerate(model.columns) if column.key == "name")
    point = QPoint(
        header.sectionViewportPosition(index) + header.sectionSize(index) // 2,
        header.height() // 2,
    )
    QTest.mouseClick(header.viewport(), Qt.MouseButton.LeftButton, pos=point)
    assert header.sortIndicatorSection() == index
    first_order = [model.person_at(i).profile.name for i in range(model.rowCount())]
    first_direction = header.sortIndicatorOrder()
    QTest.mouseClick(header.viewport(), Qt.MouseButton.LeftButton, pos=point)
    assert header.sortIndicatorOrder() != first_direction
    assert [model.person_at(i).profile.name for i in range(model.rowCount())] == first_order[::-1]
    assert model.selected_column_values("name") == frozenset({"홍길동", "김아침"})
    window.close()
