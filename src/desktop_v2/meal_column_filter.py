"""Independent driver and delivery-state choices for one meal column."""
import json

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QPushButton

from .column_search import ColumnValuePicker

_PREFIX = "meal-filter:v1:"
_STATUSES = ("-", "배송", "일시중지")


def meal_filter_values(drivers=None, statuses=None):
    if drivers is None and statuses is None:
        return None
    payload = {"drivers": None if drivers is None else sorted(drivers),
               "statuses": None if statuses is None else sorted(statuses)}
    return frozenset({_PREFIX + json.dumps(payload, ensure_ascii=False)})


def decode_meal_filter(selected):
    if selected is None or len(selected) != 1:
        return None
    token = next(iter(selected))
    if not token.startswith(_PREFIX):
        return None
    try:
        value = json.loads(token[len(_PREFIX):])
    except (ValueError, TypeError):
        return None
    if not isinstance(value, dict) or set(value) != {"drivers", "statuses"}:
        return None
    if any(items is not None and (not isinstance(items, list) or any(not isinstance(item, str) for item in items))
           for items in value.values()):
        return None
    return {key: None if items is None else frozenset(items) for key, items in value.items()}


class MealColumnValuePicker(ColumnValuePicker):
    def __init__(self, values, selected, search_text="", parent=None):
        QWidget.__init__(self, parent)
        self.setFixedWidth(340)
        self._initial_selected = selected
        self._edited = False
        parsed = decode_meal_filter(selected)
        pairs = [value.rsplit("\n", 1) for value in values if "\n" in value]
        legacy = [value.rsplit("\n", 1) for value in selected or () if "\n" in value]
        if parsed is None:
            parsed = {"drivers": None if selected is None else frozenset(pair[0] for pair in legacy),
                      "statuses": None if selected is None else frozenset(pair[1] for pair in legacy)}
        drivers = sorted({pair[0] for pair in (*pairs, *legacy)} | set(parsed["drivers"] or ()), key=str.casefold)
        root = QVBoxLayout(self)
        self.search = QLineEdit(search_text)
        self.search.setPlaceholderText("이 식종에서 검색")
        self.search.setClearButtonEnabled(True)
        root.addWidget(self.search)
        groups = QHBoxLayout()
        groups.setSpacing(8)
        self.driver_list = self._group(groups, "배송기사", drivers, parsed["drivers"])
        self.status_list = self._group(groups, "배송상태", _STATUSES, parsed["statuses"])
        root.addLayout(groups)
        hint = QLabel("기사와 상태 조건을 모두 만족하는 행을 표시합니다. 각 목록에서 여러 개를 선택할 수 있습니다.")
        hint.setWordWrap(True)
        root.addWidget(hint)
        controls = QHBoxLayout()
        self.apply, self.cancel = (QPushButton(text) for text in ("적용", "취소"))
        self.apply.setObjectName("primaryAction")
        self.cancel.setObjectName("quietAction")
        for button in (self.apply, self.cancel):
            controls.addWidget(button)
        root.addLayout(controls)
        self.apply.clicked.connect(self._apply)

    def _group(self, layout, title, values, selected):
        group = QVBoxLayout()
        group.addWidget(QLabel(title))
        box = QListWidget()
        box.setAccessibleName(title + " 선택")
        box.setMinimumSize(110, 180)
        box.all_item = QListWidgetItem("(모두 선택)")
        box.all_item.setFlags(box.all_item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        box.addItem(box.all_item)
        for value in values:
            item = QListWidgetItem(value)
            item.setData(Qt.ItemDataRole.UserRole, value)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if selected is None or value in selected else Qt.CheckState.Unchecked)
            box.addItem(item)
        box.itemChanged.connect(lambda item: self._group_changed(box, item))
        self._sync_all(box)
        group.addWidget(box)
        layout.addLayout(group)
        return box

    def _group_changed(self, box, item):
        self._edited = True
        if item is box.all_item:
            self._check_group(box, Qt.CheckState.Checked if item.checkState() != Qt.CheckState.Unchecked else Qt.CheckState.Unchecked)
            return
        self._sync_all(box)

    @staticmethod
    def _sync_all(box):
        count = sum(box.item(index).checkState() == Qt.CheckState.Checked for index in range(1, box.count()))
        state = (Qt.CheckState.Unchecked if count == 0 else
                 Qt.CheckState.Checked if count == box.count() - 1 else Qt.CheckState.PartiallyChecked)
        box.blockSignals(True)
        box.all_item.setCheckState(state)
        box.blockSignals(False)

    def _check_group(self, box, state):
        self._edited = True
        box.blockSignals(True)
        for index in range(1, box.count()):
            box.item(index).setCheckState(state)
        box.blockSignals(False)
        self._sync_all(box)

    @staticmethod
    def _selected(box):
        selected = frozenset(box.item(index).data(Qt.ItemDataRole.UserRole)
                             for index in range(1, box.count())
                             if box.item(index).checkState() == Qt.CheckState.Checked)
        return None if len(selected) == box.count() - 1 else selected

    def _apply(self):
        # Preserve older paired selections exactly until a checkbox is changed.
        values = (meal_filter_values(self._selected(self.driver_list), self._selected(self.status_list))
                  if self._edited else self._initial_selected)
        self.applied.emit(self.search.text(), values)
