"""Driver-household exports freeze the rendered table and reuse the common gate."""

import os
from datetime import date
from types import SimpleNamespace
from uuid import uuid4

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QApplication

from src.desktop_v2.application import DesktopStartupController
from src.desktop_v2.driver_households import DriverHouseholdsDialog
from src.desktop_v2.roster_window import RosterWindow
from src.domain.delivery_routing import Driver
from src.domain.roster import Meal
from src.services.driver_households import DriverHouseholdRow
from tests.test_desktop_v2_roster import _roster


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _meal_row(household, driver, member, meal, sequence, *, address="성남시"):
    return DriverHouseholdRow(
        household, driver.driver_id, driver.name, (uuid4(),), (member,),
        (meal,), address, ("010-001",), (sequence,),
    )


def test_export_freezes_filtered_split_sorted_visual_table(app):
    driver = Driver(uuid4(), "기사A")
    household = uuid4()
    dinner = _meal_row(household, driver, "=가구", Meal.DINNER, "010")
    breakfast = _meal_row(household, driver, "둘째", Meal.BREAKFAST, "001")
    merged = DriverHouseholdRow(
        household, driver.driver_id, driver.name,
        dinner.member_ids + breakfast.member_ids, ("=가구", "둘째"),
        (Meal.DINNER, Meal.BREAKFAST), "성남시", ("010-001",),
        ("010", "001"), (dinner, breakfast),
    )
    other = _meal_row(uuid4(), driver, "제외", Meal.LUNCH, "999", address="분당구")
    dialog = DriverHouseholdsDialog(
        (merged, other), service_day=date(2026, 10, 15), drivers=(driver,),
    )
    try:
        dialog.split_button.click()
        dialog._set_column_search("address", "성남시")
        dialog.table.sortItems(3, Qt.SortOrder.AscendingOrder)
        header = dialog.table.horizontalHeader()
        header.setSectionsMovable(True)
        header.moveSection(header.visualIndex(3), 0)
        dialog.table.setColumnHidden(5, True)
        captured = []
        dialog.export_requested.connect(captured.append)
        dialog.export_button.click()
        assert len(captured) == 1
        snapshot = captured[0]
        assert snapshot.sheet_name == "배송순서관리"
        assert snapshot.headers == ("가구원", "식종", "주소", "중식 시작일", "중식 종료일")
        assert snapshot.rows == (
            ("둘째", "조식", "성남시", "", ""),
            ("=가구", "석식", "성남시", "", ""),
        )
        assert dialog.export_button.objectName() == "quietAction"
    finally:
        dialog.close()


def test_export_allows_header_only_result(app):
    dialog = DriverHouseholdsDialog(
        (), service_day=date(2026, 10, 15), drivers=(),
    )
    try:
        snapshot = dialog.current_view_snapshot()
        assert snapshot.headers == ("가구원", "식종", "주소", "연락처", "중식 시작일", "중식 종료일")
        assert snapshot.rows == ()
    finally:
        dialog.close()


def test_export_uses_include_hidden_result(app):
    driver = Driver(uuid4(), "기사A")
    visible = _meal_row(uuid4(), driver, "표시", Meal.DINNER, "1")
    hidden = _meal_row(uuid4(), driver, "숨김", Meal.DINNER, "2")
    requests = []

    def rows_provider(include_hidden):
        requests.append(include_hidden)
        return (visible, hidden) if include_hidden else (visible,)

    dialog = DriverHouseholdsDialog(
        (visible,), service_day=date(2026, 10, 15), drivers=(driver,),
        rows_provider=rows_provider,
    )
    try:
        assert tuple(row[0] for row in dialog.current_view_snapshot().rows) == ("표시",)
        dialog.include_hidden_checkbox.click()
        assert requests == [True]
        assert tuple(row[0] for row in dialog.current_view_snapshot().rows) == ("표시", "숨김")
    finally:
        dialog.close()


def test_roster_relays_snapshot_with_active_driver_dialog(app):
    window = RosterWindow(_roster())
    captured = []
    window.driver_households_export_requested.connect(
        lambda snapshot, parent: captured.append((snapshot, parent))
    )

    def export_and_close():
        dialog = window.findChild(DriverHouseholdsDialog)
        assert dialog is not None and dialog.isVisible()
        dialog.export_button.click()
        dialog.reject()

    try:
        QTimer.singleShot(0, export_and_close)
        window._show_driver_households()
        assert len(captured) == 1
        assert captured[0][0].sheet_name == "배송순서관리"
        assert captured[0][1].objectName() == "driverHouseholdsDialog"
    finally:
        window.close()


def test_application_uses_active_driver_dialog_as_export_parent(tmp_path):
    session = object()
    window = object()
    dialog = object()
    snapshot = SimpleNamespace(sheet_name="기사별 가구")
    calls = []

    class PasswordController:
        def __init__(self, current_session):
            assert current_session is session

        def ensure_configured(self, parent):
            calls.append(("password", parent))
            return SimpleNamespace(
                completed=True,
                roster=None,
                setting=SimpleNamespace(password="xlsx-password"),
            )

    class SheetController:
        def run(self, *args, **kwargs):
            calls.append((args, kwargs))

    controller = DesktopStartupController(
        settings=SimpleNamespace(document_output_directory=tmp_path),
        export_password_controller_type=PasswordController,
        current_sheet_export_controller_type=SheetController,
    )
    controller._run_current_sheet_export(session, window, snapshot, parent=dialog)
    assert calls == [
        ("password", dialog),
        ((snapshot, "xlsx-password", dialog), {"directory": tmp_path}),
    ]
