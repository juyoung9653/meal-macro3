from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QApplication

from src.desktop_v2.meal_column_filter import MealColumnValuePicker, decode_meal_filter, meal_filter_values
from src.desktop_v2.roster_window import RosterTableModel, RosterWindow
from src.domain.roster import Person, PersonProfile, RosterSnapshot, Meal, MealEnrollment, DeliveryAssignment, InclusiveDateRange
from src.domain.status import MealStatus, StatusEvent, resolve_status_history


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _data():
    people = tuple(Person(uuid4(), PersonProfile(name)) for name in ("A배송", "A일시중지", "B배송", "상태없음"))
    revision = uuid4()
    roster = RosterSnapshot(revision, people,
        enrollments=tuple(MealEnrollment(p.person_id, Meal.DINNER) for p in people[:3]),
        assignments=tuple(DeliveryAssignment(p.person_id, Meal.DINNER, driver)
                          for p, driver in zip(people[:3], ("기사A", "기사A", "기사B"))))
    event = StatusEvent(uuid4(), revision, people[1].person_id, Meal.DINNER, MealStatus.PAUSED,
        InclusiveDateRange(date(2026, 1, 1), date(2028, 12, 31)), datetime(2026, 1, 1, tzinfo=UTC), "합성")
    histories = {(people[1].person_id, Meal.DINNER): resolve_status_history(people[1].person_id, Meal.DINNER, (event,))}
    return roster, histories


def _check(box, values):
    for index in range(1, box.count()):
        item = box.item(index)
        item.setCheckState(Qt.CheckState.Checked if item.data(Qt.ItemDataRole.UserRole) in values else Qt.CheckState.Unchecked)


def test_driver_and_status_filters_are_independent_and_combine(qapp):
    roster, histories = _data()
    model = RosterTableModel(roster, histories)
    model.set_context(date(2026, 8, 17), Meal.DINNER)
    def names():
        return {model.person_at(i).profile.name for i in range(model.rowCount())}
    model.set_column_values("status:dinner", meal_filter_values({"기사A"}, {"배송"}))
    assert names() == {"A배송"}
    model.set_column_values("status:dinner", meal_filter_values(None, {"배송"}))
    assert names() == {"A배송", "B배송"}
    model.set_column_values("status:dinner", meal_filter_values({"기사A"}, {"배송", "일시중지"}))
    assert names() == {"A배송", "A일시중지"}
    model.set_column_search("name", "일시")
    assert names() == {"A일시중지"}
    model.clear_column_filters()
    model.set_column_values("status:dinner", meal_filter_values(None, {"-"}))
    assert names() == {"상태없음"}
    model.set_column_values("status:dinner", meal_filter_values(set(), None))
    assert names() == set()
    model.clear_column_filters()
    assert len(names()) == 4


def test_picker_keeps_legacy_pairs_until_selection_changes(qapp):
    values = ("기사A\n배송", "기사A\n일시중지", "기사B\n배송", "기사B\n일시중지")
    legacy = frozenset({"기사A\n배송", "기사B\n일시중지"})
    picker = MealColumnValuePicker(values, legacy)
    results = []
    picker.applied.connect(lambda search, selected: results.append(selected))
    picker.apply.click()
    assert results[-1] == legacy
    _check(picker.driver_list, {"기사A"})
    _check(picker.status_list, {"배송", "일시중지"})
    picker.apply.click()
    assert decode_meal_filter(results[-1]) == {"drivers": frozenset({"기사A"}), "statuses": frozenset({"배송", "일시중지"})}
    picker.driver_list.all_item.setCheckState(Qt.CheckState.Checked)
    picker.status_list.all_item.setCheckState(Qt.CheckState.Checked)
    picker.apply.click()
    assert results[-1] is None
    assert not hasattr(picker, "reset")
    picker.close()


def test_split_filter_persists_and_header_opens_split_picker(qapp, tmp_path):
    roster, histories = _data()
    settings = QSettings(str(tmp_path / "filters.ini"), QSettings.IniFormat)
    window = RosterWindow(roster, column_settings=settings)
    try:
        window._column_search_header.open_value_filter("status:dinner")
        picker = window._column_search_header._menu.findChild(MealColumnValuePicker)
        assert picker is not None
        _check(picker.driver_list, {"기사A"})
        _check(picker.status_list, {"배송"})
        picker.apply.click()
        saved = window.table_model.selected_column_values("status:dinner")
        assert decode_meal_filter(saved)["drivers"] == frozenset({"기사A"})
    finally:
        window.close()
    reopened = RosterWindow(roster, column_settings=settings)
    try:
        assert reopened.table_model.selected_column_values("status:dinner") == saved
    finally:
        reopened.close()


def test_all_selection_row_toggles_and_tracks_partial_state(qapp):
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QPushButton
    picker = MealColumnValuePicker(("기사A\n배송", "기사B\n배송"), None)
    picker.show(); qapp.processEvents()
    box = picker.driver_list
    assert box.item(0).text() == "(모두 선택)"
    assert box.item(0).checkState() == Qt.CheckState.Checked
    box.item(1).setCheckState(Qt.CheckState.Unchecked)
    assert box.item(0).checkState() == Qt.CheckState.PartiallyChecked
    point = box.visualItemRect(box.item(0)).topLeft()
    point.setX(point.x() + 10); point.setY(point.y() + 10)
    QTest.mouseClick(box.viewport(), Qt.MouseButton.LeftButton, pos=point)
    assert box.item(0).checkState() == Qt.CheckState.Checked
    assert picker._selected(box) is None
    QTest.mouseClick(box.viewport(), Qt.MouseButton.LeftButton, pos=point)
    assert picker._selected(box) == frozenset()
    assert picker.status_list.item(0).checkState() == Qt.CheckState.Checked
    assert {button.text() for button in picker.findChildren(QPushButton)} == {"적용", "취소"}
    assert picker.width() <= 340
    picker.close()


@pytest.mark.parametrize("shared", [False, True])
def test_meal_filter_outer_popup_fits_fixed_width_content(qapp, tmp_path, shared):
    roster, _ = _data()
    settings = QSettings(str(tmp_path / "popup.ini"), QSettings.IniFormat)
    window = RosterWindow(roster, column_settings=settings)
    try:
        window.show()
        if shared:
            window.set_household_shared_state(frozenset({"status:dinner"}), frozenset())
        qapp.processEvents()
        window._column_search_header.open_value_filter("status:dinner")
        qapp.processEvents()
        menu = window._column_search_header._menu
        picker = menu.findChild(MealColumnValuePicker)
        left = picker.geometry().left()
        right = menu.width() - picker.geometry().right() - 1
        bottom = menu.height() - picker.geometry().bottom() - 1
        assert picker.width() == 340
        assert 0 <= right <= left + 4, (menu.size(), picker.geometry())
        assert 0 <= bottom <= picker.geometry().top() + 4, (menu.size(), picker.geometry())
        assert menu.width() <= picker.width() + 32
        assert picker.apply.geometry().bottom() < picker.height()
        menu.close()
    finally:
        window.close()
