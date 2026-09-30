"""Integration checks for the configurable roster workspace."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from dataclasses import replace
from datetime import date
from types import SimpleNamespace
from uuid import uuid4

import pytest
from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QApplication
from src.desktop_v2.roster_window import RosterWindow
from src.desktop_v2.application import DesktopStartupController
from src.domain import (RosterSnapshot, Person, PersonProfile, Meal, MealEnrollment,
                        DeliveryAssignment, CustomFieldDefinition, CustomFieldType, MealStatus)

@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])

@pytest.fixture
def roster():
    person = Person(uuid4(), PersonProfile('테스트 아동', birth_date=date(2016, 3, 2)))
    return RosterSnapshot(uuid4(), persons=(person,),
        enrollments=tuple(MealEnrollment(person.person_id, meal) for meal in Meal),
        assignments=tuple(DeliveryAssignment(person.person_id, meal, f'{meal.value} 담당') for meal in Meal))

def settings(tmp_path):
    return QSettings(str(tmp_path / 'columns.ini'), QSettings.Format.IniFormat)

def col(window, key):
    return next(i for i, c in enumerate(window.table_model.columns) if c.key == key)

def test_layout_survives_context_sort_and_custom_field_replacement(app, roster, tmp_path):
    store = settings(tmp_path)
    window = RosterWindow(roster, column_settings=store)
    header = window._table.horizontalHeader()
    address = col(window, 'address')
    header.moveSection(header.visualIndex(address), 0)
    header.resizeSection(address, 277)
    menu = window._column_layout.create_menu()
    next(a for a in menu.actions() if a.text() == '학교').trigger()
    window._set_meal(Meal.BREAKFAST)
    window.table_model.sort(col(window, 'name'), Qt.SortOrder.DescendingOrder)
    assert header.logicalIndex(0) == address
    assert window._table.isColumnHidden(col(window, 'school'))
    custom = CustomFieldDefinition(uuid4(), '개별 안내', CustomFieldType.TEXT, 0)
    updated = replace(roster, revision_id=uuid4(), custom_fields=(custom,))
    window.replace_roster(updated)
    assert header.logicalIndex(0) == col(window, 'address')
    assert header.sectionSize(col(window, 'address')) == 277
    assert not window._table.isColumnHidden(col(window, f'custom:{custom.definition_id}'))
    window.close()
    reopened = RosterWindow(updated, column_settings=settings(tmp_path))
    try:
        assert reopened._table.horizontalHeader().logicalIndex(0) == col(reopened, 'address')
        assert reopened._table.isColumnHidden(col(reopened, 'school'))
        for key in ('name', 'birth_date', 'status:breakfast', 'status:lunch', 'status:dinner'):
            assert not reopened._table.isColumnHidden(col(reopened, key))
    finally:
        reopened.close()

def test_meal_cell_context_targets_clicked_meal_and_history(app, roster, tmp_path):
    window = RosterWindow(roster, column_settings=settings(tmp_path))
    window.show()
    app.processEvents()
    person = roster.persons[0]
    requested, complaints = [], []
    window.status_action_requested.connect(lambda *args: requested.append(args))
    window.complaint_requested.connect(lambda *args: complaints.append(args))
    menu = window._create_context_menu(person, Meal.BREAKFAST)
    menu.actions()[1].trigger()
    next(action for action in menu.actions() if action.text() == "민원").trigger()
    assert requested == [(person.person_id, Meal.BREAKFAST, MealStatus.PAUSED)]
    assert complaints == [(person.person_id, Meal.BREAKFAST)]
    breakfast = col(window, 'status:breakfast')
    assert window.table_model.data(window.table_model.index(0, breakfast)) == '조식 담당\n배송'
    assert window._table.rowHeight(0) >= 50
    assert window._table.horizontalHeader().sectionsMovable()
    window.close()

def test_custom_field_cancel_does_not_replace_roster():
    class Cancelled:
        def __init__(self, session):
            pass
        def run(self, *, parent):
            return SimpleNamespace(roster=None)
    controller = DesktopStartupController(custom_field_controller_type=Cancelled)
    controller._run_custom_fields(object(), object())

def test_custom_field_result_refreshes_window(monkeypatch, roster):
    class Completed:
        def __init__(self, session):
            pass
        def run(self, *, parent):
            return SimpleNamespace(roster=roster)
    controller = DesktopStartupController(custom_field_controller_type=Completed)
    calls = []
    monkeypatch.setattr(controller, '_replace_roster_if_changed', lambda *args: calls.append(args))
    session, window = object(), object()
    controller._run_custom_fields(session, window)
    assert calls == [(session, window, roster)]
