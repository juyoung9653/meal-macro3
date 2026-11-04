"""Column filter buttons embedded in table headers."""

import json

from PySide6.QtCore import QPoint, QSize, Qt, Signal
from PySide6.QtGui import QPolygon
from PySide6.QtWidgets import (
    QHBoxLayout,
    QCheckBox,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QStyle,
    QStyleOptionHeader,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)

from src.domain.roster import Meal


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
        self.list = self._make_value_list(selected)
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

    def _make_value_list(self, selected):
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
        return self.list

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


_SEQUENCE_PREFIX = "sequence-pair:v1:"
_SEQUENCE_PRESENT_PREFIX = "sequence-present:v1:"
_SEQUENCE_EXCLUSIVE_PREFIX = "sequence-exclusive:v1:"
_SEQUENCE_MEALS = (Meal.DINNER, Meal.BREAKFAST, Meal.LUNCH)
_SEQUENCE_PRESENT_MEALS = frozenset(meal.value for meal in Meal)
ALL_SEQUENCES_EMPTY = "sequence-aggregate:v1:all-empty"


def sequence_pair_token(meal_label, sequence):
    return _SEQUENCE_PREFIX + json.dumps([meal_label, sequence], ensure_ascii=False)


def sequence_pair_value(token):
    if not token.startswith(_SEQUENCE_PREFIX):
        return None
    try:
        value = json.loads(token[len(_SEQUENCE_PREFIX):])
    except ValueError:
        return None
    return tuple(value) if isinstance(value, list) and len(value) == 2 and all(isinstance(x, str) for x in value) else None


def sequence_present_token(meal_label: str) -> str:
    """Return the semantic token for a supported meal having any sequence."""
    if meal_label not in _SEQUENCE_PRESENT_MEALS:
        raise ValueError(f"지원하지 않는 식종입니다: {meal_label}")
    return f"{_SEQUENCE_PRESENT_PREFIX}{meal_label}"


def sequence_present_meal(token: str) -> str | None:
    """Decode a valid sequence-present token without accepting literal values."""
    if not token.startswith(_SEQUENCE_PRESENT_PREFIX):
        return None
    meal_label = token[len(_SEQUENCE_PRESENT_PREFIX):]
    return meal_label if meal_label in _SEQUENCE_PRESENT_MEALS else None


def sequence_exclusive_token(meal_label: str) -> str:
    """Match a sequence on this meal with no sequence on any other meal."""
    if meal_label not in _SEQUENCE_PRESENT_MEALS:
        raise ValueError(f"지원하지 않는 식종입니다: {meal_label}")
    return f"{_SEQUENCE_EXCLUSIVE_PREFIX}{meal_label}"


def sequence_exclusive_meal(token: str) -> str | None:
    if not token.startswith(_SEQUENCE_EXCLUSIVE_PREFIX):
        return None
    meal_label = token[len(_SEQUENCE_EXCLUSIVE_PREFIX):]
    return meal_label if meal_label in _SEQUENCE_PRESENT_MEALS else None


class SequenceValuePicker(ColumnValuePicker):
    """Select alternatives within meals; meal groups combine with AND."""

    def __init__(self, values, selected, search_text="", parent=None):
        QWidget.__init__(self, parent)
        self._initial_selected = None if selected is None else frozenset(selected)
        self._selection_edited = False
        self._updating_tree = False
        blanks = tuple(sequence_pair_token(meal.value, "") for meal in _SEQUENCE_MEALS)
        self._values = tuple(dict.fromkeys((*values, *sorted(selected or ()), *blanks)))

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
        self.list = self._make_value_list(self._initial_selected)
        layout.addWidget(self.list)
        hint = QLabel("식종별 조건을 모두 만족해야 합니다. 선택하지 않은 식종은 순서 없음입니다.")
        hint.setWordWrap(True)
        layout.addWidget(hint)

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

        self._search_values(search_text)
        self.search.textChanged.connect(self._search_values)
        self.list.itemChanged.connect(self._item_changed)
        self.select_all.clicked.connect(lambda: self._check_all(Qt.CheckState.Checked))
        self.select_none.clicked.connect(lambda: self._check_all(Qt.CheckState.Unchecked))
        self.apply.clicked.connect(self._apply)
        self.reset.clicked.connect(lambda: self.applied.emit("", None))

    def preferred_focus_widget(self):
        return self.search

    @staticmethod
    def _implied_blank_meals(selected):
        if selected is None:
            return frozenset()
        if not selected:
            return frozenset()
        active = set()
        force_blank = set()
        structured = False
        for token in selected:
            pair = sequence_pair_value(token)
            if pair is not None:
                structured = True
                if pair[0] in _SEQUENCE_PRESENT_MEALS:
                    active.add(pair[0])
            elif meal := sequence_present_meal(token):
                structured = True
                active.add(meal)
            elif meal := sequence_exclusive_meal(token):
                structured = True
                active.add(meal)
                force_blank.update(item.value for item in _SEQUENCE_MEALS if item.value != meal)
            elif token == ALL_SEQUENCES_EMPTY:
                structured = True
                force_blank.update(item.value for item in _SEQUENCE_MEALS)
        if not structured:
            return frozenset()
        return frozenset(force_blank | (_SEQUENCE_PRESENT_MEALS - active))

    def _make_value_list(self, selected):
        tree = QTreeWidget()
        tree.setHeaderHidden(True)
        tree.setMinimumSize(300, 300)
        tree.setAccessibleName("식종별 배송순서 선택")
        self._leaves = []
        self._meal_parents = {}
        groups = {meal.value: [] for meal in _SEQUENCE_MEALS}
        extra_groups = {}
        compatibility = []
        implied_blanks = self._implied_blank_meals(selected)
        for token in self._values:
            pair = sequence_pair_value(token)
            if pair is not None and pair[0] in groups:
                groups[pair[0]].append((pair[1], token))
            elif pair is not None:
                extra_groups.setdefault(pair[0], []).append((pair[1], token))
            elif selected is not None and token in selected:
                compatibility.append((self._compatibility_label(token), token))

        for meal in _SEQUENCE_MEALS:
            label = meal.value
            parent = QTreeWidgetItem(tree, [label])
            self._meal_parents[label] = parent
            values = groups[label]
            for sequence, token in sorted(
                values,
                key=lambda item: (
                    (0, int(item[0]), item[0]) if item[0].isdigit()
                    else (1, 0, item[0].casefold())
                ),
            ):
                item = QTreeWidgetItem(parent, [sequence.replace("\n", " · ") or "(빈 값)"])
                item.setData(0, Qt.ItemDataRole.UserRole, token)
                tooltip = (f"{label} 배송순서가 '{sequence}'와 정확히 일치하는 사람"
                           if sequence else f"{label} 배송순서가 비어 있는 사람")
                item.setToolTip(0, tooltip)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                checked = selected is None or (selected is not None and token in selected)
                if not sequence and label in implied_blanks:
                    checked = True
                item.setCheckState(0, Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
                self._leaves.append(item)
            if any(sequence for sequence, _token in values):
                parent.setFlags(parent.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            self._sync_parent_state(parent)
            parent.setExpanded(True)

        for label in sorted(extra_groups, key=str.casefold):
            parent = QTreeWidgetItem(tree, [label])
            for sequence, token in sorted(extra_groups[label], key=lambda item: item[0].casefold()):
                item = QTreeWidgetItem(parent, [sequence.replace("\n", " · ") or "(빈 값)"])
                item.setData(0, Qt.ItemDataRole.UserRole, token)
                item.setToolTip(0, f"{label}: {sequence}" if sequence else label)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                checked = selected is None or (selected is not None and token in selected)
                item.setCheckState(0, Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
                self._leaves.append(item)
            parent.setExpanded(True)

        if compatibility:
            parent = QTreeWidgetItem(tree, ["기존 저장 조건"])
            for label, token in sorted(compatibility, key=lambda item: item[0].casefold()):
                item = QTreeWidgetItem(parent, [label])
                item.setData(0, Qt.ItemDataRole.UserRole, token)
                item.setToolTip(0, "이전 버전에서 저장한 조건입니다. 체크를 해제해 제거할 수 있습니다.")
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(0, Qt.CheckState.Checked)
                self._leaves.append(item)
            parent.setExpanded(True)
        return tree

    @staticmethod
    def _compatibility_label(token):
        if meal := sequence_present_meal(token):
            return f"{meal}: 순서 있음"
        if meal := sequence_exclusive_meal(token):
            return f"{meal}: 다른 식종 순서 없음"
        if token == ALL_SEQUENCES_EMPTY:
            return "전체 식종 순서 없음"
        if pair := sequence_pair_value(token):
            return f"{pair[0]}: {pair[1] or '(빈 값)'}"
        return f"이전 행 전체 일치: {token.replace(chr(10), ' · ') or '(빈 값)'}"

    def _search_values(self, text):
        query = " ".join(text.split()).casefold()
        for item in self._leaves:
            token = item.data(0, Qt.ItemDataRole.UserRole)
            pair = sequence_pair_value(token)
            value = f"{pair[0]}: {pair[1]}" if pair else item.text(0)
            item.setHidden(query not in " ".join(value.split()).casefold())
        for index in range(self.list.topLevelItemCount()):
            parent = self.list.topLevelItem(index)
            if parent.childCount():
                parent.setHidden(all(parent.child(i).isHidden() for i in range(parent.childCount())))

    def _item_changed(self, item, _column):
        if self._updating_tree:
            return
        self._selection_edited = True
        if item in self._meal_parents.values():
            state = item.checkState(0)
            self._updating_tree = True
            try:
                for index in range(item.childCount()):
                    child = item.child(index)
                    pair = sequence_pair_value(child.data(0, Qt.ItemDataRole.UserRole))
                    if pair is not None and pair[1]:
                        child.setCheckState(0, state)
            finally:
                self._updating_tree = False
            self._sync_parent_state(item)
            return
        parent = item.parent()
        if parent in self._meal_parents.values():
            self._sync_parent_state(parent)

    def _sync_parent_state(self, parent):
        nonempty = []
        for index in range(parent.childCount()):
            child = parent.child(index)
            pair = sequence_pair_value(child.data(0, Qt.ItemDataRole.UserRole))
            if pair is not None and pair[1]:
                nonempty.append(child)
        checked = sum(item.checkState(0) == Qt.CheckState.Checked for item in nonempty)
        state = (Qt.CheckState.Checked if nonempty and checked == len(nonempty)
                 else Qt.CheckState.PartiallyChecked if checked
                 else Qt.CheckState.Unchecked)
        self._updating_tree = True
        try:
            parent.setCheckState(0, state)
        finally:
            self._updating_tree = False

    def _set_all_items(self, state):
        self._updating_tree = True
        try:
            for item in self._leaves:
                item.setCheckState(0, state)
        finally:
            self._updating_tree = False

    def _check_all(self, state):
        self._set_all_items(state)
        for parent in self._meal_parents.values():
            self._sync_parent_state(parent)
        self._selection_edited = True

    def _apply(self):
        if not self._selection_edited:
            self.applied.emit(self.search.text(), self._initial_selected)
            return
        selected = frozenset(
            item.data(0, Qt.ItemDataRole.UserRole)
            for item in self._leaves
            if item.checkState(0) == Qt.CheckState.Checked
        )
        self.applied.emit(self.search.text(), selected)


class ColumnSearchHeader(QHeaderView):
    search_changed = Signal(str, str)
    values_changed = Signal(str, object)
    exceptions_changed = Signal(str, bool)
    BUTTON_WIDTH = 24

    def __init__(self, parent=None, *, values_provider=None, selection_provider=None,
                 search_provider=None, picker_provider=None, exception_provider=None):
        super().__init__(Qt.Orientation.Horizontal, parent)
        self.setSectionsClickable(True)
        self._values_provider = values_provider
        self._selection_provider = selection_provider
        self._search_provider = search_provider
        self._picker_provider = picker_provider
        self._exception_provider = exception_provider
        self.buttons: dict[str, QPushButton] = {}
        self._menu = None
        self._keys: list[str] = []
        self._sort_criteria: tuple[tuple[str, Qt.SortOrder], ...] = ()
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
        self.set_sort_criteria(self._sort_criteria)
        self.viewport().update()

    def set_sort_criteria(self, criteria):
        self._sort_criteria = tuple(criteria)
        if self._sort_criteria and self._sort_criteria[0][0] in self._keys:
            key, order = self._sort_criteria[0]
            self.setSortIndicator(self._keys.index(key), order)
        else:
            self.setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
        self.viewport().update()

    def sectionSizeFromContents(self, logical_index):
        size = super().sectionSizeFromContents(logical_index)
        if logical_index >= len(self._keys):
            return size
        key = self._keys[logical_index]
        reserved_width = self.BUTTON_WIDTH + 2
        active = next(
            ((priority, order) for priority, (sort_key, order)
             in enumerate(self._sort_criteria, 1) if sort_key == key),
            None,
        )
        if active is not None:
            priority, order = active
            arrow = "↑" if order == Qt.SortOrder.AscendingOrder else "↓"
            indicator_text = f"{arrow}{priority}"
            reserved_width += self.fontMetrics().horizontalAdvance(indicator_text) + 6
        size.setWidth(size.width() + reserved_width)
        return size

    def _update_button(self, key):
        active = bool(self._search_provider and self._search_provider(key)) or (
            self._selection_provider is not None
            and self._selection_provider(key) is not None
        )
        active = active or bool(self._exception_provider and self._exception_provider(key))
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
        picker_type = self._picker_provider(key) if self._picker_provider else ColumnValuePicker
        picker = picker_type(
            self._values_provider(key),
            self._selection_provider(key) if self._selection_provider else None,
            self._search_provider(key) if self._search_provider else "",
            menu,
        )
        exception_state = self._exception_provider(key) if self._exception_provider else None
        exceptions = None
        if exception_state is not None:
            exceptions = QCheckBox("예외만 보기", picker)
            exceptions.setAccessibleName("공용 예외만 보기")
            exceptions.setToolTip("이 칼럼에서 공용 예외로 지정한 행만 표시합니다.")
            exceptions.setChecked(exception_state)
            picker.layout().insertWidget(1, exceptions)
            # Reset must clear this condition along with search/value conditions.
            if hasattr(picker, "reset"):
                picker.reset.clicked.disconnect()
                picker.reset.clicked.connect(lambda: self._apply_filter(key, "", None, menu, False))
        action = QWidgetAction(menu)
        action.setDefaultWidget(picker)
        menu.addAction(action)
        picker.applied.connect(
            lambda search, values: self._apply_filter(
                key, search, values, menu, exceptions.isChecked() if exceptions is not None else None)
        )
        if hasattr(picker, "layout_changed"):
            picker.layout_changed.connect(
                lambda: self._resize_filter_menu(menu, self.buttons[key], picker, action)
            )
        picker.cancel.clicked.connect(menu.close)
        button = self.buttons[key]
        menu.popup(button.mapToGlobal(button.rect().bottomLeft()))
        menu.activateWindow()
        self._resize_filter_menu(menu, button, picker, action)
        focus = picker.preferred_focus_widget() if hasattr(picker, "preferred_focus_widget") else picker.search
        focus.setFocus()

    @staticmethod
    def _resize_filter_menu(menu, button, picker, action):
        action.changed.emit()
        picker.ensurePolished()
        if picker.layout() is not None:
            picker.layout().invalidate()
            picker.layout().activate()
        picker.updateGeometry()
        menu.ensurePolished()
        if menu.layout() is not None:
            menu.layout().invalidate()
            menu.layout().activate()
        menu.updateGeometry()
        menu.adjustSize()
        screen = button.screen()
        if screen is None:
            return
        available = screen.availableGeometry()
        # QWidget's layout hint can stay wider than its fixed width. Size the
        # surrounding menu from the actual constraints, including wrapped text.
        widget_hint = picker.sizeHint().boundedTo(picker.maximumSize()).expandedTo(picker.minimumSize())
        if picker.hasHeightForWidth():
            height = picker.heightForWidth(widget_hint.width())
            if height >= 0:
                widget_hint.setHeight(max(picker.minimumHeight(), min(height, picker.maximumHeight())))
        margins = menu.contentsMargins()
        panel = menu.style().pixelMetric(QStyle.PixelMetric.PM_MenuPanelWidth, None, menu)
        horizontal = menu.style().pixelMetric(QStyle.PixelMetric.PM_MenuHMargin, None, menu)
        vertical = menu.style().pixelMetric(QStyle.PixelMetric.PM_MenuVMargin, None, menu)
        hint = QSize(
            widget_hint.width() + margins.left() + margins.right() + (panel + horizontal) * 2,
            widget_hint.height() + margins.top() + margins.bottom() + (panel + vertical) * 2,
        )
        size = QSize(min(hint.width(), available.width()), min(hint.height(), available.height()))
        menu.resize(size)
        anchor = button.mapToGlobal(button.rect().bottomLeft())
        x = max(available.left(), min(anchor.x(), available.right() - size.width() + 1))
        below = anchor.y()
        above = button.mapToGlobal(button.rect().topLeft()).y() - size.height()
        y = below if below + size.height() <= available.bottom() + 1 else max(available.top(), above)
        menu.move(x, y)

    def _apply_filter(self, key, search, values, menu, exceptions=None):
        self.search_changed.emit(key, search)
        self.values_changed.emit(key, values)
        if exceptions is not None:
            self.exceptions_changed.emit(key, exceptions)
        self._update_button(key)
        menu.close()

    def paintSection(self, painter, rect, logical_index):
        key = self._keys[logical_index] if logical_index < len(self._keys) else None
        active = next(
            ((priority, order) for priority, (sort_key, order)
             in enumerate(self._sort_criteria, 1) if sort_key == key),
            None,
        )
        indicator_text = ""
        indicator_width = 0
        if active is not None:
            priority, order = active
            arrow = "↑" if order == Qt.SortOrder.AscendingOrder else "↓"
            indicator_text = f"{arrow}{priority}"
            indicator_width = self.fontMetrics().horizontalAdvance(indicator_text) + 6
        if self._sort_criteria:
            option = QStyleOptionHeader()
            self.initStyleOptionForIndex(option, logical_index)
            option.rect = rect
            option.sortIndicator = QStyleOptionHeader.SortIndicator.None_
            self.style().drawControl(QStyle.ControlElement.CE_HeaderSection, option, painter, self)
            option.rect = rect.adjusted(0, 0, -self.BUTTON_WIDTH - indicator_width - 2, 0)
            margin = self.style().pixelMetric(QStyle.PixelMetric.PM_HeaderMargin, option, self)
            if option.fontMetrics.horizontalAdvance(option.text) + 2 * margin > option.rect.width():
                option.textAlignment = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
            painter.save()
            painter.setClipRect(option.rect)
            self.style().drawControl(QStyle.ControlElement.CE_HeaderLabel, option, painter, self)
            painter.restore()
        else:
            painter.save()
            painter.setClipRect(rect.adjusted(0, 0, -self.BUTTON_WIDTH, 0))
            super().paintSection(painter, rect, logical_index)
            painter.restore()
        if active is not None:
            indicator_rect = rect.adjusted(0, 0, -self.BUTTON_WIDTH - 2, 0)
            indicator_rect.setLeft(indicator_rect.right() - indicator_width + 1)
            painter.save()
            painter.setPen(self.palette().text().color())
            painter.drawText(indicator_rect, Qt.AlignmentFlag.AlignCenter, indicator_text)
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
