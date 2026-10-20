"""Expandable action groups contained by the roster window."""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame, QMenu, QScrollArea, QSizePolicy, QToolButton, QVBoxLayout, QWidget,
)


class MoreActionsPanel(QFrame):
    action_selected = Signal()

    def __init__(self, menu: QMenu, parent=None):
        super().__init__(parent)
        self.setObjectName("moreActionsPanel")
        self.setStyleSheet("""
            QFrame#moreActionsPanel { background: #FFFFFF; border: 1px solid #CAD8DD; border-radius: 5px; }
            QToolButton { color: #17313D; background: #FFFFFF; border: none; border-radius: 3px; padding: 6px; text-align: left; }
            QToolButton:hover, QToolButton:focus { background: #EDF3F5; }
            QToolButton:checked { background: #DDF0F2; color: #16788A; }
            QToolButton:disabled { color: #82949A; }
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        groups_layout = QVBoxLayout(content)
        groups_layout.setContentsMargins(0, 0, 0, 0)
        groups_layout.setSpacing(3)
        self.group_buttons = []
        self.group_bodies = []
        self.action_buttons = []
        for group in menu.actions():
            toggle = QToolButton(content)
            toggle.setText(group.text())
            toggle.setCheckable(True)
            toggle.setArrowType(Qt.ArrowType.RightArrow)
            toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            toggle.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            toggle.setAccessibleName(group.text() + " 펼치기")
            groups_layout.addWidget(toggle)
            body = QWidget(content)
            body_layout = QVBoxLayout(body)
            body_layout.setContentsMargins(20, 0, 0, 4)
            body_layout.setSpacing(2)
            for action in group.menu().actions():
                button = QToolButton(body)
                button.setDefaultAction(action)
                button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
                button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
                button.pressed.connect(self.action_selected.emit)
                body_layout.addWidget(button)
                self.action_buttons.append(button)
            body.hide()
            toggle.toggled.connect(
                lambda expanded, button=toggle, panel=body: self._expand(button, panel, expanded)
            )
            self.group_buttons.append(toggle)
            self.group_bodies.append(body)
            groups_layout.addWidget(body)
        groups_layout.addStretch(1)
        scroll.setWidget(content)
        layout.addWidget(scroll)
        self.hide()

    @staticmethod
    def _expand(button, body, expanded):
        body.setVisible(expanded)
        button.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)
        button.setAccessibleName(button.text() + (" 접기" if expanded else " 펼치기"))

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.action_selected.emit()
            event.accept()
            return
        super().keyPressEvent(event)
