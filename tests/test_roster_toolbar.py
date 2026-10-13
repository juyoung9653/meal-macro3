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


def test_header_only_exposes_frequent_actions(window):
    header = window._more_button.parentWidget()
    visible = {button.text() for button in header.findChildren(QPushButton) if button.isVisible()}
    assert visible == {"문서 만들기", "기사별 가구", "행 추가", "더보기"}
    assert window._more_button.menu() is window._more_menu
    assert window._column_search_header.buttons["name"].isVisible()
    assert not header.findChildren(QComboBox)


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

    def create_menu(parent):
        menu = original(parent)
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
    from src.domain import Meal, MealEnrollment
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
        driver_a.driver_id: (Meal.BREAKFAST,), driver_b.driver_id: (Meal.DINNER,),
    }  # Friday's lunch is unavailable; other meals remain despite the main lunch filter.
    assert len(next(row for row in rows if row.driver_id == driver_a.driver_id).member_ids) == 2

    window._set_day(service_day + timedelta(days=1))
    window._driver_households_button.click()
    rows, settings = opened[-1]
    assert settings["service_day"] == service_day + timedelta(days=1)
    assert next(row for row in rows if row.driver_id == driver_a.driver_id).meals == (Meal.BREAKFAST,)


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
    assert seen == [(True, 1)]
