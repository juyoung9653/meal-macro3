"""Driver, output columns and paper controls for substitute delivery sheets."""
from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
import json

from PySide6.QtCore import Qt, QSettings
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout,
    QLabel, QListWidget, QListWidgetItem, QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

from src.domain.delivery_substitute import PreparedSubstituteData, SubstituteOptions
from .document_meal import DocumentMealSelection
from .theme import application_stylesheet, register_application_fonts


class DeliverySubstituteDialog(QDialog):
    """All inputs are prepared data; this dialog never reads the database."""

    def __init__(self, document: DocumentMealSelection, data: PreparedSubstituteData,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self._document, self._data = document, data
        self._preferences = None
        self._preferences_key = None
        self.setWindowTitle("배송대타 · 담당과 출력 설정")
        self.setMinimumSize(620, 560)
        self.resize(740, 640)
        register_application_fonts()
        self.setStyleSheet(application_stylesheet())
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)
        title = QLabel(f"{data.day:%Y.%m.%d} · {data.meal.value} 배송대타")
        title.setObjectName("documentCardTitle")
        layout.addWidget(title)
        description = QLabel("실제 배송할 담당자를 선택하고, 출력 칼럼과 페이지를 정해주세요.")
        description.setWordWrap(True)
        layout.addWidget(description)
        form = QFormLayout()
        self.driver = QComboBox()
        self.driver.setObjectName("substituteDriver")
        counts = {}
        for row in data.rows:
            label, households, people = counts.setdefault(row.driver_key, [row.driver_label, set(), 0])
            households.add(row.household_id)
            counts[row.driver_key][2] = people + 1
        for key, (label, households, people) in counts.items():
            self.driver.addItem(f"{label} · {len(households)}가구 · {people}명", key)
        form.addRow("배송담당", self.driver)
        paper = QHBoxLayout()
        self.orientation = QComboBox()
        for label, value in (("세로", "portrait"), ("가로", "landscape")):
            self.orientation.addItem(label, value)
        self.split = QComboBox()
        for label, value in (("가구 수마다", "groups"), ("관할동마다", "dong"), ("구분 없음", "none")):
            self.split.addItem(label, value)
        self.group_limit = QSpinBox()
        self.group_limit.setRange(1, 100)
        self.group_limit.setValue(18)
        self.group_limit.setSuffix(" 가구")
        paper.addWidget(self.orientation)
        paper.addWidget(self.split)
        paper.addWidget(self.group_limit)
        form.addRow("용지 · 페이지", paper)
        layout.addLayout(form)
        label = QLabel("출력 칼럼 · 선택한 칼럼을 목록 순서대로 출력")
        layout.addWidget(label)
        columns_layout = QHBoxLayout()
        self.columns = QListWidget()
        self.columns.setObjectName("substituteColumns")
        by_key = {column.key: column for column in data.columns}
        order = (*data.default_columns, *(column.key for column in data.columns if column.key not in data.default_columns))
        for key in order:
            item = QListWidgetItem(by_key[key].label)
            item.setData(Qt.ItemDataRole.UserRole, key)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if key in data.default_columns else Qt.CheckState.Unchecked)
            self.columns.addItem(item)
        columns_layout.addWidget(self.columns, 1)
        moves = QVBoxLayout()
        for label, direction in (("위로 ↑", -1), ("아래로 ↓", 1)):
            button = QPushButton(label)
            button.setObjectName("quietAction")
            button.clicked.connect(lambda _checked=False, step=direction: self._move(step))
            moves.addWidget(button)
        moves.addStretch()
        columns_layout.addLayout(moves)
        layout.addLayout(columns_layout, 1)
        hint = QLabel("가구는 페이지 중간에서 나누지 않습니다. 긴 내용은 자동으로 줄바꿈합니다.\n관할동·연락처를 출력하지 않아도 가구 집계와 BOX 표시는 유지됩니다.")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.error = QLabel()
        self.error.setWordWrap(True)
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.accept_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.accept_button.setText("저장 위치 선택")
        self.accept_button.setObjectName("primaryAction")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("취소")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setObjectName("quietAction")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.columns.itemChanged.connect(self._refresh)
        self.driver.currentIndexChanged.connect(self._refresh)
        self.split.currentIndexChanged.connect(self._refresh)
        self._refresh()

    def set_preferences_scope(self, database_id: str, *, settings=None) -> None:
        """Use the same operating-file identity as roster view preferences.

        Only field keys and print choices are stored; drivers and source values
        are deliberately absent. Injected dialogs need not implement this hook.
        """
        self._preferences = settings if settings is not None else QSettings("분당구", "급식 배송 운영 SQLite v2")
        self._preferences_key = "documents/substitute_v1/" + sha256(database_id.encode("utf-8")).hexdigest()
        raw = self._preferences.value(self._preferences_key)
        if not isinstance(raw, str):
            return
        try:
            saved = json.loads(raw)
            if not isinstance(saved, dict):
                return
            order, selected = saved.get("order"), saved.get("columns")
            if not isinstance(order, list) or not isinstance(selected, list):
                return
            if any(not isinstance(key, str) for key in (*order, *selected)):
                return
            items = {self.columns.item(index).data(Qt.ItemDataRole.UserRole): self.columns.item(index)
                     for index in range(self.columns.count())}
            order = list(dict.fromkeys(key for key in order if key in items))
            order.extend(key for key in items if key not in order)
            checked = set(selected) & items.keys()
            if not checked:
                checked = set(self._data.default_columns)
            self.columns.blockSignals(True)
            try:
                retained = {self.columns.item(index).data(Qt.ItemDataRole.UserRole): self.columns.takeItem(index)
                            for index in reversed(range(self.columns.count()))}
                for key in order:
                    item = retained[key]
                    item.setCheckState(Qt.CheckState.Checked if key in checked else Qt.CheckState.Unchecked)
                    self.columns.addItem(item)
            finally:
                self.columns.blockSignals(False)
            for combo, key in ((self.orientation, "orientation"), (self.split, "split")):
                index = combo.findData(saved.get(key))
                if index >= 0:
                    combo.setCurrentIndex(index)
            limit = saved.get("groups_per_page")
            if type(limit) is int and 1 <= limit <= 100:
                self.group_limit.setValue(limit)
        except (TypeError, ValueError):
            return
        self._refresh()

    def accept(self) -> None:
        try:
            options = self.selection().substitute_options
        except ValueError:
            self._refresh()
            return
        if self._preferences is not None:
            saved = options.manifest()
            saved.pop("driver_key")
            saved["order"] = [self.columns.item(index).data(Qt.ItemDataRole.UserRole)
                              for index in range(self.columns.count())]
            self._preferences.setValue(self._preferences_key, json.dumps(saved, ensure_ascii=False))
        super().accept()

    def _move(self, direction):
        current = self.columns.currentRow()
        target = current + direction
        if current >= 0 and 0 <= target < self.columns.count():
            item = self.columns.takeItem(current)
            self.columns.insertItem(target, item)
            self.columns.setCurrentRow(target)

    def options(self) -> SubstituteOptions:
        return SubstituteOptions(self.driver.currentData() or "",
            tuple(self.columns.item(index).data(Qt.ItemDataRole.UserRole)
                  for index in range(self.columns.count())
                  if self.columns.item(index).checkState() == Qt.CheckState.Checked),
            self.orientation.currentData(), self.split.currentData(), self.group_limit.value())

    def selection(self) -> DocumentMealSelection:
        options = self.options()
        self._data.selected_rows(options)
        return replace(self._document, substitute_options=options)

    def _refresh(self, *_args):
        self.group_limit.setEnabled(self.split.currentData() != "none")
        try:
            self.selection()
        except ValueError as error:
            self.error.setText(str(error))
            self.accept_button.setEnabled(False)
        else:
            self.error.clear()
            self.accept_button.setEnabled(True)
