from datetime import date
from types import SimpleNamespace
from uuid import uuid4

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QDialog

from src.desktop_v2.roster_window import RosterWindow
from src.desktop_v2.workspace_tools import (ColumnManagerDialog, ExportChoiceDialog,
    MonthlyOverviewDialog, OperatingScheduleDialog)
from src.desktop_v2.delivery_routing import DriverRegistryDialog, DriverReplacementLaunch
from src.domain.delivery_routing import Driver, RoutingState
from src.services.monthly_meal_statistics import build_monthly_meal_statistics
from src.services.monthly_meal_history import MonthlyMealHistory
from tests.test_desktop_v2_roster import _roster
from tests.test_monthly_meal_statistics import _snapshot


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app):
    window = RosterWindow(_roster())
    window.show()
    yield window
    window.close()
    app.processEvents()


@pytest.mark.parametrize("choice", ["sheet", "database", None])
def test_export_selection_runs_only_chosen_workflow(window, choice):
    called = []
    window.current_sheet_export_requested.connect(lambda snapshot: called.append("sheet"))
    window.database_export_requested.connect(lambda: called.append("database"))
    steps = window._show_export_choices.steps(window)
    request = next(steps)
    assert isinstance(request.dialog, ExportChoiceDialog)
    request.dialog.choice = choice
    with pytest.raises(StopIteration):
        steps.send(QDialog.Accepted if choice else QDialog.Rejected)
    assert called == ([] if choice is None else [choice])


@pytest.mark.parametrize("tab,expected", [(0, "school"), (1, "service")])
def test_schedule_tabs_route_original_editors(window, tab, expected):
    called = []
    window.school_calendar_requested.connect(lambda: called.append("school"))
    window.service_adjustment_requested.connect(lambda: called.append("service"))
    steps = window._show_operating_schedule.steps(window)
    request = next(steps)
    request.dialog.tabs.setCurrentIndex(tab)
    request.dialog.open_button.click()
    with pytest.raises(StopIteration):
        steps.send(QDialog.Accepted)
    assert called == [expected]


def test_monthly_tabs_share_month_preserve_filters_and_read_fresh(app):
    snapshot, _ = _snapshot(include_driver=False)
    calls = []

    def statistics(year, month):
        calls.append(("statistics", year, month))
        return build_monthly_meal_statistics(snapshot, year, month)

    def history(year, month):
        calls.append(("history", year, month))
        return MonthlyMealHistory(snapshot.revision_id, year, month, ())

    dialog = MonthlyOverviewDialog(date(2026, 12, 1), statistics, history)
    assert calls == [("statistics", 2026, 12)]
    dialog.tabs.setCurrentIndex(1)
    assert calls[-1] == ("history", 2026, 12)
    dialog.history._set_column_search("name", "예제")
    dialog._shift(1)
    assert calls[-1] == ("history", 2027, 1)
    dialog.tabs.setCurrentIndex(0)
    assert calls[-1] == ("statistics", 2027, 1)
    dialog.tabs.setCurrentIndex(1)
    assert dialog.history._column_searches["name"] == "예제"
    assert dialog.history.error.isHidden()
    dialog.close()


def test_column_manager_visibility_reset_and_shared_entry(window):
    dialog = ColumnManagerDialog(window)
    index = next(i for i, column in enumerate(window.table_model.columns) if column.key == "address")
    item = next(dialog.columns.item(i) for i in range(dialog.columns.count())
                if dialog.columns.item(i).data(Qt.UserRole) == "address")
    item.setCheckState(Qt.Unchecked)
    assert window._table.isColumnHidden(index)
    dialog._reset()
    assert not window._table.isColumnHidden(index)
    called = []
    window.household_shared_column_requested.connect(called.append)
    assert dialog.columns.currentItem() is None
    assert dialog.shared_button.text() == "칼럼 가구 공용 설정"
    assert dialog.shared_button.isEnabled()
    dialog.shared_button.click()
    assert called == []
    item = next(dialog._shared_list.item(i) for i in range(dialog._shared_list.count())
                if dialog._shared_list.item(i).data(Qt.UserRole) == "address")
    assert (item.checkState() == Qt.Checked) == ("address" in window.table_model._household_shared_columns)
    dialog._shared_menu.close()
    assert called == []
    dialog.shared_button.click()
    item = next(dialog._shared_list.item(i) for i in range(dialog._shared_list.count())
                if dialog._shared_list.item(i).data(Qt.UserRole) == "address")
    item.setCheckState(Qt.Unchecked if item.checkState() == Qt.Checked else Qt.Checked)
    assert called == ["address"]


def test_driver_replacement_remembers_selected_id_and_blocks_unsaved_order(app):
    drivers = (Driver(uuid4(), "예제 기사1"), Driver(uuid4(), "예제 기사2"))
    dialog = DriverRegistryDialog(RoutingState(drivers))
    dialog.table.selectRow(1)
    dialog.replacement_button.click()
    assert dialog.request() == DriverReplacementLaunch(drivers[1].driver_id)
    dialog = DriverRegistryDialog(RoutingState(drivers))
    dialog.table.move_row(0, 2)
    dialog.replacement_button.click()
    assert dialog.result() != QDialog.Accepted and "먼저 저장" in dialog.error.text()
    dialog.mark_saved()
    dialog.close()


def test_new_menu_has_twelve_jobs_and_filter_is_outside_more(window):
    assert len(window._more_menu.actions()) == 4
    actions = [action for group in window._more_menu.actions() for action in group.menu().actions()]
    assert len(actions) == 12
    assert window._special_only_action not in actions
    assert window._special_only_filter.isVisible()


def test_shared_columns_many_items_scroll_without_filling_screen(app):
    from dataclasses import replace
    from src.domain.roster import CustomFieldDefinition, CustomFieldType
    roster = _roster()
    start = max((field.display_order for field in roster.custom_fields), default=-1) + 1
    fields = tuple(CustomFieldDefinition(uuid4(), f"칼럼 {i}", CustomFieldType.TEXT, start + i) for i in range(100))
    window = RosterWindow(replace(roster, custom_fields=roster.custom_fields + fields))
    window.show()
    dialog = ColumnManagerDialog(window)
    dialog.show()
    dialog._shared()
    app.processEvents()
    assert dialog._shared_list.count() >= 100
    assert dialog._shared_list.height() <= 360
    scrollbar = dialog._shared_list.verticalScrollBar()
    assert scrollbar.maximum() > 0
    scrollbar.setValue(scrollbar.maximum())
    app.processEvents()
    assert scrollbar.value() == scrollbar.maximum()
    dialog._shared_menu.close()
    dialog.close()
    window.close()
