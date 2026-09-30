"""Header sorting cycles through ascending, descending, and roster order."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QPoint, QSettings, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from src.desktop_v2.roster_window import RosterWindow
from tests.test_desktop_v2_roster import _histories, _roster


def _click_header_label(header, logical_index):
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
