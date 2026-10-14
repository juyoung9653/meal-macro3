"""Date editors that keep mouse-wheel input from changing their value."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QDateEdit, QDateTimeEdit, QWidget


class _CalendarWheelGuard(QObject):
    """Consume wheel input inside an open calendar popup."""

    def eventFilter(self, watched, event) -> bool:
        if event.type() == QEvent.Type.Wheel:
            event.accept()
            return True
        return super().eventFilter(watched, event)


class _WheelSafeDateTimeMixin:
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._calendar_wheel_guard = _CalendarWheelGuard(self)

    def wheelEvent(self, event: QWheelEvent) -> None:
        # Keep the value stable while allowing a containing scroll area to move.
        event.ignore()

    def setCalendarPopup(self, enabled: bool) -> None:
        super().setCalendarPopup(enabled)
        if not enabled:
            return
        calendar = self.calendarWidget()
        calendar.installEventFilter(self._calendar_wheel_guard)
        for child in calendar.findChildren(QWidget):
            child.installEventFilter(self._calendar_wheel_guard)


class WheelSafeDateEdit(_WheelSafeDateTimeMixin, QDateEdit):
    """A date editor that ignores wheel changes at any focus state."""


class WheelSafeDateTimeEdit(_WheelSafeDateTimeMixin, QDateTimeEdit):
    """A date-time editor that ignores wheel changes at any focus state."""
