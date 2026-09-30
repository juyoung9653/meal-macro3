"""Column filter buttons embedded in table headers."""

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QPolygon
from PySide6.QtWidgets import (
    QHeaderView, QLineEdit, QMenu, QWidget, QWidgetAction, QVBoxLayout,
    QHBoxLayout, QPushButton, QListWidget, QListWidgetItem, QLabel, QStyle,
)


class ColumnValuePicker(QWidget):
    applied = Signal(str, object)

    def __init__(self, values, selected, search_text="", parent=None):
        super().__init__(parent)
        self._values = tuple(dict.fromkeys((*values, *sorted(selected or ()))))
        layout = QVBoxLayout(self)
        self.search = QLineEdit(search_text)
        self.search.setPlaceholderText("이 열에서 검색")
        self.search.setAccessibleName("열 검색어")
        self.search.setToolTip("입력한 글자가 포함된 행을 표시합니다. 아래 값 목록도 좁혀집니다.")
        self.search.setClearButtonEnabled(True)
        layout.addWidget(self.search)
        controls = QHBoxLayout()
        self.select_all = QPushButton("전체 선택")
        self.select_none = QPushButton("전체 해제")
        controls.addWidget(self.select_all)
        controls.addWidget(self.select_none)
        layout.addLayout(controls)
        self.list = QListWidget()
        self.list.setMinimumSize(250, 220)
        self.list.setAccessibleName("열 값 선택")
        for value in self._values:
            item = QListWidgetItem(value.replace("\n", " · ") if value else "(빈 값)")
            item.setData(Qt.ItemDataRole.UserRole, value)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked if selected is None or value in selected
                else Qt.CheckState.Unchecked
            )
            self.list.addItem(item)
        layout.addWidget(self.list)
        layout.addWidget(QLabel("선택한 값 중 하나와 일치하는 행을 표시합니다."))
        buttons = QHBoxLayout()
        self.reset = QPushButton("필터 해제")
        self.apply = QPushButton("적용")
        self.cancel = QPushButton("취소")
        self.apply.setObjectName("primaryAction")
        for button in (self.select_all, self.select_none, self.reset, self.cancel):
            button.setObjectName("quietAction")
        for button in (self.reset, self.apply, self.cancel):
            buttons.addWidget(button)
        layout.addLayout(buttons)
        self.search.textChanged.connect(self._search_values)
        self.select_all.clicked.connect(lambda: self._check_all(Qt.CheckState.Checked))
        self.select_none.clicked.connect(lambda: self._check_all(Qt.CheckState.Unchecked))
        self.apply.clicked.connect(self._apply)
        self.reset.clicked.connect(lambda: self.applied.emit("", None))
        self._search_values(search_text)

    def _search_values(self, text):
        query = " ".join(text.split()).casefold()
        for index in range(self.list.count()):
            item = self.list.item(index)
            value = " ".join(item.data(Qt.ItemDataRole.UserRole).split()).casefold()
            item.setHidden(query not in value)

    def _check_all(self, state):
        for index in range(self.list.count()):
            self.list.item(index).setCheckState(state)

    def _apply(self):
        selected = frozenset(
            self.list.item(index).data(Qt.ItemDataRole.UserRole)
            for index in range(self.list.count())
            if self.list.item(index).checkState() == Qt.CheckState.Checked
        )
        self.applied.emit(
            self.search.text(),
            None if selected == frozenset(self._values) else selected,
        )


class ColumnSearchHeader(QHeaderView):
    search_changed = Signal(str, str)
    values_changed = Signal(str, object)
    BUTTON_WIDTH = 24

    def __init__(self, parent=None, *, values_provider=None, selection_provider=None,
                 search_provider=None):
        super().__init__(Qt.Orientation.Horizontal, parent)
        self.setSectionsClickable(True)
        self._values_provider = values_provider
        self._selection_provider = selection_provider
        self._search_provider = search_provider
        self.buttons: dict[str, QPushButton] = {}
        self._menu = None
        self._keys: list[str] = []
        self.sectionResized.connect(self.update_positions)
        self.sectionMoved.connect(self.update_positions)
        self.geometriesChanged.connect(self.update_positions)

    def replace_columns(self, columns):
        keys = [column.key for column in columns]
        for key in list(self.buttons):
            if key not in keys:
                self.buttons.pop(key).deleteLater()
        self._keys = keys
        for column in columns:
            if column.key not in self.buttons:
                button = QPushButton(self.viewport())
                button.setObjectName("columnFilterButton")
                button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowDown))
                button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
                button.clicked.connect(
                    lambda checked=False, key=column.key: self.open_value_filter(key)
                )
                self.buttons[column.key] = button
            button = self.buttons[column.key]
            button.setAccessibleName(f"{column.label} 필터")
            self._update_button(column.key)
        self.update_positions()
        self.viewport().update()

    def _update_button(self, key):
        active = bool(self._search_provider and self._search_provider(key)) or (
            self._selection_provider is not None
            and self._selection_provider(key) is not None
        )
        button = self.buttons[key]
        button.setProperty("filterActive", active)
        button.setToolTip("필터 열기" if not active else "필터 적용 중 · 클릭하여 변경")
        background = "#D8EDF0" if active else "transparent"
        border = "2px solid #16788A" if active else "0"
        button.setStyleSheet(
            f"QPushButton {{ border: 0; border-bottom: {border}; background: {background}; }}"
            "QPushButton:hover, QPushButton:focus { background: #D9E9ED; }"
        )

    def refresh_buttons(self):
        for key in self._keys:
            self._update_button(key)

    def open_value_filter(self, key):
        if self._values_provider is None or key not in self.buttons:
            return
        if self._menu is not None:
            self._menu.close()
            self._menu.deleteLater()
        menu = QMenu(self)
        self._menu = menu
        picker = ColumnValuePicker(
            self._values_provider(key),
            self._selection_provider(key) if self._selection_provider else None,
            self._search_provider(key) if self._search_provider else "",
            menu,
        )
        action = QWidgetAction(menu)
        action.setDefaultWidget(picker)
        menu.addAction(action)
        picker.applied.connect(
            lambda search, values: self._apply_filter(key, search, values, menu)
        )
        picker.cancel.clicked.connect(menu.close)
        button = self.buttons[key]
        menu.popup(button.mapToGlobal(button.rect().bottomLeft()))
        picker.search.setFocus()

    def _apply_filter(self, key, search, values, menu):
        self.search_changed.emit(key, search)
        self.values_changed.emit(key, values)
        self._update_button(key)
        menu.close()

    def paintSection(self, painter, rect, logical_index):
        painter.save()
        painter.setClipRect(rect.adjusted(0, 0, -self.BUTTON_WIDTH, 0))
        super().paintSection(painter, rect, logical_index)
        painter.restore()
        if self.isSortIndicatorShown() and self.sortIndicatorSection() == logical_index:
            x = rect.right() - self.BUTTON_WIDTH - 8
            y = rect.center().y()
            if self.sortIndicatorOrder() == Qt.SortOrder.AscendingOrder:
                points = (QPoint(x - 4, y + 2), QPoint(x + 4, y + 2), QPoint(x, y - 3))
            else:
                points = (QPoint(x - 4, y - 2), QPoint(x + 4, y - 2), QPoint(x, y + 3))
            painter.save()
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(self.palette().text())
            painter.drawPolygon(QPolygon(points))
            painter.restore()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.update_positions()

    def scrollContentsBy(self, dx, dy):
        super().scrollContentsBy(dx, dy)
        self.update_positions()

    def update_positions(self, *args):
        for index, key in enumerate(self._keys):
            button = self.buttons[key]
            visible = index < self.count() and not self.isSectionHidden(index)
            button.setVisible(visible)
            if visible:
                button.setGeometry(
                    self.sectionViewportPosition(index) + self.sectionSize(index)
                    - self.BUTTON_WIDTH - 2,
                    2,
                    self.BUTTON_WIDTH,
                    max(0, self.height() - 4),
                )
                button.raise_()
