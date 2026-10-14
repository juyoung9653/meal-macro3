"""PyQt6 date editor that ignores mouse-wheel changes."""

from PyQt6.QtCore import QEvent, QObject
from PyQt6.QtGui import QWheelEvent
from PyQt6.QtWidgets import QDateEdit, QWidget


class _CalendarWheelGuard(QObject):
    def eventFilter(self, watched, event) -> bool:
        if event.type() == QEvent.Type.Wheel:
            event.accept()
            return True
        return super().eventFilter(watched, event)


class WheelSafeDateEdit(QDateEdit):
    """Keep keyboard and calendar input while rejecting wheel input."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._calendar_wheel_guard = _CalendarWheelGuard(self)

    def wheelEvent(self, event: QWheelEvent) -> None:
        event.ignore()

    def setCalendarPopup(self, enabled: bool) -> None:
        super().setCalendarPopup(enabled)
        if not enabled:
            return
        calendar = self.calendarWidget()
        calendar.installEventFilter(self._calendar_wheel_guard)
        for child in calendar.findChildren(QWidget):
            child.installEventFilter(self._calendar_wheel_guard)
