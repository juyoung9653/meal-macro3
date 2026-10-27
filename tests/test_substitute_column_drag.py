"""Dragging print columns retains fallback editors, selection and hidden slots."""

from PySide6.QtCore import QPoint, QPointF, QSettings, Qt
from PySide6.QtGui import QDropEvent
from PySide6.QtTest import QTest

import src.desktop_v2.delivery_substitute as module
from tests.test_substitute_column_selection import make_dialog
from tests.test_delivery_substitute_v2 import app, session


def test_native_drag_preserves_column_settings_and_hidden_slots(app, session, tmp_path, monkeypatch):
    store = QSettings(str(tmp_path / "drag.ini"), QSettings.Format.IniFormat)
    dialog, _ = make_dialog(session, tmp_path)
    dialog.set_preferences_scope("drag-db", settings=store)
    combo = dialog._fallback_combos["road_address"]
    combo.setCurrentIndex(combo.findData("detail_address"))
    visible = {"sequence", "name", "road_address"}
    dialog.set_visible_columns(visible)
    original = dialog._column_keys()
    selected = dialog.columns.topLevelItem(original.index("name"))
    selected.setCheckState(0, Qt.CheckState.Unchecked)
    checked = set(dialog._selected_keys(include_hidden=True))
    fallbacks = dialog._fallback_values()
    dialog.show()
    app.processEvents()
    tree = dialog.columns
    tree.setCurrentItem(tree.topLevelItem(original.index("sequence")))
    executions = []

    class InternalDrop(QDropEvent):
        def source(self):
            return tree

    class SimulatedDrag:
        def __init__(self, parent):
            assert parent is tree
        def setMimeData(self, data):
            self.data = data
        def exec(self, action):
            executions.append(action)
            target = tree.topLevelItem(dialog._column_keys().index("road_address"))
            point = tree.visualItemRect(target).bottomLeft()
            event = InternalDrop(QPointF(point), action, self.data,
                Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
            tree.dropEvent(event)
            assert event.isAccepted()
            assert tree.currentItem().data(0, Qt.ItemDataRole.UserRole) == "sequence"
            return Qt.DropAction.MoveAction

    monkeypatch.setattr(module, "QDrag", SimulatedDrag)
    start = tree.visualItemRect(tree.currentItem()).center()
    start.setX(65)
    viewport = tree.viewport()
    QTest.mousePress(viewport, Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(viewport, start + QPoint(0, 15))
    QTest.mouseMove(viewport, start + QPoint(0, 30))
    QTest.mouseRelease(viewport, Qt.MouseButton.LeftButton, pos=start)
    app.processEvents()
    assert executions == [Qt.DropAction.MoveAction]
    moved = dialog._column_keys()
    assert [key for key in moved if key in visible] == ["name", "road_address", "sequence"]
    assert all(moved[index] == key for index, key in enumerate(original) if key not in visible)
    assert set(dialog._selected_keys(include_hidden=True)) == checked
    assert dialog._fallback_values() == fallbacks
    assert not dialog._fallback_combos["name"].isEnabled()
    assert all(tree.topLevelItem(index).childCount() == 0 for index in range(tree.topLevelItemCount()))
    assert dialog.options().columns == ("road_address", "sequence")
    dialog.accept()
    restored, _ = make_dialog(session, tmp_path)
    restored.set_preferences_scope("drag-db", settings=store)
    restored.set_visible_columns(visible)
    assert restored._column_keys() == moved
    assert restored.options() == dialog.options()
    restored._move_column("sequence", 0)
    assert restored.options().columns == ("sequence", "road_address")
    restored.reject()
    reopened, _ = make_dialog(session, tmp_path)
    reopened.set_preferences_scope("drag-db", settings=store)
    assert reopened._column_keys() == moved
    reopened.reject()


def test_external_drop_and_same_position_do_not_change_order(app, session, tmp_path):
    dialog, _ = make_dialog(session, tmp_path)
    tree = dialog.columns
    tree.setCurrentItem(tree.topLevelItem(0))
    original = dialog._column_keys()
    event = QDropEvent(QPointF(0, 0), Qt.DropAction.MoveAction,
        tree.mimeData(tree.selectedItems()), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    tree.dropEvent(event)
    assert not event.isAccepted()
    dialog._move_column(original[0], 1)
    assert dialog._column_keys() == original
    dialog.reject()
