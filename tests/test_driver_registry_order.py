"""Driver row identity, saved display order, and ledger boundaries."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from uuid import uuid4

import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QDropEvent
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from src.desktop_v2.delivery_routing import (
    DeliveryRoutingController,
    DriverOrderRequest,
    DriverRegistryDialog,
    _driver_combo,
)
from src.domain.delivery_routing import (
    DRIVER_KIND,
    DRIVER_ORDER_ID,
    DRIVER_ORDER_KIND,
    Driver,
    RoutingEvent,
    RoutingState,
    build_driver_order_revision,
    build_driver_revision,
    routing_state_from_snapshot,
)
from src.domain.revisions import EntityRef, HistoricalSnapshot
from src.domain.roster import Meal
from src.storage.database_session import DatabaseSession
from src.storage.encrypted_database import RevisionConflictError
from tests.test_delivery_routing import DAY, args, base_roster, initialize, loaded
from tests.test_desktop_delivery_routing import Messages


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def register(session, driver):
    roster, state = loaded(session)
    session.append_revision(build_driver_revision(
        roster, state, driver_id=driver.driver_id, name=driver.name,
        telephone=driver.telephone, **args(),
    ))


def test_saved_order_reopens_and_keeps_history_assignment_identity_and_new_append(tmp_path):
    path = tmp_path / "order.sqlite3"
    first, second = Driver(uuid4(), "가기사"), Driver(uuid4(), "나기사")
    with DatabaseSession.create(path, "password", "recovery") as session:
        initialize(session, base_roster())
        register(session, second)
        register(session, first)
        roster, state = loaded(session)
        assert state.drivers == (first, second)  # Legacy snapshots retain name sorting.
        old_revision = roster.revision_id
        order = (second.driver_id, first.driver_id)
        record = build_driver_order_revision(roster, state, ordered_driver_ids=order, **args())
        stale = build_driver_order_revision(roster, state, ordered_driver_ids=order, **args())
        session.append_revision(record)
        with pytest.raises(RevisionConflictError):
            session.append_revision(stale)
        roster, state = loaded(session)
        assert state.drivers == (second, first)
        assert build_driver_order_revision(roster, state, ordered_driver_ids=order, **args()) is None
        previous = routing_state_from_snapshot(session.database.snapshot(old_revision))
        assert previous.drivers == (first, second)
        person_id = roster.persons[0].person_id
        event = RoutingEvent(uuid4(), person_id, DAY, 1, {Meal.DINNER: second.driver_id})
        assert RoutingState(state.drivers, (event,), state.driver_order).resolve(roster, person_id, DAY).driver_ids[Meal.DINNER] == second.driver_id
        third = Driver(uuid4(), "가기사를 앞서는 이름")
        register(session, third)
    with DatabaseSession.open(path, "password") as session:
        roster, state = loaded(session)
        assert state.drivers == (second, first, third)
        assert not state.events
        from src.services.revision_history import build_revision_change_rows
        snapshot = session.database.snapshot(roster.revision_id)
        rows = build_revision_change_rows(record, snapshot)
        text = str(rows)
        assert "기사 표시 순서" in text and "나기사 → 가기사" in text
        assert str(first.driver_id) not in text


@pytest.mark.parametrize("order", ["bad", ("bad",), (), ("duplicate",), ("unknown",)])
def test_invalid_saved_order_is_observable(order):
    driver = Driver(uuid4(), "기사")
    if order == ("duplicate",):
        order = (str(driver.driver_id), str(driver.driver_id))
    elif order == ("unknown",):
        order = (str(uuid4()),)
    if order == ():
        order = (123,)
    snapshot = HistoricalSnapshot(uuid4(), {
        EntityRef(DRIVER_KIND, driver.driver_id): {"name": driver.name, "telephone": ""},
        EntityRef(DRIVER_ORDER_KIND, DRIVER_ORDER_ID): {"ordered_driver_ids": order},
    })
    with pytest.raises((TypeError, ValueError)):
        routing_state_from_snapshot(snapshot)


def test_driver_edit_and_order_are_one_revision_and_keep_before_state(tmp_path):
    with DatabaseSession.create(tmp_path / "edit.sqlite3", "password", "recovery") as session:
        initialize(session, base_roster())
        first, second = Driver(uuid4(), "동명", "111"), Driver(uuid4(), "동명", "222")
        register(session, first)
        register(session, second)
        roster, state = loaded(session)
        order = (second.driver_id, first.driver_id)
        session.append_revision(build_driver_order_revision(roster, state, ordered_driver_ids=order, **args()))
        roster, state = loaded(session)
        record = build_driver_revision(roster, state, driver_id=second.driver_id,
            name="수정 기사", telephone="222", ordered_driver_ids=tuple(reversed(order)), **args())
        assert len(record.changes) == 2
        session.append_revision(record)
        _, state = loaded(session)
        assert tuple(driver.driver_id for driver in state.drivers) == tuple(reversed(order))
        assert state.drivers[1].name == "수정 기사"
        assert state.drivers[0] == first


def test_body_drop_moves_complete_row_and_edit_uses_uuid(app):
    first, second = Driver(uuid4(), "동명", "111"), Driver(uuid4(), "동명", "222")
    dialog = DriverRegistryDialog(RoutingState((first, second)))
    dialog.show()
    app.processEvents()
    dialog.table.selectRow(0)
    point = dialog.table.visualItemRect(dialog.table.item(1, 0)).bottomLeft()

    class InternalDrop(QDropEvent):
        def source(self):
            return dialog.table

    event = InternalDrop(QPointF(point), Qt.DropAction.MoveAction,
        dialog.table.mimeData(dialog.table.selectedItems()), Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier)
    dialog.table.dropEvent(event)
    assert event.isAccepted()
    assert dialog.ordered_driver_ids() == (second.driver_id, first.driver_id)
    assert dialog.table.item(0, 1).text() == "222"
    assert dialog.order_button.isEnabled()
    dialog.table.selectRow(0)
    assert dialog.phone.text() == "222"
    dialog.name.setText("수정 기사")
    dialog._save(True)
    assert dialog.request().driver_id == second.driver_id
    assert dialog.request().telephone == "222"
    combo = _driver_combo(RoutingState((second, first)), first.driver_id)
    assert [combo.itemData(index) for index in (1, 2)] == [str(second.driver_id), str(first.driver_id)]
    dialog.table.move_row(0, 2)
    dialog.close()


def test_pending_close_cancel_discard_save_and_noop(app, monkeypatch):
    state = RoutingState((Driver(uuid4(), "가기사"), Driver(uuid4(), "나기사")))
    dialog = DriverRegistryDialog(state)
    dialog.table.move_row(0, 2)
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Cancel)
    dialog.reject()
    assert dialog.result() != QDialog.DialogCode.Accepted
    assert dialog._order_pending()
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Save)
    dialog.reject()
    assert isinstance(dialog.request(), DriverOrderRequest)
    assert dialog.result() == QDialog.DialogCode.Accepted
    discarded = DriverRegistryDialog(state)
    discarded.table.move_row(0, 2)
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Discard)
    discarded.reject()
    assert discarded.result() == QDialog.DialogCode.Rejected
    unchanged = DriverRegistryDialog(state)
    unchanged.table.move_row(0, 2)
    unchanged.table.move_row(1, 0)
    assert not unchanged.order_button.isEnabled()
    unchanged._save_order()
    assert unchanged.result() != QDialog.DialogCode.Accepted
    unchanged.close()


def test_native_drag_start_owns_move_without_qt_deleting_source_cells(app, monkeypatch):
    from PySide6.QtTest import QTest
    import src.desktop_v2.delivery_routing as module

    first, second = Driver(uuid4(), "가기사", "111"), Driver(uuid4(), "나기사", "222")
    dialog = DriverRegistryDialog(RoutingState((first, second)))
    dialog.show()
    app.processEvents()
    executions = []

    class SimulatedDrag:
        def __init__(self, table):
            self.table = table
        def setMimeData(self, data):
            self.data = data
        def exec(self, action):
            executions.append(action)
            class InternalDrop(QDropEvent):
                def source(event):
                    return self.table
            point = self.table.visualItemRect(self.table.item(1, 0)).bottomLeft()
            event = InternalDrop(QPointF(point), action, self.data,
                Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
            self.table.dropEvent(event)
            return Qt.DropAction.MoveAction

    monkeypatch.setattr(module, "QDrag", SimulatedDrag)
    viewport = dialog.table.viewport()
    start = dialog.table.visualItemRect(dialog.table.item(0, 0)).center()
    QTest.mousePress(viewport, Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(viewport, start + QPoint(0, 15))
    QTest.mouseMove(viewport, start + QPoint(0, 30))
    QTest.mouseRelease(viewport, Qt.MouseButton.LeftButton, pos=start)
    assert executions == [Qt.DropAction.MoveAction]
    assert dialog.ordered_driver_ids() == (second.driver_id, first.driver_id)
    assert [dialog.table.item(row, 1).text() for row in (0, 1)] == ["222", "111"]
    dialog.mark_saved()
    dialog.close()


@pytest.mark.parametrize("failure,expected_message", [
    (OSError("disk full"), "프로그램이 자료를 처리하는 중 예상하지 못한 문제가 발생했습니다."),
    (RevisionConflictError("stale parent"), "기사 관리를 다시 열어"),
])
def test_controller_save_refresh_cancel_and_write_failure(app, tmp_path, failure, expected_message):
    first, second = Driver(uuid4(), "가기사"), Driver(uuid4(), "나기사")
    notices = Messages()
    executions = []

    class Reorder(DriverRegistryDialog):
        def exec(self):
            if executions:
                return QDialog.DialogCode.Rejected
            executions.append(True)
            self.table.move_row(0, 2)
            self._save_order()
            return self.result()

    with DatabaseSession.create(tmp_path / "controller.sqlite3", "password", "recovery") as session:
        initialize(session, base_roster())
        register(session, first)
        register(session, second)
        before = len(session.database.revision_summaries())
        refreshed = []
        result = DeliveryRoutingController(session, registry_dialog_type=Reorder, message_box=notices).run_drivers(on_saved=refreshed.append)
        assert result.routing_state.drivers == (second, first)
        assert len(session.database.revision_summaries()) == before + 1
        assert len(refreshed) == 1 and not notices.criticals
        executions.clear()
        def fail(record):
            raise failure
        session.append_revision = fail
        result = DeliveryRoutingController(session, registry_dialog_type=Reorder, message_box=notices).run_drivers()
        assert result.roster is None
        assert expected_message in notices.criticals[0][-1]
        assert loaded(session)[1].drivers == (second, first)
        assert len(session.database.revision_summaries()) == before + 1
