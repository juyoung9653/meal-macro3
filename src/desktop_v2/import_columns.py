"""Source-column mapping table using stable destination keys."""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QHeaderView, QLabel, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)


class ImportColumnsTable(QWidget):
    destination_changed = Signal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        note = QLabel("엑셀 열마다 값을 넣을 DB 칼럼을 선택하세요. 연결하지 않을 열은 ‘가져오지 않음’, 없는 칼럼은 ‘새 칼럼 추가’를 선택합니다.")
        note.setObjectName("muted")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.table = QTableWidget(0, 3)
        self.table.setAccessibleName("엑셀 열과 DB 칼럼 연결")
        self.table.setHorizontalHeaderLabels(("엑셀 열", "원본 값 예시", "연결할 DB 칼럼"))
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)
        self.table.setMinimumHeight(250)
        self.table.setMaximumHeight(430)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.setColumnWidth(0, 200)
        self.table.setColumnWidth(1, 200)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table)
        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.setObjectName("errorMessage")
        self.error.hide()
        layout.addWidget(self.error)
        self.combos = {}
        self._selected = {}

    def populate(self, inspection, targets, selected):
        self.combos.clear()
        self._selected = dict(selected)
        self.table.setRowCount(len(inspection.columns))
        for row, column in enumerate(inspection.columns):
            item = QTableWidgetItem(column.label)
            item.setToolTip(column.label)
            self.table.setItem(row, 0, item)
            examples = tuple(dict.fromkeys(sample.values[row] for sample in inspection.preview_rows
                                          if row < len(sample.values) and sample.values[row]))[:3]
            item = QTableWidgetItem(" · ".join(value.replace("\n", " / ") for value in examples))
            item.setToolTip("\n".join(examples))
            self.table.setItem(row, 1, item)
            combo = QComboBox()
            combo.setAccessibleName(column.label + " 연결할 DB 칼럼")
            combo.setMaxVisibleItems(18)
            for key, label in targets:
                combo.addItem(label, key)
            value = selected.get(column.letter, "ignore")
            if combo.findData(value) < 0:
                combo.addItem("삭제된 칼럼 — 다시 선택해주세요", value)
            combo.setCurrentIndex(combo.findData(value))
            combo.currentIndexChanged.connect(lambda _index, source=column.letter: self._changed(source))
            self.combos[column.letter] = combo
            self.table.setCellWidget(row, 2, combo)
        self.table.resizeRowsToContents()

    def sync(self, selected):
        self._selected = dict(selected)
        for source, combo in self.combos.items():
            value = selected.get(source, "ignore")
            if combo.findData(value) < 0:
                combo.addItem("삭제된 칼럼 — 다시 선택해주세요", value)
            combo.blockSignals(True)
            combo.setCurrentIndex(combo.findData(value))
            combo.blockSignals(False)

    def set_meal_targets_enabled(self, enabled):
        for combo in self.combos.values():
            for index in range(combo.count()):
                if str(combo.itemData(index)).startswith("meal:"):
                    item = combo.model().item(index)
                    item.setEnabled(enabled)
                    item.setToolTip("" if enabled else "방학 중식은 명단 포함 여부로 중식 배송을 갱신합니다.")

    def _changed(self, source):
        combo = self.combos[source]
        value = combo.currentData()
        duplicate = next((other for other, target in self._selected.items()
                          if other != source and target == value), None)
        if value not in ("ignore", "new") and duplicate is not None:
            self.error.setText(f"이미 {duplicate}열에 연결된 칼럼입니다. 기존 연결을 바꾼 뒤 선택해주세요.")
            self.error.show()
            self.sync(self._selected)
            return
        self.error.hide()
        self._selected[source] = value
        self.destination_changed.emit(source, value)
