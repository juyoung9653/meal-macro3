"""Header sorting cycles through ascending, descending, and roster order."""

import os
from dataclasses import replace
from uuid import UUID

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QPoint, QSettings, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from src.desktop_v2.roster_window import RosterTableModel, RosterWindow
from src.domain.roster import (
    Person,
    PersonProfile,
    RosterSnapshot,
)
import src.desktop_v2.inactive_roster as inactive_roster_module
from tests.test_desktop_v2_roster import CUSTOM_FIELD_ID, _histories, _roster


@pytest.fixture(autouse=True)
def isolated_archive_preference(monkeypatch, tmp_path):
    settings = QSettings(str(tmp_path / "archive.ini"), QSettings.Format.IniFormat)
    monkeypatch.setattr(inactive_roster_module, "QSettings", lambda *_args, **_kwargs: settings)


def _column(model, key):
    return next(index for index, column in enumerate(model.columns) if column.key == key)


def _names(model):
    return [model.person_at(row).profile.name for row in range(model.rowCount())]


def _click_header_label(header, logical_index):
    table = header.parentWidget()
    table.scrollTo(table.model().index(0, logical_index))
    QApplication.processEvents()
    point = QPoint(
        header.sectionViewportPosition(logical_index)
        + header.sectionSize(logical_index) // 2,
        header.height() // 2,
    )
    QTest.mouseClick(header.viewport(), Qt.MouseButton.LeftButton, pos=point)


def test_header_click_cycles_sort_and_preserves_filters_and_layout(tmp_path):
    app = QApplication.instance() or QApplication([])
    window = RosterWindow(
        _roster(),
        _histories(),
        column_settings=QSettings(str(tmp_path / "sort-cycle.ini"), QSettings.IniFormat),
    )
    window.show()
    app.processEvents()
    header = window._column_search_header
    model = window.table_model
    name = next(index for index, column in enumerate(model.columns) if column.key == "name")
    birth_date = next(
        index for index, column in enumerate(model.columns) if column.key == "birth_date"
    )
    model.set_column_values("name", frozenset({"홍길동", "김아침"}))
    header.moveSection(header.visualIndex(name), 3)
    moved_visual_index = header.visualIndex(name)

    assert header.sortIndicatorSection() == -1
    assert [model.person_at(row).profile.name for row in range(model.rowCount())] == [
        "홍길동",
        "김아침",
    ]

    _click_header_label(header, name)
    assert header.sortIndicatorSection() == name
    assert header.sortIndicatorOrder() == Qt.SortOrder.AscendingOrder
    assert [model.person_at(row).profile.name for row in range(model.rowCount())] == [
        "김아침",
        "홍길동",
    ]

    _click_header_label(header, name)
    assert header.sortIndicatorOrder() == Qt.SortOrder.DescendingOrder
    assert [model.person_at(row).profile.name for row in range(model.rowCount())] == [
        "홍길동",
        "김아침",
    ]

    _click_header_label(header, name)
    assert header.sortIndicatorSection() == -1
    assert [model.person_at(row).profile.name for row in range(model.rowCount())] == [
        "홍길동",
        "김아침",
    ]
    assert model.selected_column_values("name") == frozenset({"홍길동", "김아침"})
    assert header.visualIndex(name) == moved_visual_index

    _click_header_label(header, birth_date)
    assert header.sortIndicatorSection() == birth_date
    assert header.sortIndicatorOrder() == Qt.SortOrder.AscendingOrder
    assert header.visualIndex(name) == moved_visual_index
    window.close()


def test_multiple_header_priorities_cycle_independently_and_filters_do_not_sort(tmp_path):
    app = QApplication.instance() or QApplication([])
    window = RosterWindow(_roster(), column_settings=QSettings(str(tmp_path / "multi.ini"), QSettings.IniFormat))
    window.show()
    app.processEvents()
    model, header = window.table_model, window._column_search_header
    ascending, descending = Qt.SortOrder.AscendingOrder, Qt.SortOrder.DescendingOrder
    try:
        for key in ("name", "birth_date", "school"):
            _click_header_label(header, _column(model, key))
        assert model.sort_criteria == (("name", ascending), ("birth_date", ascending), ("school", ascending))
        _click_header_label(header, _column(model, "birth_date"))
        assert model.sort_criteria[1] == ("birth_date", descending)
        _click_header_label(header, _column(model, "birth_date"))
        assert model.sort_criteria == (("name", ascending), ("school", ascending))
        _click_header_label(header, _column(model, "birth_date"))
        assert model.sort_criteria[-1] == ("birth_date", ascending)
        prior = model.sort_criteria
        QTest.mouseClick(header.buttons["name"], Qt.MouseButton.LeftButton)
        assert model.sort_criteria == prior
        header._menu.close()
        for key in ("school", "name", "birth_date"):
            _click_header_label(header, _column(model, key))
            _click_header_label(header, _column(model, key))
        assert model.sort_criteria == ()
        assert _names(model) == [person.profile.name for person in _roster().persons]
    finally:
        window.close()


def test_secondary_sort_breaks_primary_ties_and_complete_ties_keep_roster_order():
    # Names intentionally oppose grade order, exposing the old name/UUID tie breaker.
    rows = (("다", "A", "1"), ("가", "A", "2"), ("나", "B", "3"), ("라", "A", "2"))
    people = tuple(Person(UUID(int=index + 1), PersonProfile(name, school_name=school, grade=grade))
                   for index, (name, school, grade) in enumerate(rows))
    model = RosterTableModel(RosterSnapshot(UUID(int=99), persons=people))
    model.sort(-1)
    for key in ("school", "grade", "grade"):
        model.cycle_sort(_column(model, key))
    assert _names(model) == ["가", "라", "다", "나"]
    model.cycle_sort(_column(model, "school"))
    assert _names(model) == ["나", "가", "라", "다"]
    model.cycle_sort(_column(model, "school"))
    assert _names(model) == ["나", "가", "라", "다"]
    model.cycle_sort(_column(model, "grade"))
    assert _names(model) == ["다", "가", "나", "라"]
    assert model.sort_criteria == ()
    model.cycle_sort(999)
    assert model.sort_criteria == ()


def test_sort_keys_survive_dynamic_columns_and_removed_columns_are_pruned():
    roster = _roster()
    model = RosterTableModel(roster)
    model.sort(-1)
    custom = f"custom:{CUSTOM_FIELD_ID}"
    for key in ("name", custom, "school"):
        model.cycle_sort(_column(model, key))
    expected = model.sort_criteria
    model.set_special_only(True)
    assert model.sort_criteria == expected
    model.cycle_sort(_column(model, "delivery_settings"))
    model.set_special_only(False)
    assert model.sort_criteria == expected
    model.replace_roster(replace(roster, custom_fields=(), persons=tuple(
        replace(person, profile=replace(person.profile, custom_values={})) for person in roster.persons
    )))
    assert model.sort_criteria == (expected[0], expected[2])
