"""Read-only household manifest grouped by the delivery driver for one day."""

from __future__ import annotations

from datetime import date
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from src.domain.delivery_routing import Driver

from .column_search import ColumnSearchHeader
from .theme import application_stylesheet, register_application_fonts

if TYPE_CHECKING:
    from src.services.driver_households import DriverHouseholdRow


_ALL_DRIVERS = "__all_drivers__"
_LEGACY_DRIVER = "__legacy_driver__"
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


class DriverHouseholdsDialog(QDialog):
    """Show service-day household rows supplied by the projection service.

    This dialog intentionally does not build or combine projection rows.  A row is
    already one household/driver manifest entry when it arrives here.
    """

    def __init__(
        self,
        rows: tuple[DriverHouseholdRow, ...],
        *,
        service_day: date,
        drivers: tuple[Driver, ...],
        selected_driver_id: UUID | None = None,
        parent=None,
    ):
        super().__init__(parent)
        register_application_fonts()
        self.setObjectName("driverHouseholdsDialog")
        self.setWindowTitle("기사 담당가구")
        self.setStyleSheet(application_stylesheet())
        self.resize(1000, 600)

        self._rows = tuple(rows)
        self._split_by_meal = False
        self._drivers = tuple(drivers)
        self._column_searches: dict[str, str] = {}
        self._column_values: dict[str, frozenset[str]] = {}
        layout = QVBoxLayout(self)

        title = QLabel("기사 담당가구")
        title.setObjectName("workspaceTitle")
        layout.addWidget(title)
        self.day_label = QLabel(f"선택일: {service_day:%Y-%m-%d}")
        self.day_label.setObjectName("fieldLabel")
        layout.addWidget(self.day_label)
        guide = QLabel("선택일의 배송 상태인 식종을 기사별로 모았습니다.")
        guide.setObjectName("workspaceSubtitle")
        layout.addWidget(guide)

        filters = QHBoxLayout()
        filters.addWidget(QLabel("배송기사"))
        self.driver_filter = QComboBox()
        self.driver_filter.setObjectName("driverHouseholdsDriverFilter")
        self.driver_filter.addItem("전체 기사", _ALL_DRIVERS)
        self.driver_filter.addItem("미지정", None)
        self.driver_filter.addItem("기존 배정", _LEGACY_DRIVER)
        for driver in self._drivers:
            label = driver.name + (f" · {driver.telephone}" if driver.telephone else "")
            self.driver_filter.addItem(label, driver.driver_id)
        filters.addWidget(self.driver_filter)
        filters.addWidget(QLabel("검색"))
        self.search = QLineEdit()
        self.search.setObjectName("driverHouseholdsSearch")
        self.search.setPlaceholderText("가구원, 주소, 연락처 검색")
        self.search.setClearButtonEnabled(True)
        filters.addWidget(self.search, 1)
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
        self.table.setSortingEnabled(True)
        layout.addWidget(self.table, 1)

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

        # A UUID picks a registered driver.  None is the normal all-drivers
        # default; the explicit combo entry is how a user filters unassigned rows.
        # Old name-only selections are deliberately treated as the all-drivers view.
        if isinstance(selected_driver_id, UUID):
            index = self.driver_filter.findData(selected_driver_id)
            if index >= 0:
                self.driver_filter.setCurrentIndex(index)
        self.driver_filter.currentIndexChanged.connect(self._refresh)
        self.search.textChanged.connect(self._refresh)
        self._refresh()


    def _visible_rows(self) -> tuple[DriverHouseholdRow, ...]:
        selected = self.driver_filter.currentData()
        query = self.search.text().strip().casefold()
        visible = []
        for row in self._display_rows():
            if selected is None and not (
                row.driver_id is None and not row.driver_name.strip()
            ):
                continue
            if selected == _LEGACY_DRIVER and not (
                row.driver_id is None and bool(row.driver_name.strip())
            ):
                continue
            if selected not in (_ALL_DRIVERS, None, _LEGACY_DRIVER) and row.driver_id != selected:
                continue
            if query and query not in self._searchable_text(row):
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
        return tuple(sorted({self._display_values(row)[column] for row in self._display_rows()}, key=str.casefold))

    def _set_column_search(self, key, text):
        normalized = " ".join(text.split()).casefold()
        if normalized:
            self._column_searches[key] = normalized
        else:
            self._column_searches.pop(key, None)
        self._refresh()

    def _set_column_values(self, key, values):
        if values is None:
            self._column_values.pop(key, None)
        else:
            self._column_values[key] = frozenset(values)
        self._refresh()

    @staticmethod
    def _searchable_text(row: DriverHouseholdRow) -> str:
        return " ".join((*row.member_names, row.address, *row.contacts)).casefold()

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
        self.table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            values = self._display_values(row)
            for column, value in enumerate(values):
                sort_key = _sequence_key(row.sequences) if column == 3 else (value.casefold(),)
                item = _SortableItem(value, sort_key)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                item.setToolTip(value)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, (
                        row.household_key, row.driver_id, row.driver_name, row.address,
                    ))
                self.table.setItem(index, column, item)
        self.table.setSortingEnabled(True)
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
            self.empty.setText("선택한 기사 또는 검색어에 해당하는 가구가 없습니다.")
        else:
            self.empty.setText("선택일에 배송 상태인 가구가 없습니다.")
