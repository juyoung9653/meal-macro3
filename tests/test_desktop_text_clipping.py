"""Text in narrow Qt controls retains its original value without elision."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QComboBox, QLabel, QLineEdit, QListWidget, QTabBar, QTableWidget, QTableWidgetItem

from src.desktop_v2.theme import application_stylesheet, register_application_fonts


def test_views_and_tabs_disable_qt_text_elision():
    app = QApplication.instance() or QApplication([])
    register_application_fonts()
    previous_stylesheet = app.styleSheet()
    app.setStyleSheet(application_stylesheet())
    text = "긴 텍스트가 좁은 영역의 밖까지 이어집니다"
    table = QTableWidget(1, 1)
    table.setItem(0, 0, QTableWidgetItem(text))
    listing = QListWidget()
    listing.addItem(text)
    combo = QComboBox()
    icon_image = QPixmap(12, 12)
    icon_image.fill(Qt.GlobalColor.red)
    combo.addItem(QIcon(icon_image), text)
    editable_combo = QComboBox()
    editable_combo.setEditable(True)
    editable_combo.addItem(text)
    table.setHorizontalHeaderLabels([text])
    table.horizontalHeaderItem(0).setIcon(QIcon(icon_image))
    tabs = QTabBar()
    tabs.addTab(text)
    edit = QLineEdit()
    edit.setPlaceholderText(text)
    for widget in (table, listing, combo, editable_combo, tabs, edit):
        widget.resize(80, 50)
        widget.show()
    app.processEvents()
    assert table.textElideMode() == Qt.TextElideMode.ElideNone
    assert listing.textElideMode() == Qt.TextElideMode.ElideNone
    assert combo.view().textElideMode() == Qt.TextElideMode.ElideNone
    assert editable_combo.view().textElideMode() == Qt.TextElideMode.ElideNone
    assert tabs.elideMode() == Qt.TextElideMode.ElideNone
    assert table.item(0, 0).text() == listing.item(0).text() == combo.currentText() == tabs.tabText(0) == text
    assert editable_combo.currentText() == text and not editable_combo.grab().isNull()
    overlay = edit.findChild(QLabel, "fullPlaceholderText")
    assert overlay is not None and overlay.text() == text and overlay.isVisible()
    QTest.mouseClick(edit, Qt.MouseButton.LeftButton)
    QTest.keyClicks(edit, "input")
    assert edit.text() == "input" and not overlay.isVisible()
    edit.clear()
    assert overlay.isVisible() and edit.placeholderText() == text
    assert not combo.grab().isNull()
    assert not table.horizontalHeader().grab().isNull()
    for widget in (table, listing, combo, editable_combo, tabs, edit):
        widget.close()
    app.setStyleSheet(previous_stylesheet)
