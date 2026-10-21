"""Real Qt interactions cannot commit unavailable delivery weekdays."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QDate, QPoint, QPointF, Qt
from PySide6.QtGui import QPalette, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDateTimeEdit, QLineEdit, QSpinBox, QTableView, QToolButton, QVBoxLayout, QWidget

from src.desktop_v2.delivery_date_edit import DeliveryDateEdit


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def editor(app):
    value = DeliveryDateEdit(QDate(2026, 9, 4), (4,))
    value.setDisplayFormat("yyyy.MM.dd")
    value.resize(180, 32)
    value.show()
    app.processEvents()
    yield value
    value.close()


def _open_calendar(editor, app):
    QTest.mouseClick(editor, Qt.MouseButton.LeftButton, pos=QPoint(editor.width() - 10, editor.height() // 2))
    app.processEvents()
    calendar = editor.calendarWidget()
    assert calendar.isVisible()
    return calendar, calendar.findChild(QTableView)


def _cell(view, day):
    matches = [
        view.model().index(row, column)
        for row in range(1, view.model().rowCount())
        for column in range(view.model().columnCount())
        if view.model().index(row, column).data() == day
    ]
    if len(matches) > 1:
        matches = [index for index in matches if index.row() < 4] if day < 15 else [index for index in matches if index.row() > 2]
    assert len(matches) == 1
    return view.visualRect(matches[0]).center()


@pytest.mark.parametrize("double_click", (False, True))
def test_unavailable_calendar_cell_does_not_select_or_close_popup(app, editor, double_click):
    observed = []
    editor.dateChanged.connect(observed.append)
    calendar, view = _open_calendar(editor, app)
    invalid = _cell(view, 3)
    click = QTest.mouseDClick if double_click else QTest.mouseClick
    click(view.viewport(), Qt.MouseButton.LeftButton, pos=invalid)
    app.processEvents()
    assert editor.date() == QDate(2026, 9, 4)
    assert calendar.selectedDate() == QDate(2026, 9, 4)
    assert calendar.isVisible()
    assert observed == []
    disabled_color = calendar.palette().color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text)
    assert calendar.weekdayTextFormat(Qt.DayOfWeek.Thursday).foreground().color() == disabled_color

    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=_cell(view, 11))
    app.processEvents()
    assert editor.date() == QDate(2026, 9, 11)
    assert not calendar.isVisible()
    assert observed and all(value.dayOfWeek() == 5 for value in observed)


def test_direct_typing_rejects_invalid_commit_then_accepts_valid_date(app, editor):
    observed = []
    editor.dateChanged.connect(observed.append)
    editor.setFocus()
    editor.selectAll()
    QTest.keyClicks(editor, "2026.09.03")
    assert editor.date() == QDate(2026, 9, 4)
    QTest.keyClick(editor, Qt.Key.Key_Return)
    assert editor.date() == QDate(2026, 9, 4)
    assert editor.text() == "2026.09.04"
    editor.setFocus()
    editor.selectAll()
    QTest.keyClicks(editor, "2026.10.02")
    QTest.keyClick(editor, Qt.Key.Key_Return)
    assert editor.date() == QDate(2026, 10, 2)
    assert all(value.dayOfWeek() == 5 for value in observed)


def test_paste_partial_edit_and_focus_loss_validate_before_emitting(app, editor):
    container = QWidget()
    layout = QVBoxLayout(container)
    layout.addWidget(editor)
    other = QLineEdit()
    layout.addWidget(other)
    container.show()
    observed = []
    editor.dateChanged.connect(observed.append)
    try:
        for text, expected in (
            ("2026.09.03", QDate(2026, 9, 4)),
            ("2026.10.", QDate(2026, 9, 4)),
            ("2027.01.01", QDate(2027, 1, 1)),
        ):
            editor.setFocus()
            app.processEvents()
            editor.selectAll()
            app.clipboard().setText(text)
            QTest.keyClick(editor, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
            assert editor.text() == text
            QTest.keyClick(editor, Qt.Key.Key_Tab)
            app.processEvents()
            assert other.hasFocus()
            assert editor.date() == expected
            assert editor.text() == expected.toString("yyyy.MM.dd")
        assert all(value.dayOfWeek() == 5 for value in observed)
    finally:
        editor.setParent(None)
        container.close()


@pytest.mark.parametrize("pending, expected", (
    ("2026.09.03", QDate(2026, 9, 4)),
    ("2026.10.02", QDate(2026, 10, 2)),
))
def test_open_popup_with_pending_text_preserves_weekday_contract(app, editor, pending, expected):
    observed = []
    editor.dateChanged.connect(observed.append)
    editor.selectAll()
    QTest.keyClicks(editor, pending)
    calendar, _ = _open_calendar(editor, app)
    assert editor.date() == expected
    assert calendar.selectedDate() == expected
    assert all(value.dayOfWeek() == 5 for value in observed)


@pytest.mark.parametrize("key, expected", (
    (Qt.Key.Key_Left, QDate(2026, 8, 28)),
    (Qt.Key.Key_Right, QDate(2026, 9, 11)),
    (Qt.Key.Key_Up, QDate(2026, 8, 28)),
    (Qt.Key.Key_Down, QDate(2026, 9, 11)),
    (Qt.Key.Key_Home, QDate(2026, 9, 4)),
    (Qt.Key.Key_End, QDate(2026, 9, 25)),
    (Qt.Key.Key_PageUp, QDate(2026, 7, 31)),
    (Qt.Key.Key_PageDown, QDate(2026, 10, 9)),
))
def test_calendar_keyboard_skips_unavailable_dates(app, editor, key, expected):
    observed = []
    editor.dateChanged.connect(observed.append)
    calendar, view = _open_calendar(editor, app)
    QTest.keyClick(view, key)
    assert calendar.selectedDate() == expected
    QTest.keyClick(view, Qt.Key.Key_Return)
    assert editor.date() == expected
    assert all(value.dayOfWeek() == 5 for value in observed)


def test_calendar_navigation_keeps_selected_date_and_disables_separate_typing(app, editor):
    calendar, view = _open_calendar(editor, app)
    calendar.findChild(QToolButton, "qt_calendar_nextmonth").click()
    assert calendar.monthShown() == 10
    assert editor.date() == QDate(2026, 9, 4)
    year = calendar.findChild(QSpinBox)
    year.setValue(2027)
    QTest.keyClick(year, Qt.Key.Key_Return)
    assert editor.date().dayOfWeek() == 5
    assert not calendar.isDateEditEnabled()
    QTest.keyClicks(view, "20260903")
    assert editor.date().dayOfWeek() == 5


@pytest.mark.parametrize("section, initial, expected", (
    (QDateTimeEdit.Section.DaySection, QDate(2026, 9, 25), QDate(2026, 10, 2)),
    (QDateTimeEdit.Section.MonthSection, QDate(2026, 9, 4), QDate(2026, 10, 9)),
    (QDateTimeEdit.Section.YearSection, QDate(2026, 9, 4), QDate(2027, 9, 10)),
))
def test_editor_steps_preserve_weekday_contract(app, editor, section, initial, expected):
    observed = []
    editor.setDate(initial)
    editor.dateChanged.connect(observed.append)
    editor.setCurrentSection(section)
    QTest.keyClick(editor, Qt.Key.Key_Up)
    assert editor.date() == expected
    QTest.keyClick(editor, Qt.Key.Key_Down)
    assert editor.date().dayOfWeek() == 5
    assert all(value.dayOfWeek() == 5 for value in observed)


def test_custom_weekdays_and_range_limits(app):
    editor = DeliveryDateEdit(QDate(2026, 12, 31), (0, 3))
    editor.setCurrentSection(QDateTimeEdit.Section.DaySection)
    editor.stepBy(1)
    assert editor.date() == QDate(2027, 1, 4)
    editor.stepBy(-1)
    assert editor.date() == QDate(2026, 12, 31)
    editor.setDateRange(QDate(2026, 12, 31), QDate(2027, 1, 4))
    editor.stepBy(-1)
    assert editor.date() == QDate(2026, 12, 31)
    editor.stepBy(1)
    editor.stepBy(1)
    assert editor.date() == QDate(2027, 1, 4)
    editor.close()


def test_replacement_calendar_preserves_wheel_guard(app, editor):
    calendar, view = _open_calendar(editor, app)
    for target in (editor, calendar, view.viewport(), calendar.findChild(QSpinBox)):
        event = QWheelEvent(QPointF(4, 4), QPointF(4, 4), QPoint(), QPoint(0, 120), Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.ScrollUpdate, False)
        QApplication.sendEvent(target, event)
    assert editor.date() == QDate(2026, 9, 4)
    assert (calendar.yearShown(), calendar.monthShown()) == (2026, 9)


@pytest.mark.parametrize("initial, first_weekday, invalid_day, valid_day, expected", (
    (QDate(2026, 11, 6), Qt.DayOfWeek.Sunday, 5, 13, QDate(2026, 11, 13)),
    (QDate(2027, 2, 5), Qt.DayOfWeek.Monday, 4, 12, QDate(2027, 2, 12)),
    (QDate(2027, 1, 1), Qt.DayOfWeek.Sunday, 31, 8, QDate(2027, 1, 8)),
))
def test_calendar_mouse_mapping_handles_week_and_year_boundaries(
    app, editor, initial, first_weekday, invalid_day, valid_day, expected,
):
    editor.setDate(initial)
    editor.calendarWidget().setFirstDayOfWeek(first_weekday)
    calendar, view = _open_calendar(editor, app)
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=_cell(view, invalid_day))
    assert editor.date() == initial
    assert calendar.isVisible()
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=_cell(view, valid_day))
    assert editor.date() == expected
