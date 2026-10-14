"""Selected driver changes preserve the exact cells and commit atomically."""

import os
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog

from src.desktop_v2.application import DesktopStartupController
from src.desktop_v2.delivery_routing import (
    BatchRoutingChangeDialog, DeliveryRoutingController, DeliveryRoutingOutcome,
)
from src.domain.delivery_routing import (
    Driver, RoutingEvent, RoutingState, build_batch_assignment_revision,
    build_driver_revision,
)
from src.domain.roster import Meal, Person, PersonProfile
from src.storage.database_session import DatabaseSession
from tests.test_delivery_routing import BEFORE, DAY, args, base_roster, initialize, loaded
from tests.test_desktop_delivery_routing import Messages
from tests.test_roster_batch_selection import _column, _window


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_ctrl_click_driver_menu_uses_exact_selection_and_unselected_click_resets(app):
    window = _window()
    window.show()
    app.processEvents()
    first = window.table_model.index(0, _column(window, "status:breakfast"))
    second = window.table_model.index(1, _column(window, "status:breakfast"))
    window._table.scrollTo(first)
    app.processEvents()
    QTest.mouseClick(window._table.viewport(), Qt.MouseButton.LeftButton,
                     pos=window._table.visualRect(first).center())
    QTest.mouseClick(window._table.viewport(), Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.ControlModifier,
                     window._table.visualRect(second).center())
    targets = window.selected_meal_targets()
    assert len(targets) == 2
    window._select_for_context_menu(first)
    assert window.selected_meal_targets() == targets
    requests = []
    window.batch_driver_assignment_requested.connect(requests.append)
    menu = window._create_context_menu(window.table_model.person_at(0), Meal.BREAKFAST,
                                       "status:breakfast", targets)
    action = next(action for action in menu.actions() if "배송담당 변경" in action.text())
    action.trigger()
    assert requests == [targets]
    unselected = window.table_model.index(1, _column(window, "status:dinner"))
    window._select_for_context_menu(unselected)
    assert len(window.selected_meal_targets()) == 1
    window.close()


def test_batch_dialog_requires_explicit_driver_and_allows_unassignment(app):
    roster = base_roster()
    driver = Driver(uuid4(), "기사")
    targets = ((roster.persons[0].person_id, Meal.DINNER),)
    dialog = BatchRoutingChangeDialog(roster, RoutingState((driver,)), targets, DAY)
    dialog._save()
    assert dialog.result() != QDialog.DialogCode.Accepted
    dialog.driver.setCurrentIndex(dialog.driver.findData(""))
    dialog._save()
    assert dialog.request().driver_id is None
    assert dialog.request().effective_on == DAY
    dialog.close()


def test_batch_controller_real_form_atomic_exact_cells_cancel_noop_and_future(tmp_path, app):
    original = base_roster()
    first = original.persons[0].person_id
    second = Person(uuid4(), PersonProfile("둘째", road_address="둘째 주소"))
    roster = replace(original, persons=(*original.persons, second))
    new = Driver(uuid4(), "새 기사")
    later = Driver(uuid4(), "예약 기사")
    targets = ((first, Meal.DINNER), (second.person_id, Meal.BREAKFAST), (first, Meal.DINNER))
    messages = Messages()

    class Filled(BatchRoutingChangeDialog):
        def exec(self):
            self.driver.setCurrentIndex(self.driver.findData(str(new.driver_id)))
            self._save()
            return self.result()

    class Cancel(Filled):
        def exec(self):
            return QDialog.DialogCode.Rejected

    class Clear(Filled):
        def exec(self):
            self.driver.setCurrentIndex(self.driver.findData(""))
            self._save()
            return self.result()

    from src.domain.delivery_routing import build_routing_revision
    with DatabaseSession.create(tmp_path / "batch.sqlite3", "password", "recovery") as session:
        initialize(session, roster)
        for driver in (new, later):
            current, state = loaded(session)
            session.append_revision(build_driver_revision(current, state, driver_id=driver.driver_id,
                                                          name=driver.name, telephone="", **args()))
        current, state = loaded(session)
        session.append_revision(build_routing_revision(
            current, state, person_id=first, effective_on=DAY + timedelta(days=2),
            driver_ids={Meal.DINNER: later.driver_id}, event_id=uuid4(), **args(),
        ))
        current, state = loaded(session)
        revision_before = current.revision_id
        records = []
        append = session.append_revision

        def record_append(record):
            records.append(record)
            return append(record)

        session.append_revision = record_append
        controller = DeliveryRoutingController(session, batch_change_dialog_type=Filled, message_box=messages)
        outcome = controller.run_batch_assignment(targets, DAY)
        assert outcome.roster is not None
        assert len(records) == 1
        assert records[0].metadata.parent_revision_id == revision_before
        assert len(records[0].changes) == 2
        assert outcome.roster.assignments == roster.assignments
        assert outcome.routing_state.resolve(outcome.roster, first, BEFORE).drivers[Meal.DINNER] == "기존 기사"
        view = outcome.routing_state.resolve(outcome.roster, first, DAY)
        assert view.driver_ids[Meal.DINNER] == new.driver_id
        assert view.drivers[Meal.LUNCH] == "기존 기사"
        assert view.road_address == "이전 도로"
        assert outcome.routing_state.resolve(outcome.roster, second.person_id, DAY).driver_ids == {
            Meal.BREAKFAST: new.driver_id, Meal.LUNCH: None, Meal.DINNER: None,
        }
        assert outcome.routing_state.resolve(outcome.roster, first, DAY + timedelta(days=2)).driver_ids[Meal.DINNER] == later.driver_id
        assert controller.run_batch_assignment(targets, DAY).roster is None
        assert len(records) == 1
        cancelled = DeliveryRoutingController(session, batch_change_dialog_type=Cancel, message_box=messages)
        assert cancelled.run_batch_assignment(targets, DAY).roster is None
        assert len(records) == 1
        clear = DeliveryRoutingController(session, batch_change_dialog_type=Clear, message_box=messages)
        outcome = clear.run_batch_assignment(((first, Meal.LUNCH),), DAY)
        assert outcome.routing_state.resolve(outcome.roster, first, DAY).drivers[Meal.LUNCH] == ""
        assert len(records) == 2
        assert not messages.criticals


def test_batch_invalid_target_has_no_partial_record():
    roster = base_roster()
    driver = Driver(uuid4(), "기사")
    with pytest.raises(KeyError):
        build_batch_assignment_revision(roster, RoutingState((driver,)),
            targets=((roster.persons[0].person_id, Meal.DINNER), (uuid4(), Meal.LUNCH)),
            driver_id=driver.driver_id, effective_on=DAY, **args())


def test_application_batch_assignment_passes_selected_day_and_refreshes():
    targets = ((uuid4(), Meal.DINNER), (uuid4(), Meal.LUNCH))
    calls = []
    outcome = DeliveryRoutingOutcome()

    class Controller:
        def __init__(self, session):
            calls.append(session)

        def run_batch_assignment(self, selected, day, parent=None):
            calls.append((selected, day, parent))
            return outcome

    controller = object.__new__(DesktopStartupController)
    controller._routing_controller_type = Controller
    controller._apply_routing_outcome = lambda *values: calls.append(values)
    session = object()
    window = SimpleNamespace(table_model=SimpleNamespace(service_day=DAY))
    controller._run_batch_driver_assignment(session, window, targets)
    assert calls == [session, (targets, DAY, window), (session, window, outcome)]
