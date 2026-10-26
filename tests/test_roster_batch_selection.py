import os
from datetime import date
from uuid import UUID

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QItemSelectionModel
from PySide6.QtWidgets import QApplication

from src.desktop_v2.roster_window import RosterWindow
from src.domain.roster import (
    DeliveryAssignment,
    Meal,
    MealEnrollment,
    Person,
    PersonProfile,
    RosterSnapshot,
)
from src.domain.status import MealStatus


FIRST = UUID("90000000-0000-0000-0000-000000000001")
SECOND = UUID("90000000-0000-0000-0000-000000000002")
REVISION = UUID("90000000-0000-0000-0000-000000000003")


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _window() -> RosterWindow:
    roster = RosterSnapshot(
        REVISION,
        persons=(
            Person(FIRST, PersonProfile("첫째")),
            Person(SECOND, PersonProfile("둘째")),
        ),
        enrollments=(
            MealEnrollment(FIRST, Meal.BREAKFAST),
            MealEnrollment(SECOND, Meal.BREAKFAST),
        ),
        assignments=(
            DeliveryAssignment(FIRST, Meal.BREAKFAST, "기사1", "001"),
            DeliveryAssignment(SECOND, Meal.BREAKFAST, "기사2", "002"),
        ),
    )
    window = RosterWindow(roster)
    window._set_day(date(2026, 8, 17))
    return window


def _column(window: RosterWindow, key: str) -> int:
    return next(
        index for index, column in enumerate(window.table_model.columns)
        if column.key == key
    )


def _select(window: RosterWindow, *indexes) -> None:
    selection = window._table.selectionModel()
    selection.setCurrentIndex(indexes[0], QItemSelectionModel.SelectionFlag.ClearAndSelect)
    for index in indexes[1:]:
        selection.select(index, QItemSelectionModel.SelectionFlag.Select)


def test_batch_targets_keep_only_visible_meal_cells_and_emit(app):
    window = _window()
    breakfast = _column(window, "status:breakfast")
    dinner = _column(window, "status:dinner")
    name = _column(window, "name")
    first_breakfast = window.table_model.index(0, breakfast)
    second_breakfast = window.table_model.index(1, breakfast)
    second_dinner = window.table_model.index(1, dinner)
    _select(
        window,
        first_breakfast,
        second_breakfast,
        window.table_model.index(0, name),
        second_dinner,
    )
    window._table.setColumnHidden(dinner, True)

    targets = window.selected_meal_targets()
    assert set(targets) == {(FIRST, Meal.BREAKFAST), (SECOND, Meal.BREAKFAST)}
    menu = window._create_context_menu(
        window.table_model.person_at(0), Meal.BREAKFAST, "status:breakfast", targets
    )
    assert menu.actions()[0].text() == "선택한 식종 2개 · 일시중지"
    requested = []
    window.batch_status_action_requested.connect(lambda *args: requested.append(args))
    menu.actions()[0].trigger()
    assert requested == [(targets, MealStatus.PAUSED)]
    window.close()


def test_context_click_resets_unselected_batch_and_inspector_uses_current_cell(app):
    window = _window()
    breakfast = _column(window, "status:breakfast")
    name = _column(window, "name")
    first_breakfast = window.table_model.index(0, breakfast)
    second_breakfast = window.table_model.index(1, breakfast)
    _select(window, first_breakfast, second_breakfast)
    assert len(window.selected_meal_targets()) == 2

    ordinary = window.table_model.index(1, name)
    window._select_for_context_menu(ordinary)
    assert window.selected_meal_targets() == ()
    assert window._table.selectionModel().selectedIndexes() == [ordinary]
    window._table.selectionModel().setCurrentIndex(
        second_breakfast, QItemSelectionModel.SelectionFlag.ClearAndSelect
    )
    app.processEvents()
    assert window._inspector_name.text() == window.table_model.person_at(1).profile.name
    window.close()
