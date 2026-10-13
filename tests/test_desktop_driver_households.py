"""Read-only tests for the driver household manifest dialog."""

import os
from dataclasses import replace
from datetime import date
from uuid import uuid4

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QDialogButtonBox
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest

from src.desktop_v2.driver_households import DriverHouseholdsDialog
from src.desktop_v2.column_search import ColumnValuePicker
from src.domain.delivery_routing import Driver
from src.domain.roster import Meal

from src.services.driver_households import DriverHouseholdRow


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _row(household, driver, name, *, address="성남시", contact="010-1", meals=(Meal.DINNER,), sequence=("1",)):
    return DriverHouseholdRow(
        household, driver.driver_id if driver else None, name,
        (uuid4(),), ("김가구",), meals, address, (contact,), sequence,
    )


def test_fixed_uuid_scope_and_no_top_filter_controls(app):
    from PySide6.QtWidgets import QComboBox, QLineEdit
    first, second = Driver(uuid4(), "동일"), Driver(uuid4(), "동일")
    rows = (_row(uuid4(), first, first.name), _row(uuid4(), second, second.name),
            _row(uuid4(), None, ""), _row(uuid4(), None, "이전 기사"))
    dialog = DriverHouseholdsDialog(rows, service_day=date(2026, 9, 22), drivers=(first, second), selected_driver_id=second.driver_id)
    assert dialog.table.rowCount() == 1
    assert dialog._selected_driver_id == second.driver_id
    assert not dialog.findChildren(QComboBox)
    assert not dialog.findChildren(QLineEdit)
    assert dialog.table.item(0, 3).data(Qt.UserRole).driver_id == second.driver_id
    dialog._set_column_values("driver", {"이전 기사"})
    assert dialog.table.rowCount() == 0  # Header filters cannot widen the fixed scope.
    assert dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Close).text() == "닫기"
    dialog.close()


def test_name_search_empty_state_and_pre_grouped_rows_are_not_reprojected(app):
    driver = Driver(uuid4(), "기사")
    household = uuid4()
    rows = (
        _row(household, driver, driver.name, meals=(Meal.DINNER, Meal.LUNCH), sequence=("1", "2")),
        _row(household, None, "", address="다른 주소", contact="010-9999"),
    )
    dialog = DriverHouseholdsDialog(rows, service_day=date(2026, 9, 22), drivers=(driver,))
    assert dialog.table.rowCount() == 2
    assert "가구 1건 · 표시 2행" == dialog.count.text()
    assert "석식, 중식" in {
        dialog.table.item(index, 2).text() for index in range(dialog.table.rowCount())
    }
    dialog._set_column_search("contacts", "9999")
    assert dialog.table.rowCount() == 1
    dialog._set_column_search("members", "없는 가구")
    assert dialog.table.rowCount() == 0
    assert "해당" in dialog.empty.text()
    dialog.close()


def test_meal_split_uses_exact_rows_and_restores_merged_view_with_filters_and_sort(app):
    driver = Driver(uuid4(), "기사")
    household = uuid4()
    first, second = uuid4(), uuid4()
    dinner = DriverHouseholdRow(
        household, driver.driver_id, driver.name, (first,), ("첫째",),
        (Meal.DINNER,), "성남시", ("010-1",), ("003",),
    )
    breakfast = DriverHouseholdRow(
        household, driver.driver_id, driver.name, (first, second), ("첫째", "둘째"),
        (Meal.BREAKFAST,), "성남시", ("010-1", "010-2"), ("001", "002"),
    )
    lunch = DriverHouseholdRow(
        household, driver.driver_id, driver.name, (second,), ("둘째",),
        (Meal.LUNCH,), "성남시", ("010-2",), ("004",),
    )
    merged = DriverHouseholdRow(
        household, driver.driver_id, driver.name, (first, second), ("첫째", "둘째"),
        (Meal.DINNER, Meal.BREAKFAST, Meal.LUNCH), "성남시",
        ("010-1", "010-2"), ("001", "002", "003", "004"),
        (dinner, breakfast, lunch),
    )
    dialog = DriverHouseholdsDialog((merged,), service_day=date(2026, 9, 22), drivers=(driver,), selected_driver_id=driver.driver_id)
    dialog.table.selectRow(0)
    dialog.table.sortItems(3, Qt.SortOrder.DescendingOrder)
    dialog.split_button.click()
    assert dialog.split_button.text() == "분리 해제"
    assert dialog.count.text() == "가구 1건 · 표시 3행"
    assert {tuple(dialog.table.item(i, c).text() for c in (1, 2, 3, 5))
            for i in range(3)} == {
        ("첫째", "석식", "003", "010-1"),
        ("첫째, 둘째", "조식", "001, 002", "010-1, 010-2"),
        ("둘째", "중식", "004", "010-2"),
    }
    assert dialog.table.item(0, 3).text() == "004"
    assert dialog.table.selectedItems()
    dialog._set_column_search("members", "둘째")
    assert dialog.table.rowCount() == 2
    dialog.split_button.click()
    assert dialog.split_button.text() == "식종별 분리"
    assert dialog.table.rowCount() == 1
    assert dialog.table.item(0, 2).text() == "석식, 조식, 중식"
    assert dialog._selected_driver_id == driver.driver_id
    assert dialog._column_searches["members"] == "둘째"
    dialog.close()


def test_split_reconciles_exact_values_and_empty_state(app):
    driver = Driver(uuid4(), "기사")
    household = uuid4()
    dinner = _row(household, driver, driver.name, meals=(Meal.DINNER,))
    breakfast = replace(dinner, meals=(Meal.BREAKFAST,))
    merged = replace(dinner, meals=(Meal.DINNER, Meal.BREAKFAST), meal_rows=(dinner, breakfast))
    dialog = DriverHouseholdsDialog((merged,), service_day=date(2026, 9, 22), drivers=(driver,))
    dialog._set_column_values("meals", {"석식, 조식"})
    assert dialog.table.rowCount() == 1
    dialog.split_button.click()
    assert dialog.table.rowCount() == 2
    assert "meals" not in dialog._column_values
    dialog._set_column_search("meals", "중식")
    assert dialog.table.rowCount() == 0
    assert "해당" in dialog.empty.text()
    dialog._set_column_search("meals", "")
    dialog._set_column_values("meals", set())
    dialog.split_button.click()
    assert dialog.table.rowCount() == 0
    assert dialog._column_values["meals"] == frozenset()
    dialog.close()

    empty = DriverHouseholdsDialog((), service_day=date(2026, 9, 22), drivers=())
    empty.split_button.click()
    assert empty.count.text() == "가구 0건 · 표시 0행"
    assert "배송 상태" in empty.empty.text()
    empty.close()


def test_legacy_name_selection_is_not_mistaken_for_unassigned(app):
    driver = Driver(uuid4(), "기사")
    dialog = DriverHouseholdsDialog((_row(uuid4(), None, ""),), service_day=date(2026, 9, 22), drivers=(driver,), selected_driver_id="기사")
    assert dialog._selected_driver_id is None
    assert dialog.table.rowCount() == 1
    dialog.close()


def test_no_rows_explains_that_the_selected_day_has_no_delivery_households(app):
    dialog = DriverHouseholdsDialog((), service_day=date(2026, 9, 22), drivers=())
    assert dialog.table.rowCount() == 0
    assert "배송 상태" in dialog.empty.text()
    assert dialog.count.text() == "가구 0건 · 표시 0행"
    dialog.close()


@pytest.fixture
def searchable_dialog(app):
    first, second = Driver(uuid4(), "기사A"), Driver(uuid4(), "기사B")
    rows = (
        replace(_row(uuid4(), first, first.name, address="판교", contact="010-10", sequence=("10",)), member_names=("가구다",)),
        replace(_row(uuid4(), first, first.name, address="정자", contact="010-2", sequence=("2",), meals=(Meal.BREAKFAST,)), member_names=("가구나",)),
        replace(_row(uuid4(), second, second.name, address="판교", contact="010-1", sequence=("1",), meals=(Meal.LUNCH,)), member_names=("가구가",)),
        replace(_row(uuid4(), None, "", address="", contact="", sequence=()), member_names=("미등록",)),
    )
    dialog = DriverHouseholdsDialog(rows, service_day=date(2026, 9, 22), drivers=(first, second))
    dialog.show()
    app.processEvents()
    yield dialog
    dialog.close()


def test_header_click_sorts_numeric_sequences_and_filter_refresh_keeps_rows_intact(searchable_dialog, app):
    dialog = searchable_dialog
    header = dialog._column_search_header
    assert [dialog.table.item(i, 3).text() for i in range(4)] == ["10", "2", "1", ""]
    dialog.table.sortItems(3, Qt.SortOrder.AscendingOrder)
    assert [dialog.table.item(i, 3).text() for i in range(4)] == ["1", "2", "10", ""]
    dialog._set_column_search("members", "가구")
    assert [dialog.table.item(i, 5).text() for i in range(3)] == ["010-1", "010-2", "010-10"]
    point = QPoint(header.sectionViewportPosition(3) + header.sectionSize(3) // 2,
                   header.height() // 2)
    QTest.mouseClick(header.viewport(), Qt.MouseButton.LeftButton, pos=point)
    app.processEvents()
    assert header.sortIndicatorOrder() == Qt.SortOrder.DescendingOrder
    assert [dialog.table.item(i, 3).text() for i in range(3)] == ["10", "2", "1"]
    dialog._set_column_search("contacts", "010-")
    assert [dialog.table.item(i, 1).text() for i in range(3)] == ["가구다", "가구나", "가구가"]
    assert all(dialog.table.item(i, 5).toolTip() == dialog.table.item(i, 5).text() for i in range(3))


@pytest.mark.parametrize("key,query,count", (
    ("driver", "기사a", 2), ("members", "가구나", 1), ("meals", "조식", 1),
    ("sequence", "  2  ", 1), ("address", "판교", 2), ("contacts", "010-2", 1),
))
def test_each_column_search_filters_its_own_displayed_values(searchable_dialog, key, query, count):
    dialog = searchable_dialog
    dialog._column_search_header.open_value_filter(key)
    picker = dialog._column_search_header._menu.findChild(ColumnValuePicker)
    picker.search.setText(query)
    picker.apply.click()
    assert dialog.table.rowCount() == count
    dialog._column_search_header.open_value_filter(key)
    dialog._column_search_header._menu.findChild(ColumnValuePicker).reset.click()
    assert dialog.table.rowCount() == 4


def test_column_searches_combine_with_driver_header_filter(searchable_dialog):
    dialog = searchable_dialog
    header = dialog._column_search_header
    dialog._set_column_values("driver", {"기사A"})
    dialog._set_column_search("contacts", "010")
    dialog._set_column_search("address", "판교")
    dialog._set_column_search("meals", "석식")
    assert dialog.table.rowCount() == 1
    assert dialog.table.item(0, 1).text() == "가구다"
    assert dialog.count.text() == "가구 1건 · 표시 1행"
    dialog._set_column_values("driver", {"기사B"})
    assert dialog.table.rowCount() == 0
    assert "해당" in dialog.empty.text()
    dialog._set_column_search("meals", "")
    assert dialog.table.rowCount() == 1
    assert dialog.table.item(0, 1).text() == "가구가"


def test_column_value_picker_filters_blanks_and_can_reset(searchable_dialog, app):
    from src.desktop_v2.column_search import ColumnValuePicker

    dialog = searchable_dialog
    header = dialog._column_search_header
    header.open_value_filter("address")
    app.processEvents()
    picker = header._menu.findChild(ColumnValuePicker)
    picker.select_none.click()
    picker.list.item(0).setCheckState(Qt.CheckState.Checked)  # Blank address.
    picker.apply.click()
    assert dialog.table.rowCount() == 1
    assert dialog.table.item(0, 1).text() == "미등록"
    assert set(dialog.column_filter_values("address")) == {"", "정자", "판교"}
    dialog._set_column_search("members", "가구")
    assert dialog.table.rowCount() == 0
    header.open_value_filter("address")
    header._menu.findChild(ColumnValuePicker).reset.click()
    assert dialog.table.rowCount() == 3


def test_column_buttons_stay_aligned_after_resizing_and_horizontal_scroll(searchable_dialog, app):
    dialog = searchable_dialog
    header = dialog._column_search_header
    dialog.resize(760, 560)
    header.resizeSection(1, 270)
    app.processEvents()
    dialog.table.horizontalScrollBar().setValue(dialog.table.horizontalScrollBar().maximum())
    app.processEvents()
    for column, key in enumerate(("driver", "members", "meals", "sequence", "address", "contacts")):
        button = header.buttons[key]
        assert button.geometry().left() == header.sectionViewportPosition(column) + header.sectionSize(column) - header.BUTTON_WIDTH - 2
        assert button.width() == header.BUTTON_WIDTH
