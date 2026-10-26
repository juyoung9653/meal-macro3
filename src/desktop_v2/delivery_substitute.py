"""Driver, output columns and paper controls for substitute delivery sheets."""
from __future__ import annotations

from dataclasses import replace
import json

from PySide6.QtCore import Qt, QSize
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout,
    QLabel, QTreeWidget, QTreeWidgetItem, QHeaderView, QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

from src.domain.delivery_substitute import PreparedSubstituteData, SubstituteOptions
from .document_meal import DocumentMealSelection
from .theme import application_stylesheet, register_application_fonts
from .substitute_preferences import preferences


class DeliverySubstituteDialog(QDialog):
    """All inputs are prepared data; this dialog never reads the database."""

    def __init__(self, document: DocumentMealSelection, data: PreparedSubstituteData,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self._document, self._data = document, data
        self._preferences = None
        self._preferences_key = None
        self._visible_keys = {column.key for column in data.columns}
        self._fallback_combos = {}
        self._saved_preferences = {}
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
        label = QLabel("배송대타에 넣을 항목 · 체크한 칼럼을 목록 순서대로 출력")
        layout.addWidget(label)
        columns_layout = QHBoxLayout()
        self.columns = QTreeWidget()
        self.columns.setObjectName("substituteColumns")
        self.columns.setRootIsDecorated(False)
        self.columns.setColumnCount(2)
        self.columns.setHeaderLabels(["출력할 칼럼", "값이 비어 있으면"])
        self.columns.header().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.columns.setTextElideMode(Qt.TextElideMode.ElideNone)
        order = (*data.default_columns, *(column.key for column in data.columns if column.key not in data.default_columns))
        self._rebuild_columns(order, set(data.default_columns), {})
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
        self.visibility_note = QLabel()
        self.visibility_note.setWordWrap(True)
        layout.addWidget(self.visibility_note)
        hint = QLabel("칼럼 이름을 문서 제목으로 사용하고, 값이 없을 때만 오른쪽 칼럼에서 가져옵니다.\n"
                      "출력 선택·순서·대체 칼럼은 이 PC의 운영 파일별로 기억합니다.\n"
                      "가구 집계와 BOX 표시는 원래 명단을 기준으로 유지됩니다.")
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
        self._preferences, self._preferences_key = preferences(database_id, settings)
        raw = self._preferences.value(self._preferences_key)
        if not isinstance(raw, str):
            return
        try:
            saved = json.loads(raw)
            if not isinstance(saved, dict):
                return
            self._saved_preferences = saved
            order, selected = saved.get("order"), saved.get("columns")
            if not isinstance(order, list) or not isinstance(selected, list):
                return
            if any(not isinstance(key, str) for key in (*order, *selected)):
                return
            keys = self._column_keys()
            order = list(dict.fromkeys(key for key in order if key in keys))
            order.extend(key for key in keys if key not in order)
            checked = set(selected) & set(keys)
            if not checked:
                checked = set(self._data.default_columns)
            by_meal = saved.get("fallbacks_by_meal", {})
            fallbacks = by_meal.get(self._data.meal.value, {}) if isinstance(by_meal, dict) else {}
            if not isinstance(fallbacks, dict) or any(not isinstance(key, str) or not isinstance(value, str) for key, value in fallbacks.items()):
                fallbacks = {}
            self._rebuild_columns(order, checked, fallbacks)
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
            saved = {**self._saved_preferences, **options.manifest()}
            saved.pop("driver_key")
            saved.pop("column_labels", None)
            saved.pop("fallbacks", None)
            saved["columns"] = self._selected_keys(include_hidden=True)
            saved["order"] = self._column_keys()
            by_meal = saved.get("fallbacks_by_meal", {})
            saved["fallbacks_by_meal"] = {**(by_meal if isinstance(by_meal, dict) else {}),
                                        self._data.meal.value: self._fallback_values()}
            self._preferences.setValue(self._preferences_key, json.dumps(saved, ensure_ascii=False))
        super().accept()

    def _move(self, direction):
        item = self.columns.currentItem()
        if item is None:
            return
        key = item.data(0, Qt.ItemDataRole.UserRole)
        order = self._column_keys()
        visible = [key for key in order if key in self._visible_keys]
        if key not in visible:
            return
        index = visible.index(key)
        target = index + direction
        if 0 <= target < len(visible):
            first, second = order.index(key), order.index(visible[target])
            order[first], order[second] = order[second], order[first]
            self._rebuild_columns(order, set(self._selected_keys(include_hidden=True)), self._fallback_values())
            self.columns.setCurrentItem(self.columns.topLevelItem(second))

    def _column_keys(self):
        return [self.columns.topLevelItem(index).data(0, Qt.ItemDataRole.UserRole)
                for index in range(self.columns.topLevelItemCount())]

    def _selected_keys(self, *, include_hidden=False):
        return [item.data(0, Qt.ItemDataRole.UserRole)
                for index in range(self.columns.topLevelItemCount())
                if (item := self.columns.topLevelItem(index)).checkState(0) == Qt.CheckState.Checked
                and (include_hidden or not item.isHidden())]

    def _fallback_values(self):
        return {key: combo.currentData() for key, combo in self._fallback_combos.items() if combo.currentData()}

    def _rebuild_columns(self, order, checked, fallbacks):
        self.columns.blockSignals(True)
        self.columns.clear()
        self._fallback_combos = {}
        by_key = {column.key: column for column in self._data.columns}
        for key in order:
            item = QTreeWidgetItem([by_key[key].label, ""])
            item.setSizeHint(0, QSize(0, 36))
            item.setData(0, Qt.ItemDataRole.UserRole, key)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(0, Qt.CheckState.Checked if key in checked else Qt.CheckState.Unchecked)
            self.columns.addTopLevelItem(item)
            item.setHidden(key not in self._visible_keys)
            combo = QComboBox()
            combo.setAccessibleName(f"{by_key[key].label} 값이 비어 있으면")
            combo.addItem("사용 안 함", "")
            for candidate in self._data.columns:
                if candidate.key != key and candidate.key in self._visible_keys:
                    combo.addItem(candidate.label, candidate.key)
            selected = fallbacks.get(key, "")
            if selected and combo.findData(selected) < 0:
                combo.addItem("저장된 대체 칼럼 (현재 목록에 없음)", selected)
            combo.setCurrentIndex(combo.findData(selected))
            combo.currentIndexChanged.connect(self._refresh)
            self.columns.setItemWidget(item, 1, combo)
            self._fallback_combos[key] = combo
        self.columns.blockSignals(False)

    def set_visible_columns(self, keys):
        self._visible_keys = set(keys)
        self._rebuild_columns(self._column_keys(), set(self._selected_keys(include_hidden=True)), self._fallback_values())
        hidden_count = len(self._data.columns) - len(self._visible_keys & set(self._column_keys()))
        self.visibility_note.setText(
            f"모든 시트에서 숨긴 칼럼 {hidden_count}개 제외 · 설정 → 배송대타 칼럼 표시에서 변경"
            if hidden_count else ""
        )
        self._refresh()

    def options(self) -> SubstituteOptions:
        return SubstituteOptions(self.driver.currentData() or "",
            tuple(self._selected_keys()),
            self.orientation.currentData(), self.split.currentData(), self.group_limit.value(),
            fallbacks={key: value for key, value in self._fallback_values().items() if key in self._selected_keys()},
            column_labels={column.key: column.label for column in self._data.columns})

    def selection(self) -> DocumentMealSelection:
        options = self.options()
        self._data.selected_rows(options)
        known = {column.key for column in self._data.columns}
        if any(value not in known for value in options.fallbacks.values()):
            raise ValueError("저장된 대체 칼럼이 현재 원본에 없습니다. 다시 선택해주세요.")
        return replace(self._document, substitute_options=options)

    def _refresh(self, *_args):
        for index in range(self.columns.topLevelItemCount()):
            item = self.columns.topLevelItem(index)
            self._fallback_combos[item.data(0, Qt.ItemDataRole.UserRole)].setEnabled(
                item.checkState(0) == Qt.CheckState.Checked)
        self.group_limit.setEnabled(self.split.currentData() != "none")
        try:
            self.selection()
        except ValueError as error:
            self.error.setText(str(error))
            self.accept_button.setEnabled(False)
        else:
            self.error.clear()
            self.accept_button.setEnabled(True)
