"""Selected history cancellation stays atomic across domain, storage and UI."""
from dataclasses import replace
from datetime import date
from uuid import uuid4

import pytest
from PySide6.QtCore import QItemSelectionModel, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QTableView

from src.desktop_v2.meal_history_cancel import MealHistoryCancelController
from src.desktop_v2.person_history import create_person_history_dialog
from src.desktop_v2.roster_window import RosterWindow
from src.domain.meal_history_cancellation import build_meal_history_cancellations
from src.domain.status import MealStatus
from src.domain.roster import Meal
from src.services.revision_restore import restore_revision
from tests.test_meal_history_cancellation import (
    ACTOR, DAY, STAMP, AcceptedDialog, Messages, initialize, load_revision_chain,
    rows, session, shared_household, status,
)


def prepare_batch(session, targets):
    current = session.database.current_roster()
    return build_meal_history_cancellations(
        session.database.snapshot(current.revision_id),
        load_revision_chain(session.database, current.revision_id), targets,
        actor=ACTOR, recorded_at=STAMP, revision_id=uuid4(),
    )


def test_batch_undo_start_and_stop_in_one_revision_and_restore(session):
    person = initialize(session, enrolled=False)
    started = status(session, person, MealStatus.DELIVERY)
    stopped = status(session, person, MealStatus.STOPPED, date(2026, 10, 2))
    before = session.database.snapshot(stopped.revision_id)
    result, record = prepare_batch(session, (started, stopped, started))
    session.append_revision(record)
    assert len(session.database.revision_summaries()) == 4
    assert result.enrollments == ()
    assert session.database.status_events() == ()
    assert rows(session, person) == ()
    assert session.database.snapshot(stopped.revision_id) == before
    session.database.check_integrity()
    restore_revision(session, stopped.revision_id, result.revision_id, ACTOR)
    assert len(rows(session, person)) == 2


def test_batch_rejects_dependency_without_partial_cancellation(session):
    person = initialize(session, enrolled=False)
    started = status(session, person, MealStatus.DELIVERY)
    stopped = status(session, person, MealStatus.STOPPED, date(2026, 10, 2))
    status(session, person, MealStatus.PAUSED, date(2026, 10, 3), date(2026, 10, 4))
    before = session.database.snapshot(session.database.latest_revision_id())
    with pytest.raises(ValueError, match="다른 상태 기록"):
        prepare_batch(session, (started, stopped))
    assert session.database.snapshot(session.database.latest_revision_id()) == before
    assert len(rows(session, person)) == 3


def test_batch_preserves_unselected_and_rejects_tampering(session):
    person = initialize(session)
    first = status(session, person, MealStatus.STOPPED)
    second = status(session, person, MealStatus.PAUSED, date(2026, 10, 3), date(2026, 10, 4))
    kept = status(session, person, MealStatus.DELIVERY, date(2026, 10, 5))
    _, record = prepare_batch(session, (first, second))
    with pytest.raises(ValueError, match="취소 내용"):
        session.append_revision(replace(record, changes=record.changes[:-1]))
    assert len(rows(session, person)) == 3
    session.append_revision(record)
    assert [e.event_id for e in session.database.status_events()] == [kept.entity_id]
    assert len(rows(session, person)) == 1
    session.database.check_integrity()


def test_batch_checks_final_shared_state(session):
    person = initialize(session)
    other = shared_household(session, person)
    status(session, person, MealStatus.STOPPED)
    roster = session.database.current_roster()
    from src.desktop_v2.meal_history import build_meal_history_entries_by_person
    entries = build_meal_history_entries_by_person(load_revision_chain(session.database, roster.revision_id))
    _, record = prepare_batch(session, (entries[person][0].target, entries[other][0].target))
    session.append_revision(record)
    assert session.database.status_events() == ()
    session.database.check_integrity()


@pytest.mark.parametrize("accept", (True, False))
def test_person_multiselect_survives_context_menu_and_saves_once(session, accept):
    app = QApplication.instance() or QApplication([])
    person = initialize(session)
    first = status(session, person, MealStatus.STOPPED)
    second = status(session, person, MealStatus.PAUSED, date(2026, 10, 3), date(2026, 10, 4))
    roster = session.database.current_roster()
    dialog = create_person_history_dialog(session, roster.person(person), Meal.DINNER)
    table = dialog.findChild(QTableView, "history_식종 변경")
    table.selectionModel().select(table.model().index(1, 0),
        QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)
    emitted = []
    dialog.meal_history_cancel_requested.connect(emitted.append)
    menu = dialog._meal_history_menu(0)
    assert len(table.selectionModel().selectedRows()) == 2
    assert "2건" in menu.actions()[0].text()
    menu.actions()[0].trigger()
    assert set(emitted[0]) == {first, second}
    class Confirm(AcceptedDialog):
        def __init__(self, names, rows, parent):
            assert len(names) == len(rows) == 2
        def exec(self):
            return QDialog.DialogCode.Accepted if accept else QDialog.DialogCode.Rejected
    messages = Messages()
    result = MealHistoryCancelController(session, batch_dialog_type=Confirm,
        message_box=messages, actor_provider=lambda: ACTOR).run(emitted[0], roster.revision_id)
    assert bool(result) is accept
    assert not messages.errors
    assert len(session.database.revision_summaries()) == (4 if accept else 3)
    dialog.reload_meal_history(session)
    assert table.model().rowCount() == (0 if accept else 2)
    dialog.close()
    app.processEvents()


def test_inspector_ctrl_selection_and_context_target(session, monkeypatch):
    from src.desktop_v2 import roster_window as module
    from src.desktop_v2.inspector_history import InspectorMealHistory
    app = QApplication.instance() or QApplication([])
    person = initialize(session)
    first = status(session, person, MealStatus.STOPPED)
    second = status(session, person, MealStatus.PAUSED, date(2026, 10, 3), date(2026, 10, 4))
    third = status(session, person, MealStatus.DELIVERY, date(2026, 10, 5))
    roster = session.database.current_roster()
    history = InspectorMealHistory(session.database)
    window = RosterWindow(roster)
    window.set_meal_change_history_provider(lambda item: history.rows_for(roster, item.person_id))
    window._populate_meal_history(roster.person(person))
    window.show()
    app.processEvents()
    view = window._meal_history_list
    for index, modifier in ((0, Qt.KeyboardModifier.NoModifier), (1, Qt.KeyboardModifier.ControlModifier)):
        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, modifier,
                         view.visualItemRect(view.item(index)).center())
    assert len(view.selectedItems()) == 2
    emitted = []
    window.meal_history_cancel_requested.connect(lambda target, revision: emitted.append(target))
    class Menu(module.QMenu):
        def exec(self, *args):
            self.actions()[0].trigger()
    monkeypatch.setattr(module, "QMenu", Menu)
    window._open_meal_history_context_menu(view.visualItemRect(view.item(0)).center())
    assert set(emitted[-1]) == {second, third}
    window._open_meal_history_context_menu(view.visualItemRect(view.item(2)).center())
    assert emitted[-1] == first
    assert len(view.selectedItems()) == 1
    window.close()
