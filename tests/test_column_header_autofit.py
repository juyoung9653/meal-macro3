from types import SimpleNamespace

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QHeaderView, QTableView

from src.desktop_v2.column_search import ColumnSearchHeader


def _autofit_header(sort_criteria=(), *, long_title=False, long_body=False):
    app = QApplication.instance() or QApplication([])
    table = QTableView()
    model = QStandardItemModel(1, 2, table)
    title = "필터 버튼 너비를 포함해야 하는 긴 열 제목" if long_title else "대상"
    body = "본문 데이터가 헤더보다 훨씬 긴 항목" if long_body else "값"
    model.setHorizontalHeaderLabels([title, "보조"])
    model.setItem(0, 0, QStandardItem(body))
    model.setItem(0, 1, QStandardItem("값"))
    table.setModel(model)
    header = ColumnSearchHeader(table)
    table.setHorizontalHeader(header)
    header.replace_columns([
        SimpleNamespace(key="target", label="대상"),
        SimpleNamespace(key="other", label="보조"),
    ])
    header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    header.set_sort_criteria(sort_criteria)
    header.resizeSection(0, 50)
    table.resize(500, 180)
    table.show()
    app.processEvents()
    return app, table, header


def test_native_header_double_click_autofit_reserves_filter_button():
    app, table, header = _autofit_header(long_title=True)
    index = 0
    start = header.sectionViewportPosition(index)
    boundary = start + header.sectionSize(index) - 1
    body_width = table.sizeHintForColumn(index)

    QTest.mouseDClick(
        header.viewport(), Qt.MouseButton.LeftButton,
        pos=QPoint(boundary, header.height() // 2),
    )
    app.processEvents()

    title_width = QHeaderView.sectionSizeFromContents(header, index).width()
    assert header.sectionSize(index) >= title_width + header.BUTTON_WIDTH + 2
    assert header.sectionSize(index) >= body_width
    table.close()


def test_native_header_double_click_still_fits_wider_body_content():
    app, table, header = _autofit_header(long_body=True)
    index = 0
    body_width = table.sizeHintForColumn(index)
    boundary = header.sectionViewportPosition(index) + header.sectionSize(index) - 1

    QTest.mouseDClick(
        header.viewport(), Qt.MouseButton.LeftButton,
        pos=QPoint(boundary, header.height() // 2),
    )
    app.processEvents()

    assert header.sectionSize(index) >= body_width
    table.close()


def test_autofit_size_includes_active_multi_sort_priority_label():
    app, table, header = _autofit_header([
        ("other", Qt.SortOrder.AscendingOrder),
        ("target", Qt.SortOrder.DescendingOrder),
    ])

    content_width = super(ColumnSearchHeader, header).sectionSizeFromContents(0).width()
    priority_width = header.fontMetrics().horizontalAdvance("↓2") + 6
    assert header.sectionSizeFromContents(0).width() == (
        content_width + header.BUTTON_WIDTH + 2 + priority_width
    )
    table.close()
