"""Frequent actions stay visible; occasional work remains reachable by menu."""

from datetime import date

import pytest
from PySide6.QtWidgets import QApplication, QComboBox, QPushButton

from src.desktop_v2.roster_window import RosterWindow
from tests.test_desktop_v2_roster import _roster


@pytest.fixture
def window():
    app = QApplication.instance() or QApplication([])
    view = RosterWindow(_roster())
    view._set_day(date(2026, 8, 21))
    view.show()
    app.processEvents()
    yield view
    view.close()


@pytest.mark.parametrize("action_name,signal_name", (
    ("_import_action", "import_review_requested"),
    ("_database_export_action", "database_export_requested"),
    ("_revision_history_action", "revision_history_requested"),
    ("_drivers_action", "drivers_requested"),
    ("_driver_replacement_action", "driver_replacement_requested"),
    ("_school_calendar_action", "school_calendar_requested"),
    ("_service_adjustment_action", "service_adjustment_requested"),
    ("_unit_price_action", "unit_price_requested"),
    ("_settings_action", "settings_requested"),
))
def test_more_menu_actions_keep_their_original_workflows(window, action_name, signal_name):
    called = []
    getattr(window, signal_name).connect(lambda: called.append(True))
    action = getattr(window, action_name)
    assert action in window._more_menu.actions()
    action.trigger()
    assert called == [True]


def test_current_sheet_export_action_emits_frozen_snapshot(window):
    snapshots = []
    window.current_sheet_export_requested.connect(snapshots.append)
    window._current_sheet_export_action.trigger()
    assert len(snapshots) == 1
    assert snapshots[0].sheet_name == "명단"
    assert snapshots[0].headers
    assert len(snapshots[0].rows) == window.table_model.rowCount()


def test_header_only_exposes_frequent_actions(window):
    header = window._more_button.parentWidget()
    visible = {button.text() for button in header.findChildren(QPushButton) if button.isVisible()}
    assert visible == {"문서 만들기", "기사별 가구", "행 추가", "더보기"}
    assert window._more_button.menu() is window._more_menu
    assert window._column_search_header.buttons["name"].isVisible()
    assert not header.findChildren(QComboBox)


def test_archive_threshold_button_only_appears_in_archive_mode(window):
    assert window._archive_threshold_button.isHidden()
    assert "보관 기준 설정" not in {
        action.text() for action in window._more_menu.actions()
    }
    window._archive_action.trigger()
    assert window._archive_threshold_button.isVisible()
    window._archive_action.trigger()
    assert window._archive_threshold_button.isHidden()


def test_row_actions_emit_selected_meal_and_person(window):
    from src.domain.roster import Meal

    added = []
    deleted = []
    window.person_add_requested.connect(added.append)
    window.person_delete_requested.connect(deleted.append)
    window._add_person_button.click()
    window._add_person_action.trigger()
    assert added == [Meal.LUNCH, Meal.LUNCH]
    person = window.table_model.person_at(0)
    menu = window._create_context_menu(person, column_key="name")
    menu.actions()[-1].trigger()
    assert deleted == [person.person_id]


def test_column_settings_remains_reachable_from_more_menu(window, monkeypatch):
    menus = []
    from PySide6.QtCore import QTimer

    original = window._column_layout.create_menu

    def create_menu(parent, **kwargs):
        menu = original(parent, **kwargs)
        menus.append(menu)
        QTimer.singleShot(0, menu.close)
        return menu

    monkeypatch.setattr(window._column_layout, "create_menu", create_menu)
    window._columns_action.trigger()
    assert len(menus) == 1
    assert "기본 열 구성으로 되돌리기" in {action.text() for action in menus[0].actions()}
    assert "사용자 항목 추가·삭제" in {action.text() for action in menus[0].actions()}


def test_driver_view_uses_all_delivery_meals_and_current_date_despite_roster_filters(window, monkeypatch):
    from dataclasses import replace
    from datetime import timedelta
    from uuid import uuid4

    from src.desktop_v2 import driver_households
    from src.domain.roster import (
        Meal,
        MealEnrollment,
    )
    from src.domain.delivery_routing import Driver, RoutingEvent, RoutingState
    from tests.test_desktop_v2_roster import _calendar, _histories

    service_day = date(2026, 8, 21)

    roster = _roster()
    first, second = roster.persons
    second = replace(second, profile=replace(second.profile, road_address=first.profile.road_address))
    roster = replace(roster, persons=(first, second), enrollments=(
        *roster.enrollments,
        MealEnrollment(first.person_id, Meal.BREAKFAST),
        MealEnrollment(first.person_id, Meal.DINNER),
    ))
    driver_a, driver_b = Driver(uuid4(), "기사A"), Driver(uuid4(), "기사B")
    state = RoutingState((driver_a, driver_b), (
        RoutingEvent(uuid4(), first.person_id, service_day, 1, {
            Meal.BREAKFAST: driver_a.driver_id, Meal.LUNCH: driver_a.driver_id,
            Meal.DINNER: driver_b.driver_id,
        }),
        RoutingEvent(uuid4(), second.person_id, service_day, 2, {Meal.BREAKFAST: driver_a.driver_id}),
    ))
    window.replace_roster(roster, _histories())
    window.set_routing_state(state)
    window.set_school_calendar(_calendar())
    window._set_day(service_day)
    window.table_model.set_column_search("name", "검색에 해당하는 사람 없음")
    assert window.table_model.rowCount() == 0
    opened = []

    class Dialog:
        def __init__(self, rows, **kwargs):
            opened.append((rows, kwargs))
            self.sequence_save_requested = SignalStub()

        def exec(self):
            return 0

        def deleteLater(self):
            pass

    class SignalStub:
        def connect(self, callback):
            pass

    monkeypatch.setattr(driver_households, "DriverHouseholdsDialog", Dialog)
    window._driver_households_button.click()
    rows, settings = opened[-1]
    assert settings["service_day"] == service_day
    assert settings.get("selected_driver_id") is None
    assert settings["drivers"] == state.drivers
    assert len(rows) == 2
    assert {row.driver_id: row.meals for row in rows} == {
        driver_a.driver_id: (Meal.BREAKFAST, Meal.LUNCH), driver_b.driver_id: (Meal.DINNER,),
    }  # All assigned meals remain regardless of service days and main-table filters.
    assert len(next(row for row in rows if row.driver_id == driver_a.driver_id).member_ids) == 2

    window._set_day(service_day + timedelta(days=1))
    window._driver_households_button.click()
    rows, settings = opened[-1]
    assert settings["service_day"] == service_day + timedelta(days=1)
    assert next(row for row in rows if row.driver_id == driver_a.driver_id).meals == (Meal.BREAKFAST, Meal.LUNCH)


def test_driver_households_button_opens_real_dialog_and_can_close(window):
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QDialogButtonBox
    from src.desktop_v2.driver_households import DriverHouseholdsDialog

    seen = []

    def close_dialog():
        dialog = window.findChild(DriverHouseholdsDialog)
        if dialog is not None:
            seen.append((dialog.isVisible(), dialog.table.rowCount()))
            dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Close).click()

    QTimer.singleShot(0, close_dialog)
    window._driver_households_button.click()
    assert seen == [(True, 2)]

@pytest.mark.parametrize("size", [(1080, 680), (1440, 860)])
def test_archive_controls_share_filter_row_and_exit_preserves_conditions(tmp_path, size):
    from PySide6.QtCore import QSettings, Qt

    app = QApplication.instance() or QApplication([])
    settings = QSettings(str(tmp_path / "toolbar.ini"), QSettings.Format.IniFormat)
    view = RosterWindow(_roster(), column_settings=settings, inactive_settings=settings)
    try:
        view.resize(*size)
        view._set_day(date(2026, 8, 21))
        view._search_input.setText("김")
        model = view.table_model
        name_column = next(i for i, column in enumerate(model.columns) if column.key == "name")
        model.sort(name_column, Qt.SortOrder.DescendingOrder)
        before = (view.roster.revision_id, model.service_day, model.sort_criteria, model.rowCount())
        view.show()
        app.processEvents()
        assert view._archive_exit_button.isHidden()
        assert view._database_export_action.text() == "DB 전체 Excel로 내보내기"
        assert view._current_sheet_export_action.text() == "현재 시트 Excel 내보내기"
        for with_filter in (False, True):
            view._on_column_search_changed("name", "김" if with_filter else "")
            view._archive_action.setChecked(True)
            app.processEvents()
            assert view._column_filter_strip.isVisible()
            assert view._clear_column_filters_button.isVisible() is with_filter
            controls = [view._archive_threshold_button, view._archive_exit_button]
            if with_filter:
                controls.insert(0, view._clear_column_filters_button)
            assert all(button.isVisible() for button in controls)
            assert all(button.parentWidget() is view._column_filter_strip for button in controls)
            assert len({button.height() for button in controls}) == 1
            assert len({button.geometry().top() for button in controls}) == 1
            assert all(left.geometry().right() < right.geometry().left()
                       for left, right in zip(controls, controls[1:]))
            view._archive_exit_button.click()
            app.processEvents()
            assert not view._archive_action.isChecked()
            assert view._archive_threshold_button.isHidden()
            assert view._archive_exit_button.isHidden()
            assert view._column_filter_strip.isVisible() is with_filter
            assert view._search_input.text() == "김"
            assert model._column_searches == ({"name": "김"} if with_filter else {})
            assert (view.roster.revision_id, model.service_day, model.sort_criteria, model.rowCount()) == before
        view._archive_action.setChecked(True)
        view._clear_column_filters_button.click()
        assert view._column_filter_strip.isVisible()
        assert view._clear_column_filters_button.isHidden()
        assert view._column_filter_label.text() == ""
        assert view._archive_exit_button.isVisible()
    finally:
        view.close()
