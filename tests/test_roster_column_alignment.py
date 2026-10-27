"""Header alignment follows column identity across sheets and reopened views."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from dataclasses import replace
from uuid import uuid4
from PySide6.QtCore import Qt, QSettings
from PySide6.QtWidgets import QApplication
from src.desktop_v2.roster_window import RosterWindow
from src.domain.view_layout import validate_view_layout
from tests.test_desktop_v2_roster import _roster


def test_alignment_menu_persistence_sheet_isolation_and_reset(tmp_path):
    app = QApplication.instance() or QApplication([])
    settings = QSettings(str(tmp_path / "alignment.ini"), QSettings.Format.IniFormat)
    roster = _roster()
    window = RosterWindow(roster, column_settings=settings, view_settings_scope="alignment-db")
    layout = window._column_layout
    menu = layout.create_menu(window, column_key="name")
    alignment = next(action.menu() for action in menu.actions() if action.text() == "정렬")
    horizontal, vertical = [action.menu() for action in alignment.actions()]
    next(action for action in horizontal.actions() if action.text() == "오른쪽").trigger()
    next(action for action in vertical.actions() if action.text() == "아래").trigger()
    expected = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom
    assert window.table_model.column_alignment("name") == expected
    column = next(i for i, column in enumerate(window.table_model.columns) if column.key == "name")
    assert window.table_model.data(window.table_model.index(0, column), Qt.ItemDataRole.TextAlignmentRole) == expected
    window._rename_column("name", "이름")
    window.replace_roster(replace(roster, revision_id=uuid4()))
    assert window.table_model.column_alignment("name") == expected
    window._add_sheet("다른 시트", {"name"})
    assert window.table_model.column_alignment("name") != expected
    window._sheet_tabs.setCurrentIndex(0)
    assert window.table_model.column_alignment("name") == expected
    portable = validate_view_layout(window.export_view_layout())
    assert portable["sheets"][0]["layout"]["alignments"]["name"] == int(expected)
    window.close()
    reopened = RosterWindow(roster, column_settings=settings, view_settings_scope="alignment-db")
    assert reopened.table_model.column_alignment("name") == expected
    reopened._column_layout.reset()
    assert reopened.table_model.column_alignment("name") == Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
    reopened.close()
