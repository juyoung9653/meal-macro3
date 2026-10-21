"""Weekday-restricted dates for the meal-status period editor."""

from __future__ import annotations

from collections.abc import Iterable

from PySide6.QtCore import QDate, QEvent, Qt
from PySide6.QtGui import QKeySequence, QPalette, QTextCharFormat, QValidator
from PySide6.QtWidgets import QCalendarWidget, QDateTimeEdit, QTableView

from .date_edit import WheelSafeDateEdit


class _DeliveryCalendar(QCalendarWidget):
    """Reject unavailable cells before Qt selects or activates them."""

    def __init__(self, weekdays: frozenset[int], parent) -> None:
        super().__init__(parent)
        self._weekdays = weekdays
        # The enclosing date editor provides direct typing. Qt's separate calendar
        # date navigator would bypass the weekday-aware editor validator.
        self.setDateEditEnabled(False)
        self.setVerticalHeaderFormat(QCalendarWidget.VerticalHeaderFormat.NoVerticalHeader)
        self._view = self.findChild(QTableView)
        self._viewport = self._view.viewport()
        self._view.installEventFilter(self)
        self._viewport.installEventFilter(self)
        disabled = QTextCharFormat()
        self._unavailable_color = self.palette().color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text)
        disabled.setForeground(self._unavailable_color)
        for weekday in range(7):
            if weekday not in weekdays:
                self.setWeekdayTextFormat(Qt.DayOfWeek(weekday + 1), disabled)

    def _allowed(self, value: QDate) -> bool:
        return (
            value.isValid()
            and self.minimumDate() <= value <= self.maximumDate()
            and value.dayOfWeek() - 1 in self._weekdays
        )

    def paintCell(self, painter, rect, value: QDate) -> None:
        if self._allowed(value):
            super().paintCell(painter, rect, value)
            return
        # Weekday formats alone do not color adjacent-month cells in Qt.
        painter.save()
        painter.setPen(self._unavailable_color)
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, str(value.day()))
        painter.restore()

    def _cell_date(self, position) -> QDate:
        index = self._view.indexAt(position)
        if not index.isValid() or index.row() == 0:
            return QDate()
        first = QDate(self.yearShown(), self.monthShown(), 1)
        first = first.addDays(-((first.dayOfWeek() - self.firstDayOfWeek().value) % 7))
        # Qt displays a complete preceding week when the month starts on the
        # first weekday. Read that first cell instead of assuming its layout.
        if self._view.model().index(1, 0).data() != first.day():
            first = first.addDays(-7)
        return first.addDays((index.row() - 1) * 7 + index.column())

    def eventFilter(self, watched, event) -> bool:
        if watched is self._viewport and event.type() in (
            QEvent.Type.MouseButtonPress,
            QEvent.Type.MouseButtonRelease,
            QEvent.Type.MouseButtonDblClick,
            QEvent.Type.MouseMove,
        ):
            if not self._allowed(self._cell_date(event.position().toPoint())):
                return True
        if watched is self._view and event.type() == QEvent.Type.KeyPress:
            key = event.key()
            current = self.selectedDate()
            if key in (Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down):
                days = {
                    Qt.Key.Key_Left: -1,
                    Qt.Key.Key_Right: 1,
                    Qt.Key.Key_Up: -7,
                    Qt.Key.Key_Down: 7,
                }[key]
                if self.layoutDirection() == Qt.LayoutDirection.RightToLeft and abs(days) == 1:
                    days = -days
                self._select_available(current.addDays(days), 1 if days > 0 else -1)
                return True
            if key in (Qt.Key.Key_PageUp, Qt.Key.Key_PageDown):
                direction = -1 if key == Qt.Key.Key_PageUp else 1
                candidate = (
                    current.addYears(direction)
                    if event.modifiers() & Qt.KeyboardModifier.ControlModifier
                    else current.addMonths(direction)
                )
                self._select_available(candidate, direction)
                return True
            if key in (Qt.Key.Key_Home, Qt.Key.Key_End):
                candidate = QDate(current.year(), current.month(), 1)
                if key == Qt.Key.Key_End:
                    candidate = candidate.addDays(candidate.daysInMonth() - 1)
                self._select_available(candidate, 1 if key == Qt.Key.Key_Home else -1)
                return True
        return super().eventFilter(watched, event)

    def _select_available(self, candidate: QDate, direction: int) -> None:
        for _ in range(7):
            if self._allowed(candidate):
                self.setSelectedDate(candidate)
                return
            candidate = candidate.addDays(direction)


class DeliveryDateEdit(WheelSafeDateEdit):
    """Only commit configured actual delivery weekdays (Python Monday = 0)."""

    def __init__(self, value: QDate, weekdays: Iterable[int]) -> None:
        self._weekdays = frozenset(weekdays)
        if not self._weekdays or not self._weekdays <= set(range(7)):
            raise ValueError("실제 배송 요일은 0부터 6 사이의 요일이 필요합니다.")
        if value.dayOfWeek() - 1 not in self._weekdays:
            raise ValueError("초기 날짜는 실제 배송 요일이어야 합니다.")
        super().__init__(value)
        self.setKeyboardTracking(False)
        # Native QDateTimeEdit interprets each section separator as a commit.
        # Plain line-edit input lets a user finish changing month/year and day
        # before the weekday-aware validator runs on Enter or focus loss.
        self.lineEdit().setValidator(None)
        self.setCalendarPopup(True)
        self.setCalendarWidget(_DeliveryCalendar(self._weekdays, self))
        # Install the existing wheel guard on the replacement calendar as well.
        self.setCalendarPopup(True)

    def selectAll(self) -> None:
        self.lineEdit().selectAll()

    def keyPressEvent(self, event) -> None:
        if (
            event.text() and event.text().isprintable()
            or event.key() in (
                Qt.Key.Key_Backspace, Qt.Key.Key_Delete,
                Qt.Key.Key_Left, Qt.Key.Key_Right,
                Qt.Key.Key_Home, Qt.Key.Key_End,
            )
            or any(event.matches(shortcut) for shortcut in (
                QKeySequence.StandardKey.SelectAll, QKeySequence.StandardKey.Copy,
                QKeySequence.StandardKey.Cut, QKeySequence.StandardKey.Paste,
                QKeySequence.StandardKey.Undo, QKeySequence.StandardKey.Redo,
            ))
        ):
            self.lineEdit().keyPressEvent(event)
            return
        super().keyPressEvent(event)

    def validate(self, text: str, position: int):
        state, text, position = super().validate(text, position)
        if state == QValidator.State.Acceptable:
            value = super().dateTimeFromText(text).date()
            if value.dayOfWeek() - 1 not in self._weekdays:
                state = QValidator.State.Intermediate
        return state, text, position

    def setDate(self, value: QDate) -> None:
        if value.isValid() and value.dayOfWeek() - 1 in self._weekdays:
            super().setDate(value)

    def stepBy(self, steps: int) -> None:
        if not steps:
            return
        candidate = self.date()
        section = self.currentSection()
        if section == QDateTimeEdit.Section.YearSection:
            candidate = candidate.addYears(steps)
        elif section == QDateTimeEdit.Section.MonthSection:
            candidate = candidate.addMonths(steps)
        else:
            candidate = candidate.addDays(steps)
        direction = 1 if steps > 0 else -1
        for _ in range(7):
            if not self.minimumDate() <= candidate <= self.maximumDate():
                return
            if candidate.dayOfWeek() - 1 in self._weekdays:
                self.setDate(candidate)
                return
            candidate = candidate.addDays(direction)
