"""Small overlay handles for the roster's collapsible panel boundaries."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QPoint, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QToolButton, QWidget


class PanelEdgeHandle(QToolButton):
    """A roomy click target with a narrow, centered visible tab."""

    def __init__(self, host: QWidget, boundary: QWidget, *, points_right: bool) -> None:
        super().__init__(host)
        self._host = host
        self._boundary = boundary
        self._points_right = points_right
        self.setObjectName("panelEdgeToggle")
        self.setFixedSize(30, 84)
        self.setCheckable(True)
        self.setChecked(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        host.installEventFilter(self)
        boundary.installEventFilter(self)
        self.reposition()

    def set_points_right(self, value: bool) -> None:
        self._points_right = value
        self.update()

    def reposition(self) -> None:
        if self._host is not self._boundary and not self._host.isAncestorOf(self._boundary):
            return
        point = self._boundary.mapTo(self._host, QPoint(0, 0))
        x = point.x() - (self.width() - 2) // 2
        y = (self._host.height() - self.height()) // 2
        self.move(max(0, min(x, self._host.width() - self.width())),
                  max(0, min(y, self._host.height() - self.height())))
        self.raise_()

    def eventFilter(self, watched, event) -> bool:
        if watched in (self._host, self._boundary) and event.type() in (
            QEvent.Type.Move, QEvent.Type.Resize, QEvent.Type.Show
        ):
            self.reposition()
        return super().eventFilter(watched, event)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        focused = self.hasFocus()
        hovered = self.underMouse()
        fill = QColor("#BCD8DE" if self.isDown() else "#D6E8EC" if hovered else "#F5F9FA")
        border = QColor("#16788A" if focused else "#87B5BE" if hovered else "#B9CDD2")
        rect = QRectF((self.width() - 14) / 2, 13, 14, 58)
        painter.setPen(QPen(border, 1.5 if focused else 1))
        painter.setBrush(fill)
        painter.drawRoundedRect(rect, 7, 7)
        cx, cy = self.width() / 2, self.height() / 2
        path = QPainterPath()
        dx = 2.5 if self._points_right else -2.5
        path.moveTo(cx - dx / 2, cy - 5)
        path.lineTo(cx + dx / 2, cy)
        path.lineTo(cx - dx / 2, cy + 5)
        painter.setPen(QPen(QColor("#17313D"), 1.8, Qt.PenStyle.SolidLine,
                            Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)
