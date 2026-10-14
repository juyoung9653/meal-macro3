"""Shared date-editor interaction behavior."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QDate, QDateTime, QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLineEdit, QSpinBox

from src.desktop_v2.date_edit import WheelSafeDateEdit, WheelSafeDateTimeEdit
from src.desktop_v2.import_wizard import RosterImportWizard


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _wheel() -> QWheelEvent:
    return QWheelEvent(
        QPointF(4, 4),
        QPointF(4, 4),
        QPoint(),
        QPoint(0, 120),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.ScrollUpdate,
        False,
    )


@pytest.mark.parametrize("focused", (False, True))
def test_date_edit_rejects_wheel_with_or_without_focus(app, focused):
    editor = WheelSafeDateEdit(QDate(2026, 10, 14))
    other = QLineEdit()
    editor.show()
    other.show()
    (editor if focused else other).setFocus()
    app.processEvents()

    QApplication.sendEvent(editor, _wheel())

    assert editor.date() == QDate(2026, 10, 14)
    editor.close()
    other.close()


def test_date_time_edit_rejects_wheel_but_keeps_keyboard_input(app):
    original = QDateTime(2026, 10, 14, 9, 30, 0)
    editor = WheelSafeDateTimeEdit(original)
    editor.show()
    editor.setFocus()
    app.processEvents()

    QApplication.sendEvent(editor, _wheel())
    assert editor.dateTime() == original

    QTest.keyClick(editor, Qt.Key.Key_Up)
    assert editor.dateTime() != original
    editor.close()


def test_calendar_popup_rejects_wheel_and_still_selects_dates(app):
    editor = WheelSafeDateEdit(QDate(2026, 10, 14))
    editor.setCalendarPopup(True)
    editor.show()
    app.processEvents()
    calendar = editor.calendarWidget()
    original = calendar.selectedDate()
    original_page = (calendar.yearShown(), calendar.monthShown())

    QApplication.sendEvent(calendar, _wheel())
    assert calendar.selectedDate() == original
    assert (calendar.yearShown(), calendar.monthShown()) == original_page

    year_editor = calendar.findChild(QSpinBox)
    assert year_editor is not None
    QApplication.sendEvent(year_editor, _wheel())
    assert (calendar.yearShown(), calendar.monthShown()) == original_page

    selected = original.addDays(2)
    calendar.setSelectedDate(selected)
    calendar.clicked.emit(selected)
    assert editor.date() == selected
    editor.close()


def test_import_wizard_effective_date_uses_wheel_safe_editor(app):
    wizard = RosterImportWizard()
    try:
        editor = wizard.effective_date_edit
        original = editor.date()
        editor.setFocus()
        QApplication.sendEvent(editor, _wheel())
        assert isinstance(editor, WheelSafeDateEdit)
        assert editor.date() == original
    finally:
        wizard.close()
