"""Household manifest with scoped sequence editing for one service day."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING
from uuid import UUID

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemDelegate,
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QStyledItemDelegate,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from src.domain.delivery_routing import Driver

from .column_search import ColumnSearchHeader
from .theme import application_stylesheet, register_application_fonts

if TYPE_CHECKING:
    from src.services.driver_households import DriverHouseholdRow


_COLUMNS = ("배송기사", "가구원", "식종", "배송순서", "주소", "연락처")
_COLUMN_KEYS = ("driver", "members", "meals", "sequence", "address", "contacts")


@dataclass(frozen=True, slots=True)
class _Column:
    key: str
    label: str


class _SortableItem(QTableWidgetItem):
    def __init__(self, text, sort_key):
        super().__init__(text)
        self._sort_key = sort_key

    def __lt__(self, other):
        if isinstance(other, _SortableItem):
            return self._sort_key < other._sort_key
        return super().__lt__(other)


def _sequence_key(values):
    return tuple(
        (0, int(value.strip()), value.strip()) if value.strip().isdigit()
        else (1, 0, value.strip().casefold())
        for value in values if value.strip()
    ) or ((2, 0, ""),)


def _sequence_targets(row):
    # Merged rows require exact meal projections; the cartesian product of
    # household members and meals could update meals absent from this manifest.
    meal_rows = row.meal_rows or ((row,) if len(row.meals) == 1 else ())
    return tuple(dict.fromkeys(
        (person_id, meal_row.meals[0])
        for meal_row in meal_rows if len(meal_row.meals) == 1
        for person_id in meal_row.member_ids
    ))


class _SequenceDelegate(QStyledItemDelegate):
    save_requested = Signal(object, str)
    editing_started = Signal(int)
    editing_cancelled = Signal()

    def createEditor(self, parent, option, index):
        row = index.data(Qt.ItemDataRole.UserRole)
        targets = _sequence_targets(row)
        if not targets:
            return None
        editor = QLineEdit(parent)
        editor.setObjectName("householdSequenceEditor")
        editor.setAccessibleName("가구 배송순서")
        # Capture the immutable projection and exact targets before any sorting
        # or filtering can change the QModelIndex's row number.
        editor._sequence_row = row
        editor._sequence_targets = targets
        editor._sequence_finished = False
        editor.setPlaceholderText("가구에 적용할 순서")
        self.editing_started.emit(len(targets))
        return editor

    def setEditorData(self, editor, index):
        values = editor._sequence_row.sequences
        editor.setText(values[0] if len(values) == 1 and not editor._sequence_row.has_empty_sequence else "")
        editor.selectAll()

    def setModelData(self, editor, model, index):
        # Persistence owns the display. Never write uncommitted editor text into
        # the table model, including default delegate focus/navigation paths.
        pass

    def eventFilter(self, editor, event):
        if not isinstance(editor, QLineEdit):
            return super().eventFilter(editor, event)
        if event.type() == QEvent.Type.KeyPress:
            key = event.key()
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                targets, value = editor._sequence_targets, editor.text()
                editor._sequence_finished = True
                self.closeEditor.emit(editor, QAbstractItemDelegate.EndEditHint.RevertModelCache)
                self.save_requested.emit(targets, value)
                return True
            if key in (Qt.Key.Key_Escape, Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
                self._cancel(editor)
                return True
        if event.type() == QEvent.Type.FocusOut:
            self._cancel(editor)
            return True
        return super().eventFilter(editor, event)

    def _cancel(self, editor):
        if not editor._sequence_finished:
            editor._sequence_finished = True
            self.closeEditor.emit(editor, QAbstractItemDelegate.EndEditHint.RevertModelCache)
            self.editing_cancelled.emit()


class DriverHouseholdsDialog(QDialog):
    """Show service-day household rows supplied by the projection service.

    This dialog intentionally does not build or combine projection rows.  A row is
    already one household/driver manifest entry when it arrives here.
    """

    sequence_save_requested = Signal(object, str)

    def __init__(
        self,
        rows: tuple[DriverHouseholdRow, ...],
        *,
        service_day: date,
        drivers: tuple[Driver, ...],
        selected_driver_id: UUID | None = None,
        rows_provider: Callable[[bool], tuple[DriverHouseholdRow, ...]] | None = None,
        parent=None,
    ):
        super().__init__(parent)
        register_application_fonts()
        self.setObjectName("driverHouseholdsDialog")
        self.setWindowTitle("기사 담당가구")
        self.setStyleSheet(application_stylesheet())
        self.resize(1000, 600)

        self._rows = tuple(rows)
        self._rows_provider = rows_provider
        self._split_by_meal = False
        self._drivers = tuple(drivers)
        self._selected_driver_id = selected_driver_id if isinstance(selected_driver_id, UUID) else None
        self._column_searches: dict[str, str] = {}
        self._column_values: dict[str, frozenset[str]] = {}
        layout = QVBoxLayout(self)

        title = QLabel("기사 담당가구")
        title.setObjectName("workspaceTitle")
        layout.addWidget(title)
        self.day_label = QLabel(f"선택일: {service_day:%Y-%m-%d}")
        self.day_label.setObjectName("fieldLabel")
        layout.addWidget(self.day_label)
        self.scope_label = QLabel("가입·배정된 모든 식종 · 일시중지·중지 포함")
        self.scope_label.setObjectName("driverHouseholdsScope")
        layout.addWidget(self.scope_label)
        guide = QLabel("배송순서를 더블클릭해 입력하면 이 행의 가구원 식종에 함께 적용됩니다.")
        guide.setWordWrap(True)
        guide.setObjectName("workspaceSubtitle")
        layout.addWidget(guide)

        filters = QHBoxLayout()
        filters.addStretch(1)
        self.include_hidden_checkbox = QCheckBox("숨긴 명단 포함")
        self.include_hidden_checkbox.setObjectName("includeHiddenHouseholds")
        self.include_hidden_checkbox.setToolTip("장기 미배송으로 숨긴 대상자를 기사별 가구에 포함")
        self.include_hidden_checkbox.setEnabled(rows_provider is not None)
        self.include_hidden_checkbox.toggled.connect(self._set_include_hidden)
        filters.addWidget(self.include_hidden_checkbox)
        self.split_button = QPushButton("식종별 분리")
        self.split_button.setObjectName("quietAction")
        self.split_button.setToolTip("가구 행을 식종별 배송 행으로 표시")
        self.split_button.clicked.connect(self._toggle_meal_rows)
        filters.addWidget(self.split_button)
        layout.addLayout(filters)

        self.table = QTableWidget(0, len(_COLUMNS))
        self.table.setObjectName("driverHouseholdsTable")
        self._column_search_header = ColumnSearchHeader(
            self.table,
            values_provider=self.column_filter_values,
            selection_provider=self._column_values.get,
            search_provider=lambda key: self._column_searches.get(key, ""),
        )
        self.table.setHorizontalHeader(self._column_search_header)
        self.table.setHorizontalHeaderLabels(_COLUMNS)
        self._column_search_header.replace_columns(tuple(
            _Column(key, label) for key, label in zip(_COLUMN_KEYS, _COLUMNS, strict=True)
        ))
        self._column_search_header.search_changed.connect(self._set_column_search)
        self._column_search_header.values_changed.connect(self._set_column_values)
        self.table.horizontalScrollBar().valueChanged.connect(
            self._column_search_header.update_positions
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.table.setWordWrap(True)
        self.table.verticalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked)
        self._sequence_delegate = _SequenceDelegate(self.table)
        self.table.setItemDelegateForColumn(3, self._sequence_delegate)
        self._sequence_delegate.save_requested.connect(self.sequence_save_requested.emit)
        self._sequence_delegate.editing_started.connect(
            lambda _count: self.sequence_status.setText("이 행의 가구원 식종에 같은 순서 적용 · Enter 저장 · Esc 또는 다른 곳 클릭 취소")
        )
        self._sequence_delegate.editing_cancelled.connect(
            lambda: self.sequence_status.setText("수정을 취소했습니다. · 더블클릭하여 수정 · Enter 저장 · Esc 취소")
        )
        self.table.setAlternatingRowColors(True)
        header = self.table.horizontalHeader()
        header.setToolTip("열 제목 클릭: 정렬 · ▾: 칼럼별 검색과 값 필터")
        header.setMinimumSectionSize(110)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for column, width in enumerate((140, 165, 125, 115, 240, 180)):
            header.resizeSection(column, width)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        # Preserve the service's initial order until a column title is clicked.
        header.setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
        self.table.setSortingEnabled(True)
        layout.addWidget(self.table, 1)

        self.sequence_status = QLabel("더블클릭하여 가구 배송순서 수정 · Enter 저장 · Esc 또는 다른 곳 클릭 취소")
        self.sequence_status.setObjectName("householdSequenceStatus")
        self.sequence_status.setWordWrap(True)
        layout.addWidget(self.sequence_status)

        self.count = QLabel()
        self.count.setObjectName("driverHouseholdsCount")
        layout.addWidget(self.count)
        self.empty = QLabel()
        self.empty.setObjectName("driverHouseholdsEmpty")
        self.empty.setWordWrap(True)
        layout.addWidget(self.empty)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("닫기")
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

        self._refresh()

    @property
    def include_hidden(self) -> bool:
        return self.include_hidden_checkbox.isChecked()

    def replace_rows(self, rows):
        self._rows = tuple(rows)
        self._refresh()

    def show_sequence_result(self, result):
        from .sequence_edit import SequenceSaveStatus

        problem = result.status is SequenceSaveStatus.FAILED or result.backup_warning
        self.sequence_status.setStyleSheet("color: #B42318;" if problem else "")
        self.sequence_status.setText(result.message)

    def _set_include_hidden(self, include_hidden: bool) -> None:
        if self._rows_provider is None:
            return
        editor = self.table.findChild(QLineEdit, "householdSequenceEditor")
        if editor is not None:
            self._sequence_delegate._cancel(editor)
        self.replace_rows(self._rows_provider(include_hidden))

    def _visible_rows(self) -> tuple[DriverHouseholdRow, ...]:
        visible = []
        for row in self._display_rows():
            if self._selected_driver_id is not None and row.driver_id != self._selected_driver_id:
                continue
            values = dict(zip(_COLUMN_KEYS, self._display_values(row), strict=True))
            if any(
                text not in " ".join(values[key].split()).casefold()
                for key, text in self._column_searches.items()
            ):
                continue
            if any(values[key] not in selected for key, selected in self._column_values.items()):
                continue
            visible.append(row)
        return tuple(visible)

    def _display_rows(self) -> tuple[DriverHouseholdRow, ...]:
        if not self._split_by_meal:
            return self._rows
        return tuple(
            meal_row
            for row in self._rows
            for meal_row in (row.meal_rows or (row,))
        )

    def _toggle_meal_rows(self) -> None:
        self._split_by_meal = not self._split_by_meal
        self.split_button.setText("분리 해제" if self._split_by_meal else "식종별 분리")
        self.split_button.setToolTip(
            "가구별로 합쳐 표시" if self._split_by_meal else "가구 행을 식종별 배송 행으로 표시"
        )
        # Exact-value filters refer to the displayed projection. Drop only
        # values that no longer occur after switching between merged and meal rows.
        for key, selected in tuple(self._column_values.items()):
            if not selected:
                continue  # An explicit "no values" selection stays empty.
            available = set(self.column_filter_values(key))
            remaining = selected & available
            if remaining:
                self._column_values[key] = frozenset(remaining)
            else:
                self._column_values.pop(key)
        self._refresh()
        self._column_search_header.refresh_buttons()

    @staticmethod
    def _display_values(row):
        return (
            row.driver_name or "미지정",
            ", ".join(row.member_names),
            ", ".join(meal.value for meal in row.meals),
            ", ".join(row.sequences),
            row.address,
            ", ".join(row.contacts),
        )

    def column_filter_values(self, key):
        column = _COLUMN_KEYS.index(key)
        return tuple(sorted({self._display_values(row)[column] for row in self._display_rows()
                             if self._selected_driver_id is None or row.driver_id == self._selected_driver_id}, key=str.casefold))

    def _set_column_search(self, key, text):
        normalized = " ".join(text.split()).casefold()
        if self._column_searches.get(key, "") == normalized:
            return
        if normalized:
            self._column_searches[key] = normalized
        else:
            self._column_searches.pop(key, None)
        self._refresh()

    def _set_column_values(self, key, values):
        values = None if values is None else frozenset(values)
        if self._column_values.get(key) == values:
            return
        if values is None:
            self._column_values.pop(key, None)
        else:
            self._column_values[key] = frozenset(values)
        self._refresh()

    def _refresh(self) -> None:
        rows = self._visible_rows()
        current = self.table.currentItem()
        selected_identity = (
            self.table.item(current.row(), 0).data(Qt.ItemDataRole.UserRole)
            if current is not None and self.table.item(current.row(), 0) is not None
            else None
        )
        # Inserting into a sorted QTableWidget can move a half-populated row.
        self.table.setSortingEnabled(False)
        # Replacing populated cells otherwise recalculates wrapped row heights
        # after every setItem, which can block filter Apply for large manifests.
        row_header = self.table.verticalHeader()
        row_header.setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        try:
            self.table.setRowCount(len(rows))
            for index, row in enumerate(rows):
                values = self._display_values(row)
                for column, value in enumerate(values):
                    sort_key = _sequence_key(row.sequences) if column == 3 else (value.casefold(),)
                    item = _SortableItem(value, sort_key)
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                    item.setToolTip(value)
                    if column == 3:
                        item.setData(Qt.ItemDataRole.UserRole, row)
                        if _sequence_targets(row):
                            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
                        item.setToolTip(f"{value or '순서 미지정'}\n더블클릭하여 가구에 같은 순서 적용 · Enter 저장 · Esc 취소")
                    if column == 0:
                        item.setData(Qt.ItemDataRole.UserRole, (
                            row.household_key, row.driver_id, row.driver_name, row.address,
                        ))
                    self.table.setItem(index, column, item)
            self.table.setSortingEnabled(True)
        finally:
            row_header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        if selected_identity is not None:
            for index in range(self.table.rowCount()):
                if self.table.item(index, 0).data(Qt.ItemDataRole.UserRole) == selected_identity:
                    self.table.selectRow(index)
                    break
        households = len({row.household_key for row in rows})
        self.count.setText(f"가구 {households}건 · 표시 {len(rows)}행")
        if rows:
            self.empty.setText("")
        elif self._rows:
            self.empty.setText("현재 기사와 칼럼 필터에 해당하는 가구가 없습니다.")
        else:
            self.empty.setText("표시할 가입·배정 가구가 없습니다.")
