"""Integration checks for the configurable roster workspace."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from dataclasses import replace
from datetime import date
from types import SimpleNamespace
from uuid import uuid4

import pytest
from PySide6.QtCore import QPoint, QSettings, Qt
from PySide6.QtWidgets import QApplication
from src.desktop_v2.roster_window import RosterWindow
from src.desktop_v2.application import DesktopStartupController
from src.domain.roster import (
    RosterSnapshot,
    Person,
    PersonProfile,
    Meal,
    MealEnrollment,
    DeliveryAssignment,
    CustomFieldDefinition,
    CustomFieldType,
)
from src.domain.status import MealStatus

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
    menu = window._column_layout.create_menu(column_key="school")
    next(a for a in menu.actions() if a.text() == "숨기기").trigger()
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
    window._set_day(date(2026, 10, 16))
    window.show()
    app.processEvents()
    person = roster.persons[0]
    requested, complaints = [], []
    window.status_action_requested.connect(lambda *args: requested.append(args))
    window.complaint_requested.connect(lambda *args: complaints.append(args))
    menu = window._create_context_menu(person, Meal.BREAKFAST)
    next(action for action in menu.actions() if action.text() == "조식 일시중지").trigger()
    next(action for action in menu.actions() if action.text() == "민원").trigger()
    assert requested == [(person.person_id, Meal.BREAKFAST, MealStatus.PAUSED)]
    assert complaints == [(person.person_id, Meal.BREAKFAST)]
    breakfast = col(window, 'status:breakfast')
    assert window.table_model.data(window.table_model.index(0, breakfast)) == '조식 담당\n배송'
    assert window._table.rowHeight(0) >= 50
    assert window._table.horizontalHeader().sectionsMovable()
    window.close()


def test_reordered_header_context_targets_visual_column(app, roster, tmp_path, monkeypatch):
    window = RosterWindow(roster, column_settings=settings(tmp_path))
    header = window._table.horizontalHeader()
    address = col(window, "address")
    header.moveSection(header.visualIndex(address), 0)
    captured = []
    monkeypatch.setattr(
        window,
        "_show_column_menu",
        lambda position, *, column_key=None: captured.append(column_key),
    )

    window._show_header_column_menu(
        QPoint(header.sectionViewportPosition(address) + 4, header.height() // 2)
    )

    assert captured == ["address"]
    window.close()


def test_builtin_alias_and_hidden_labels_persist_per_database(app, roster, tmp_path):
    path = tmp_path / "columns.ini"
    first = RosterWindow(
        roster,
        column_settings=QSettings(str(path), QSettings.Format.IniFormat),
        view_settings_scope="db-a",
    )
    assert first._rename_column("name", "아동명")
    assert first._rename_column("school", "재학 학교")
    assert not first._rename_column("name", "아동명")
    with pytest.raises(ValueError, match="입력"):
        first._rename_column("name", "   ")
    first._column_layout._set_hidden("name", True)
    first._column_layout._set_hidden("school", True)
    first.table_model.set_column_search("name", "테스트")
    first._refresh_column_filter_strip()
    assert "아동명" in first._column_filter_label.text()
    menu = first._column_layout.create_menu()
    hidden_menu = next(
        action.menu() for action in menu.actions()
        if action.text() == "숨기기 해제"
    )
    assert {action.text() for action in hidden_menu.actions()} >= {"아동명", "재학 학교"}
    first.close()

    restored = RosterWindow(
        roster,
        column_settings=QSettings(str(path), QSettings.Format.IniFormat),
        view_settings_scope="db-a",
    )
    labels = {column.key: column.label for column in restored.table_model.columns}
    assert labels["name"] == "아동명"
    assert labels["school"] == "재학 학교"
    assert restored._table.isColumnHidden(col(restored, "name"))
    assert restored._table.isColumnHidden(col(restored, "school"))
    restored.close()

    isolated = RosterWindow(
        roster,
        column_settings=QSettings(str(path), QSettings.Format.IniFormat),
        view_settings_scope="db-b",
    )
    labels = {column.key: column.label for column in isolated.table_model.columns}
    assert labels["name"] == "성명"
    assert labels["school"] == "학교"
    assert not isolated._table.isColumnHidden(col(isolated, "name"))
    isolated.close()


def test_all_hidden_columns_can_be_recovered_from_column_menu(app, roster, tmp_path):
    window = RosterWindow(roster, column_settings=settings(tmp_path))
    window._rename_column("name", "아동명")
    window._column_layout.set_visible_keys(set())
    assert all(
        window._table.isColumnHidden(index)
        for index in range(window.table_model.columnCount())
    )

    menu = window._column_layout.create_menu()
    hidden_menu = next(
        action.menu() for action in menu.actions()
        if action.text() == "숨기기 해제"
    )
    name_action = next(action for action in hidden_menu.actions() if action.text() == "아동명")
    name_action.trigger()

    assert not window._table.isColumnHidden(col(window, "name"))
    window.close()

def test_custom_field_cancel_does_not_replace_roster():
    class Cancelled:
        def __init__(self, session):
            pass
        def run(self, *, parent):
            return SimpleNamespace(roster=None)
    controller = DesktopStartupController(settings=SimpleNamespace(), custom_field_controller_type=Cancelled)
    controller._run_custom_fields(object(), object())

def test_custom_field_result_refreshes_window(monkeypatch, roster):
    class Completed:
        def __init__(self, session):
            pass
        def run(self, *, parent):
            return SimpleNamespace(roster=roster)
    controller = DesktopStartupController(settings=SimpleNamespace(), custom_field_controller_type=Completed)
    calls = []
    monkeypatch.setattr(controller, '_replace_roster_if_changed', lambda *args: calls.append(args))
    session, window = object(), object()
    controller._run_custom_fields(session, window)
    assert calls == [(session, window, roster)]
