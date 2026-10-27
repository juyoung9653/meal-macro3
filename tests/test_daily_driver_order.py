"""Per-meal output order persists with history and retains driver identity."""

import os
from dataclasses import replace
from datetime import date
from uuid import uuid4

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QDropEvent
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from src.desktop_v2.daily_driver_order import DailyDriverOrderDialog
from src.desktop_v2.delivery_routing import DeliveryRoutingController, DriverRegistryDialog, DailyDriverOrderLaunch
from src.domain.daily_driver_order import (
    DAILY_DRIVER_ORDER_KIND, build_daily_driver_order_revision, daily_driver_orders,
    saved_daily_driver_orders, daily_driver_order_id,
)
from src.domain.delivery_routing import DRIVER_KIND, Driver, build_driver_order_revision, routing_state_from_snapshot
from src.domain.projections import roster_from_historical_snapshot, roster_entity_states
from src.domain.revisions import EntityRef, HistoricalSnapshot
from src.domain.roster import Meal
from src.services.sqlite_delivery_report_service import prepare_delivery_report_data
from src.services.revision_history import build_revision_change_rows
from src.storage.database_session import DatabaseSession
from src.storage.encrypted_database import RevisionConflictError
from tests.test_delivery_routing import args, base_roster, initialize, loaded
from tests.test_driver_registry_order import register
from tests.test_desktop_delivery_routing import Messages
from tests.test_sqlite_delivery_report_service import _snapshot


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_persist_reopen_rename_new_driver_history_and_common_order(tmp_path):
    path = tmp_path / "daily-order.sqlite3"
    first, second, third = (Driver(uuid4(), name) for name in ("가기사", "나기사", "다기사"))
    with DatabaseSession.create(path, "password", "recovery") as session:
        initialize(session, base_roster())
        for driver in (first, second, third):
            register(session, driver)
        roster, routing = loaded(session)
        original = session.database.snapshot(roster.revision_id)
        default = (first.driver_id, second.driver_id, third.driver_id)
        orders = daily_driver_orders(original, routing)
        assert all(value == default for value in orders.values())
        orders[Meal.BREAKFAST] = tuple(reversed(default))
        orders[Meal.DINNER] = (second.driver_id, first.driver_id, third.driver_id)
        record = build_daily_driver_order_revision(roster, original, routing, orders=orders, **args())
        assert len(record.changes) == 2
        session.append_revision(record)
        with pytest.raises(RevisionConflictError):
            session.append_revision(build_daily_driver_order_revision(roster, original, routing, orders=orders, **args()))
        roster, routing = loaded(session)
        snapshot = session.database.snapshot(roster.revision_id)
        assert build_daily_driver_order_revision(roster, snapshot, routing, orders=orders, **args()) is None
        assert set(saved_daily_driver_orders(snapshot, routing)) == {Meal.DINNER, Meal.BREAKFAST}
        history = str(build_revision_change_rows(record, snapshot))
        assert "일일식수 기사 순서" in history and "다기사 → 나기사 → 가기사" in history
        assert str(first.driver_id) not in history
        assert daily_driver_orders(original, routing)[Meal.BREAKFAST] == default
        new_common = (third.driver_id, first.driver_id, second.driver_id)
        session.append_revision(build_driver_order_revision(roster, routing, ordered_driver_ids=new_common, **args()))
        register(session, replace(second, name="이름 변경 기사"))
        newcomer = Driver(uuid4(), "가나다 새기사")
        register(session, newcomer)
    with DatabaseSession.open(path, "password") as session:
        roster, routing = loaded(session)
        restored = daily_driver_orders(session.database.snapshot(roster.revision_id), routing)
        assert restored[Meal.BREAKFAST] == orders[Meal.BREAKFAST] + (newcomer.driver_id,)
        assert restored[Meal.DINNER] == orders[Meal.DINNER] + (newcomer.driver_id,)
        assert restored[Meal.LUNCH] == new_common + (newcomer.driver_id,)
        assert routing._drivers_by_id[second.driver_id].name == "이름 변경 기사"


def report_source():
    source, people = _snapshot(people=2, same_household=False, meals=(Meal.DINNER, Meal.BREAKFAST))
    roster = roster_from_historical_snapshot(source)
    roster = replace(roster, assignments=tuple(
        replace(item, driver="가기사" if item.person_id == people[0] else "나기사")
        for item in roster.assignments))
    first, second = uuid4(), uuid4()
    entities = dict(source.entities)
    entities.update(roster_entity_states(roster))
    entities[EntityRef(DRIVER_KIND, first)] = {"name": "가기사", "telephone": "111"}
    entities[EntityRef(DRIVER_KIND, second)] = {"name": "나기사", "telephone": "222"}
    return HistoricalSnapshot(source.revision_id, entities), first, second


def test_daily_report_uses_independent_meal_order_and_explicit_override():
    source, first, second = report_source()
    entities = dict(source.entities)
    entities[EntityRef(DAILY_DRIVER_ORDER_KIND, daily_driver_order_id(Meal.BREAKFAST))] = {
        "meal": Meal.BREAKFAST.value, "ordered_driver_ids": (str(second), str(first))}
    changed = HistoricalSnapshot(source.revision_id, entities)
    day = date(2026, 10, 2)
    for meal, names in ((Meal.DINNER, ["가기사", "나기사"]), (Meal.BREAKFAST, ["나기사", "가기사"])):
        delivery_day = date(2026, 10, 1) if meal == Meal.DINNER else day
        report = prepare_delivery_report_data(changed, delivery_day, delivery_day, (meal,))
        assert [row.staff_label.splitlines()[0] for row in report.reports[0].rows] == names
    old = prepare_delivery_report_data(source, day, day, (Meal.BREAKFAST,))
    assert [row.staff_label.splitlines()[0] for row in old.reports[0].rows] == ["가기사", "나기사"]
    explicit = prepare_delivery_report_data(changed, day, day, (Meal.BREAKFAST,), staff_sort={Meal.BREAKFAST: ("가기사", "나기사")})
    assert explicit.calculation_manifest["staff_order"][Meal.BREAKFAST.value] == ("가기사", "나기사")


@pytest.mark.parametrize("invalid", ["duplicate", "missing", "unknown"])
def test_reject_invalid_order(invalid):
    snapshot, first, second = report_source()
    order = {"duplicate": (first, first), "missing": (first,), "unknown": (first, uuid4())}[invalid]
    with pytest.raises(ValueError):
        build_daily_driver_order_revision(roster_from_historical_snapshot(snapshot), snapshot,
            routing_state_from_snapshot(snapshot), orders={Meal.DINNER: order}, **args())


def test_dialog_drag_copy_close_and_registry_launch(app, monkeypatch):
    snapshot, first, second = report_source()
    routing = routing_state_from_snapshot(snapshot)
    dialog = DailyDriverOrderDialog(snapshot, routing)
    assert not dialog.save_button.isEnabled()
    dialog.show()
    app.processEvents()
    table = dialog.tables[Meal.BREAKFAST]
    table.selectRow(0)
    class InternalDrop(QDropEvent):
        def source(self):
            return table
    event = InternalDrop(QPointF(table.visualItemRect(table.item(1, 0)).bottomLeft()),
        Qt.DropAction.MoveAction, table.mimeData(table.selectedItems()),
        Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    table.dropEvent(event)
    assert event.isAccepted()
    assert dialog.request()[Meal.BREAKFAST] == (second, first)
    assert dialog.request()[Meal.DINNER] == (first, second)
    assert table.item(0, 1).text() == "222"
    dialog.copy_order(Meal.BREAKFAST, (Meal.DINNER,))
    assert dialog.request()[Meal.DINNER] == (second, first)
    assert dialog.request()[Meal.LUNCH] == (first, second)
    monkeypatch.setattr(QMessageBox, "question", lambda *a: QMessageBox.StandardButton.Cancel)
    dialog.reject()
    assert dialog.isVisible()
    monkeypatch.setattr(QMessageBox, "question", lambda *a: QMessageBox.StandardButton.Save)
    dialog.reject()
    assert dialog.result() == QDialog.DialogCode.Accepted
    registry = DriverRegistryDialog(routing)
    registry.daily_order_button.click()
    assert isinstance(registry.request(), DailyDriverOrderLaunch)


@pytest.mark.parametrize("failure", [None, OSError("disk full"), RevisionConflictError("stale")])
def test_controller_atomic_save_cancel_and_retains_failed_edit(app, tmp_path, failure):
    messages = Messages()
    instances = []
    class Edit(DailyDriverOrderDialog):
        def exec(self):
            if self in instances:
                assert self.request()[Meal.BREAKFAST] != self._saved[Meal.BREAKFAST]
                assert self.error.text()
                return QDialog.DialogCode.Rejected
            instances.append(self)
            self.tables[Meal.BREAKFAST].move_row(0, 2)
            return QDialog.DialogCode.Accepted
    with DatabaseSession.create(tmp_path / "controller.sqlite3", "password", "recovery") as session:
        initialize(session, base_roster())
        for name in ("가기사", "나기사"):
            register(session, Driver(uuid4(), name))
        before = len(session.database.revision_summaries())
        if failure:
            def fail(_record):
                raise failure
            session.append_revision = fail
        outcome = DeliveryRoutingController(session, daily_order_dialog_type=Edit, message_box=messages).run_daily_order()
        assert len(instances) == 1
        assert len(session.database.revision_summaries()) == before + (failure is None)
        assert bool(messages.criticals) == (failure is not None)
        assert (outcome.roster is not None) == (failure is None)
        roster, routing = loaded(session)
        saved = saved_daily_driver_orders(session.database.snapshot(roster.revision_id), routing)
        assert set(saved) == (set() if failure else {Meal.BREAKFAST})
