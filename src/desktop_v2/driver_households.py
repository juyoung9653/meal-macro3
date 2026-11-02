"""Household manifest with scoped sequence editing for one service day."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING
from uuid import UUID

from PySide6.QtCore import QEvent, QSettings, Qt, Signal
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QStyle,
    QStyleOptionHeader,
    QTableWidgetItem,
    QVBoxLayout,
)

from src.domain.delivery_routing import Driver
from src.domain.roster import Meal
from src.services.driver_households import sequence_sort_key as _sequence_key

from .column_search import ColumnSearchHeader
from .current_sheet_export import CurrentSheetSnapshot
from .delivery_routing import DriverOrderTable
from .driver_households_export import build_driver_households_template_snapshot
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


class _DeliveryOrderTable(DriverOrderTable):
    """Name the row-number header without adding an editable sequence column."""

    def __init__(self):
        super().__init__(0)
        self.verticalHeader().setMinimumWidth(86)
        self.corner_header = self.findChild(QAbstractButton)
        if self.corner_header is not None:
            self.corner_header.setText("배송순서")
            self.corner_header.setAccessibleName("배송순서")
            self.corner_header.installEventFilter(self)

    def eventFilter(self, watched, event):
        if watched is getattr(self, "corner_header", None) and event.type() == QEvent.Type.Paint:
            option = QStyleOptionHeader()
            option.initFrom(watched)
            option.rect = watched.rect()
            option.text = "배송순서"
            option.textAlignment = Qt.AlignmentFlag.AlignCenter
            painter = QPainter(watched)
            watched.style().drawControl(QStyle.ControlElement.CE_Header, option, painter, watched)
            painter.end()
            return True
        return super().eventFilter(watched, event)


def _sequence_targets(row):
    # Merged rows require exact meal projections; the cartesian product of
    # household members and meals could update meals absent from this manifest.
    meal_rows = row.meal_rows or ((row,) if len(row.meals) == 1 else ())
    return tuple(dict.fromkeys(
        (person_id, meal_row.meals[0])
        for meal_row in meal_rows if len(meal_row.meals) == 1
        for person_id in meal_row.member_ids
    ))


class DriverHouseholdsDialog(QDialog):
    """Show service-day household rows supplied by the projection service.

    This dialog intentionally does not build or combine projection rows.  A row is
    already one household/driver manifest entry when it arrives here.
    """

    sequence_save_requested = Signal(object, str)
    order_save_requested = Signal(object)
    export_requested = Signal(object)

    def __init__(
        self,
        rows: tuple[DriverHouseholdRow, ...],
        *,
        service_day: date,
        drivers: tuple[Driver, ...],
        selected_driver_id: UUID | None = None,
        rows_provider: Callable[[bool], tuple[DriverHouseholdRow, ...]] | None = None,
        settings: QSettings | None = None,
        settings_key: str | None = None,
        parent=None,
    ):
        super().__init__(parent)
        register_application_fonts()
        self.setObjectName("driverHouseholdsDialog")
        self.setWindowTitle("배송순서관리")
        self.setStyleSheet(application_stylesheet())
        self.resize(1000, 600)

        self._rows = tuple(rows)
        self._rows_provider = rows_provider
        identities = {}
        for row in self._rows:
            identities.setdefault(row.household_key, set()).add((row.driver_id, row.driver_name))
        self._split_by_meal = any(len(values) > 1 for values in identities.values())
        self._drivers = tuple(drivers)
        self._selected_driver_id = selected_driver_id if isinstance(selected_driver_id, UUID) else None
        self._column_searches: dict[str, str] = {}
        self._column_values: dict[str, frozenset[str]] = {}
        self._filter_settings = settings if settings_key else None
        self._filter_settings_key = settings_key
        layout = QVBoxLayout(self)

        title = QLabel("배송순서관리")
        title.setObjectName("workspaceTitle")
        layout.addWidget(title)
        self.day_label = QLabel(f"선택일: {service_day:%Y-%m-%d}")
        self.day_label.setObjectName("fieldLabel")
        layout.addWidget(self.day_label)
        self.scope_label = QLabel()
        self.scope_label.setObjectName("driverHouseholdsScope")
        layout.addWidget(self.scope_label)
        guide = QLabel("기사를 선택하고 행을 위아래로 드래그하세요. 놓은 위치가 배송순서로 저장됩니다.")
        guide.setWordWrap(True)
        guide.setObjectName("workspaceSubtitle")
        layout.addWidget(guide)

        filters = QHBoxLayout()
        self.driver_combo = QComboBox()
        self.driver_combo.setAccessibleName("배송기사 선택")
        for driver in self._drivers:
            self.driver_combo.addItem(driver.name, (driver.driver_id, driver.name))
        known = {(driver.driver_id, driver.name) for driver in self._drivers}
        for row in self._rows:
            identity = (row.driver_id, row.driver_name)
            if identity not in known:
                label = f"{row.driver_name} (기존 배정)" if row.driver_id is None and row.driver_name else row.driver_name or "미지정"
                self.driver_combo.addItem(label, identity)
                known.add(identity)
        if self._selected_driver_id is not None:
            for index in range(self.driver_combo.count()):
                if self.driver_combo.itemData(index)[0] == self._selected_driver_id:
                    self.driver_combo.setCurrentIndex(index)
                    break
        filters.addWidget(QLabel("배송기사"))
        filters.addWidget(self.driver_combo)
        self.meal_combo = QComboBox()
        self.meal_combo.setAccessibleName("배송순서 식종 선택")
        self.meal_combo.addItem("전체 식종", None)
        for meal in (Meal.DINNER, Meal.BREAKFAST, Meal.LUNCH):
            self.meal_combo.addItem(meal.value, meal.value)
        filters.addWidget(QLabel("식종"))
        filters.addWidget(self.meal_combo)
        filters.addStretch(1)
        self.clear_filters_button = QPushButton("전체 필터 해제")
        self.clear_filters_button.setObjectName("quietAction")
        self.clear_filters_button.setToolTip("열 검색과 값 선택 필터를 모두 해제")
        self.clear_filters_button.clicked.connect(self._clear_all_filters)
        filters.addWidget(self.clear_filters_button)
        self.include_hidden_checkbox = QCheckBox("숨긴 명단 포함")
        self.include_hidden_checkbox.setObjectName("includeHiddenHouseholds")
        self.include_hidden_checkbox.setToolTip("중지·현재 상태가 없는 식종과 장기 미배송으로 숨긴 대상자를 포함")
        self.include_hidden_checkbox.setEnabled(rows_provider is not None)
        self.include_hidden_checkbox.toggled.connect(self._set_include_hidden)
        filters.addWidget(self.include_hidden_checkbox)
        self.split_button = QPushButton("분리 해제" if self._split_by_meal else "식종별 분리")
        self.split_button.setObjectName("quietAction")
        self.split_button.setToolTip("가구 행을 식종별 배송 행으로 표시")
        self.split_button.clicked.connect(self._toggle_meal_rows)
        filters.addWidget(self.split_button)
        self.export_button = QPushButton("Excel 내보내기")
        self.export_button.setObjectName("quietAction")
        self.export_button.setToolTip("현재 필터와 정렬이 적용된 표시 내용을 Excel로 저장")
        self.export_button.clicked.connect(self._request_export)
        self.template_export_button = QPushButton("기사·식종별 Excel 내보내기")
        self.template_export_button.setObjectName("quietAction")
        self.template_export_button.setToolTip(
            "열 필터와 화면 정렬을 제외하고 기사·식종별 시트로 저장"
        )
        self.template_export_button.clicked.connect(self._request_template_export)
        layout.addLayout(filters)
        exports = QHBoxLayout()
        exports.addStretch(1)
        exports.addWidget(self.export_button)
        exports.addWidget(self.template_export_button)
        layout.addLayout(exports)

        self.table = _DeliveryOrderTable()
        self.table.setColumnCount(len(_COLUMNS))
        self.table.orderChanged.connect(self._save_row_order)
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
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
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
        self.table.setSortingEnabled(False)
        self.table.setColumnHidden(0, True)
        self.table.setColumnHidden(3, True)
        header.setToolTip("행 드래그: 배송순서 변경 · ▾: 칼럼별 검색과 값 필터")
        layout.addWidget(self.table, 1)

        self.sequence_status = QLabel("행을 드래그하면 배송순서가 즉시 저장됩니다. 순서는 날짜 제한 없이 적용됩니다.")
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

        self._restore_filter_settings()
        self.driver_combo.currentIndexChanged.connect(self._change_driver)
        self.meal_combo.currentIndexChanged.connect(self._change_meal)
        self._change_driver()
        self._refresh()
        self._update_filter_controls()

    @property
    def include_hidden(self) -> bool:
        return self.include_hidden_checkbox.isChecked()

    def refresh_rows(self):
        """Reload using the provider and service day captured when opened."""
        if self._rows_provider is not None:
            self.replace_rows(self._rows_provider(self.include_hidden))

    def replace_rows(self, rows):
        self._rows = tuple(rows)
        known = {tuple(self.driver_combo.itemData(index)) for index in range(self.driver_combo.count())}
        for row in self._rows:
            identity = (row.driver_id, row.driver_name)
            if identity not in known:
                label = f"{row.driver_name} (기존 배정)" if row.driver_id is None and row.driver_name else row.driver_name or "미지정"
                self.driver_combo.addItem(label, identity)
                known.add(identity)
        self._refresh()

    def show_sequence_result(self, result):
        from .sequence_edit import SequenceSaveStatus

        problem = result.status is SequenceSaveStatus.FAILED or result.backup_warning
        self.sequence_status.setStyleSheet("color: #B42318;" if problem else "")
        self.sequence_status.setText(result.message)
        if result.status is SequenceSaveStatus.FAILED:
            self._refresh()

    def _change_driver(self, *_args):
        identity = self.driver_combo.currentData()
        self._selected_driver_id = identity[0] if identity else None
        self._column_searches.pop("driver", None)
        self._column_values.pop("driver", None)
        self._refresh()

    def _matches_driver(self, row):
        identity = self.driver_combo.currentData()
        return identity is not None and (row.driver_id, row.driver_name) == tuple(identity)

    def _save_row_order(self):
        reordered = tuple(self.table.item(index, 3).data(Qt.ItemDataRole.UserRole)
                          for index in range(self.table.rowCount()))
        visible_ids = {id(row) for row in reordered}
        affected_meals = {meal for row in reordered for _, meal in _sequence_targets(row)}
        # Replace only visible slots in the full route. Hidden households keep
        # their positions relative to those slots, and unrelated meals/drivers
        # are never included in the saved targets.
        route = sorted((row for row in self._display_rows()
                        if self._matches_driver(row) and any(meal in affected_meals for meal in row.meals)),
                       key=lambda row: _sequence_key(row.sequences))
        moved = iter(reordered)
        route = tuple(next(moved) if id(row) in visible_ids else row for row in route)
        values = {}
        for index, row in enumerate(route):
            for target in _sequence_targets(row):
                if target[1] in affected_meals:
                    values[target] = str(index + 1)
        self._set_order_headers(route, preview=True)
        if values:
            self.order_save_requested.emit(values)

    def _set_order_headers(self, route, *, preview=False):
        positions = {id(row): index + 1 for index, row in enumerate(route)}
        for index in range(self.table.rowCount()):
            row = self.table.item(index, 3).data(Qt.ItemDataRole.UserRole)
            if preview:
                label = str(positions[id(row)])
            elif len(row.sequences) <= 1 and not (row.has_empty_sequence and any(row.sequences)):
                label = next(iter(row.sequences), "") or "-"
            else:
                label = "\n".join(
                    f"{part.meals[0].value}: {', '.join(part.sequences) or '-'}"
                    for part in (row.meal_rows or (row,))
                )
            item = QTableWidgetItem(label)
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setVerticalHeaderItem(index, item)

    def current_view_snapshot(self) -> CurrentSheetSnapshot:
        """Freeze displayed strings in current visual column and sorted row order."""

        header = self.table.horizontalHeader()
        logical_columns = tuple(
            logical
            for visual in range(header.count())
            if (logical := header.logicalIndex(visual)) >= 0
            and not self.table.isColumnHidden(logical)
        )
        if not logical_columns:
            raise ValueError("배송순서관리에 표시된 칼럼이 없어 내보낼 수 없습니다.")
        headers = tuple(
            self.table.horizontalHeaderItem(logical).text()
            for logical in logical_columns
        )
        rows = tuple(
            tuple(
                self.table.item(row, logical).text()
                if self.table.item(row, logical) is not None else ""
                for logical in logical_columns
            )
            for row in range(self.table.rowCount())
        )
        return CurrentSheetSnapshot("배송순서관리", headers, rows)

    def _request_export(self) -> None:
        self.export_requested.emit(self.current_view_snapshot())

    def _request_template_export(self) -> None:
        source_rows = self._rows if self.meal_combo.currentData() is None else self._display_rows()
        snapshot = build_driver_households_template_snapshot(
            tuple(row for row in source_rows if self._matches_driver(row)),
        )
        self.export_requested.emit(snapshot)

    def _set_include_hidden(self, include_hidden: bool) -> None:
        if self._rows_provider is None:
            return
        self.refresh_rows()

    def _visible_rows(self) -> tuple[DriverHouseholdRow, ...]:
        visible = []
        for row in self._display_rows():
            if not self._matches_driver(row):
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
        return tuple(sorted(visible, key=lambda row: _sequence_key(row.sequences)))

    def _display_rows(self) -> tuple[DriverHouseholdRow, ...]:
        selected_meal = self.meal_combo.currentData()
        if not self._split_by_meal and selected_meal is None:
            return self._rows
        return tuple(
            meal_row
            for row in self._rows
            for meal_row in (row.meal_rows or (row,))
            if selected_meal is None or Meal(selected_meal) in meal_row.meals
        )

    def _change_meal(self, *_args):
        self.split_button.setEnabled(self.meal_combo.currentData() is None)
        self._column_searches.pop("meals", None)
        self._column_values.pop("meals", None)
        self._refresh()
        self._update_filter_controls()
        self._save_filter_settings()

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
        self._update_filter_controls()
        self._save_filter_settings()

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
                             if self._matches_driver(row)}, key=str.casefold))

    def _set_column_search(self, key, text):
        normalized = " ".join(text.split()).casefold()
        if self._column_searches.get(key, "") == normalized:
            return
        if normalized:
            self._column_searches[key] = normalized
        else:
            self._column_searches.pop(key, None)
        self._refresh()
        self._update_filter_controls()
        self._save_filter_settings()

    def _set_column_values(self, key, values):
        values = None if values is None else frozenset(values)
        if self._column_values.get(key) == values:
            return
        if values is None:
            self._column_values.pop(key, None)
        else:
            self._column_values[key] = frozenset(values)
        self._refresh()
        self._update_filter_controls()
        self._save_filter_settings()

    def _clear_all_filters(self) -> None:
        if not self._column_searches and not self._column_values:
            return
        self._column_searches.clear()
        self._column_values.clear()
        self._refresh()
        self._update_filter_controls()
        self._save_filter_settings()

    def _update_filter_controls(self) -> None:
        self._column_search_header.refresh_buttons()
        self.clear_filters_button.setEnabled(
            bool(self._column_searches) or bool(self._column_values)
        )
        self.sequence_status.setText("필터를 유지한 채 드래그할 수 있습니다. 숨긴 행은 원래 위치에 두고 선택한 식종의 순서를 저장합니다. 순서는 날짜 제한 없이 적용됩니다.")

    def _save_filter_settings(self) -> None:
        if self._filter_settings is None or self._filter_settings_key is None:
            return
        state = {
            "address_columns_version": 2,
            "columns": dict(self._column_searches),
            "values": {
                key: sorted(values) for key, values in self._column_values.items()
            },
        }
        if not self._column_searches and not self._column_values:
            self._filter_settings.remove(self._filter_settings_key)
        else:
            self._filter_settings.setValue(
                self._filter_settings_key, json.dumps(state, ensure_ascii=False)
            )
        self._filter_settings.sync()

    def _restore_filter_settings(self) -> None:
        if self._filter_settings is None or self._filter_settings_key is None:
            return
        raw = self._filter_settings.value(self._filter_settings_key)
        if not isinstance(raw, str):
            return
        try:
            state = json.loads(raw)
        except (TypeError, ValueError):
            return
        if not isinstance(state, dict):
            return

        allowed_keys = set(_COLUMN_KEYS) - {"driver", "sequence"}
        if state.get("address_columns_version") != 2:
            allowed_keys.discard("address")

        searches = state.get("columns")
        if isinstance(searches, dict):
            for key, search in searches.items():
                if key in allowed_keys and isinstance(search, str):
                    normalized = " ".join(search.split()).casefold()
                    if normalized:
                        self._column_searches[key] = normalized

        selections = state.get("values")
        if isinstance(selections, dict):
            for key, values in selections.items():
                if (
                    key in allowed_keys
                    and isinstance(values, list)
                    and all(isinstance(value, str) for value in values)
                ):
                    self._column_values[key] = frozenset(values)

    def _refresh(self) -> None:
        self.scope_label.setText(
            "가입·배정된 모든 식종 · 중지·현재 상태 없음 포함"
            if self.include_hidden
            else "선택일 기준 배송·일시중지 식종"
        )
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
                    if column == 0:
                        item.setData(Qt.ItemDataRole.UserRole, (
                            row.household_key, row.driver_id, row.driver_name, row.address, row.member_ids, row.meals,
                        ))
                    self.table.setItem(index, column, item)
            self.table.setSortingEnabled(False)
            self._set_order_headers(sorted((row for row in self._display_rows() if self._matches_driver(row)),
                                           key=lambda row: _sequence_key(row.sequences)))
        finally:
            row_header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        if selected_identity is not None:
            for index in range(self.table.rowCount()):
                if self.table.item(index, 0).data(Qt.ItemDataRole.UserRole) == selected_identity:
                    self.table.selectRow(index)
                    break
        households = len({row.household_key for row in rows})
        self.table.setDragEnabled(True)
        self.table.setAcceptDrops(True)
        self.count.setText(f"가구 {households}건 · 표시 {len(rows)}행")
        if rows:
            self.empty.setText("")
        elif self._rows:
            self.empty.setText("현재 기사와 칼럼 필터에 해당하는 가구가 없습니다.")
        else:
            self.empty.setText("표시할 가입·배정 가구가 없습니다.")
