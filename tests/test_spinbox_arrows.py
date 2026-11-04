"""Real pointer targets must agree with spin-button drawing on native themes."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QDate, QDateTime, QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication, QDateTimeEdit, QDoubleSpinBox, QLineEdit, QSpinBox,
    QStyle, QStyleFactory, QStyleOptionSpinBox, QVBoxLayout, QWidget,
)

from src.desktop_v2.date_edit import WheelSafeDateEdit, WheelSafeDateTimeEdit
from src.desktop_v2.theme import application_stylesheet, register_application_fonts


@pytest.fixture(scope="module")
def app():
    application = QApplication.instance() or QApplication([])
    register_application_fonts()
    return application


@pytest.fixture(scope="module", params=("windows11", "windowsvista", "Windows", "Fusion"))
def native_style(app, request):
    style = QStyleFactory.create(request.param)
    if style is None:
        pytest.skip(f"{request.param} style is unavailable on this platform")
    # Popup widgets can outlive their editor until deferred Qt cleanup. Keep
    # their shared native style alive for the lifetime of the QApplication.
    style.setParent(app)
    return style


@pytest.fixture
def shell(app, native_style):
    style = native_style
    host = QWidget()
    host.setStyle(style)
    host.setStyleSheet(application_stylesheet())
    layout = QVBoxLayout(host)
    other = QLineEdit("다른 입력칸")
    layout.addWidget(other)
    host.resize(380, 160)
    yield host, layout, other, style
    host.close()
    host.deleteLater()
    app.processEvents()


def _show(shell, editor):
    host, layout, other, style = shell
    editor.setStyle(style)
    layout.addWidget(editor)
    host.show()
    QApplication.processEvents()
    return host, other


def _button_rect(editor, control):
    option = QStyleOptionSpinBox()
    editor.initStyleOption(option)
    return editor.style().subControlRect(QStyle.ComplexControl.CC_SpinBox, option, control, editor)


def _click_at(host, editor, point):
    # Sending directly to the spinbox would hide this bug: real clicks hit its
    # child QLineEdit wherever the text editor overlaps a painted arrow.
    target = host.childAt(editor.mapTo(host, point))
    assert target is editor
    QTest.mouseClick(target, Qt.MouseButton.LeftButton, pos=point)
    QApplication.processEvents()


@pytest.mark.parametrize("name", ("", "unitPriceInput", "documentPeriodYear", "monthlyStatisticsYear", "monthlyMealHistoryYear", "holidayReviewYear"))
@pytest.mark.parametrize("focused", (False, True))
def test_numeric_arrow_entire_width_changes_value(shell, name, focused):
    editor = QSpinBox()
    editor.setObjectName(name)
    editor.setRange(0, 10000)
    editor.setSingleStep(500 if name == "unitPriceInput" else 1)
    editor.setSuffix(" 원" if name == "unitPriceInput" else "년")
    host, other = _show(shell, editor)
    for control, direction in ((QStyle.SubControl.SC_SpinBoxUp, 1), (QStyle.SubControl.SC_SpinBoxDown, -1)):
        for fraction in (0.1, 0.5, 0.9):
            editor.setValue(2026)
            (editor if focused else other).setFocus()
            QApplication.processEvents()
            rect = _button_rect(editor, control)
            assert not rect.isEmpty()
            assert not editor.lineEdit().geometry().intersects(rect)
            point = QPoint(rect.left() + int((rect.width() - 1) * fraction), rect.center().y())
            _click_at(host, editor, point)
            assert editor.value() == 2026 + direction * editor.singleStep()


def test_numeric_limits_read_only_and_keyboard_still_work(shell):
    editor = QSpinBox()
    editor.setRange(0, 100)
    host, _ = _show(shell, editor)
    for value, control in ((0, QStyle.SubControl.SC_SpinBoxDown), (100, QStyle.SubControl.SC_SpinBoxUp)):
        editor.setValue(value)
        _click_at(host, editor, _button_rect(editor, control).center())
        assert editor.value() == value
    editor.setValue(50)
    editor.setReadOnly(True)
    _click_at(host, editor, _button_rect(editor, QStyle.SubControl.SC_SpinBoxUp).center())
    assert editor.value() == 50
    editor.setReadOnly(False)
    editor.setFocus()
    editor.selectAll()
    QTest.keyClicks(editor, "25")
    QTest.keyClick(editor, Qt.Key.Key_Return)
    assert editor.value() == 25
    QTest.keyClick(editor, Qt.Key.Key_Up)
    assert editor.value() == 26


@pytest.mark.parametrize("kind,name", (("decimal", ""), ("date", ""), ("date", "unitPriceDate"), ("date", "schoolCalendarDate"), ("datetime", "")))
def test_date_time_and_decimal_arrows(shell, kind, name):
    editor = (QDoubleSpinBox() if kind == "decimal" else
              WheelSafeDateEdit(QDate(2026, 11, 4)) if kind == "date" else
              WheelSafeDateTimeEdit(QDateTime(2026, 11, 4, 9, 30, 0)))
    editor.setObjectName(name)
    host, other = _show(shell, editor)
    if isinstance(editor, QDateTimeEdit):
        editor.setCurrentSection(QDateTimeEdit.Section.DaySection)
        value = lambda: editor.date().day()
    else:
        editor.setValue(25.5)
        value = editor.value
    before = value()
    other.setFocus()
    _click_at(host, editor, _button_rect(editor, QStyle.SubControl.SC_SpinBoxUp).center())
    assert value() == before + 1
    _click_at(host, editor, _button_rect(editor, QStyle.SubControl.SC_SpinBoxDown).center())
    assert value() == before


@pytest.mark.parametrize("name", ("", "serviceDateEdit", "unitPriceDate", "documentPeriodDate", "schoolCalendarDate"))
def test_calendar_popup_still_opens_from_its_button(shell, name):
    editor = WheelSafeDateEdit(QDate(2026, 11, 4))
    editor.setObjectName(name)
    editor.setCalendarPopup(True)
    host, other = _show(shell, editor)
    other.setFocus()
    _click_at(host, editor, _button_rect(editor, QStyle.SubControl.SC_SpinBoxDown).center())
    assert editor.calendarWidget().isVisible()
    editor.calendarWidget().setSelectedDate(QDate(2026, 11, 6))
    editor.calendarWidget().clicked.emit(QDate(2026, 11, 6))
    assert editor.date() == QDate(2026, 11, 6)
    editor.calendarWidget().window().hide()
