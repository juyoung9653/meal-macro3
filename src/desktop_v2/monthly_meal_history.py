"""Read-only monthly meal action history work window."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from PySide6.QtCore import QSignalBlocker, Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QHBoxLayout, QHeaderView, QLabel,
    QPushButton, QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from src.services.monthly_meal_history import MonthlyMealHistory
from .column_search import ColumnSearchHeader
from .theme import application_stylesheet, register_application_fonts


@dataclass(frozen=True)
class _Column:
    key: str
    label: str


_COLUMNS = tuple(_Column(key, label) for key, label in (
    ("name", "성명"), ("meal", "식종"), ("status", "구분"),
    ("period", "적용 기간"), ("actor", "변경자"), ("revision", "변경번호"),
))


class MonthlyMealHistoryDialog(QDialog):
    def __init__(self, selected_day: date, load: Callable[[int, int], MonthlyMealHistory], parent: QWidget | None = None):
        super().__init__(parent)
        register_application_fonts()
        self.setStyleSheet(application_stylesheet())
        self.setWindowTitle("월별 식종 변경 이력")
        self.resize(900, 680)
        self.setMinimumSize(760, 480)
        self._load = load
        self._column_searches: dict[str, str] = {}
        self._column_values: dict[str, frozenset[str]] = {}
        self._loaded = False
        root = QVBoxLayout(self)
        title = QLabel("월별 식종 변경 이력")
        title.setObjectName("workspaceTitle")
        root.addWidget(title)
        scope = QLabel("전체 명단 · 선택한 달의 배송·일시중지·중지 변경 기록")
        scope.setObjectName("workspaceSubtitle")
        root.addWidget(scope)
        note = QLabel("종료일 없는 기록은 시작한 달에만 표시합니다. 기간이 정해진 기록은 그 기간과 겹치는 달마다 표시합니다.")
        note.setWordWrap(True)
        root.addWidget(note)
        period = QHBoxLayout()
        previous = QPushButton("이전 달")
        previous.setObjectName("quietAction")
        previous.clicked.connect(lambda: self._shift_month(-1))
        period.addWidget(previous)
        self.year = QSpinBox()
        self.year.setObjectName("monthlyMealHistoryYear")
        self.year.setRange(1900, 9999)
        self.year.setValue(selected_day.year)
        self.year.setSuffix("년")
        period.addWidget(self.year)
        self.month = QComboBox()
        self.month.setObjectName("monthlyMealHistoryMonth")
        for number in range(1, 13):
            self.month.addItem(f"{number}월", number)
        self.month.setCurrentIndex(selected_day.month - 1)
        period.addWidget(self.month)
        next_month = QPushButton("다음 달")
        next_month.setObjectName("quietAction")
        next_month.clicked.connect(lambda: self._shift_month(1))
        period.addWidget(next_month)
        refresh = QPushButton("새로고침")
        refresh.setObjectName("quietAction")
        refresh.clicked.connect(self.refresh)
        period.addWidget(refresh)
        self.clear_filters_button = QPushButton("전체 필터 해제")
        self.clear_filters_button.setObjectName("quietAction")
        self.clear_filters_button.setEnabled(False)
        self.clear_filters_button.clicked.connect(self._clear_all_filters)
        period.addWidget(self.clear_filters_button)
        period.addStretch(1)
        root.addLayout(period)
        self.year.valueChanged.connect(self.refresh)
        self.month.currentIndexChanged.connect(self.refresh)
        self.error = QLabel()
        self.error.setObjectName("errorMessage")
        self.error.setWordWrap(True)
        self.error.hide()
        root.addWidget(self.error)
        self.table = QTableWidget(0, 6)
        self.table.setObjectName("monthlyMealHistoryTable")
        self._column_search_header = ColumnSearchHeader(
            self.table, values_provider=self.column_filter_values,
            selection_provider=self._column_values.get,
            search_provider=lambda key: self._column_searches.get(key, ""),
        )
        self.table.setHorizontalHeader(self._column_search_header)
        self.table.setHorizontalHeaderLabels(tuple(column.label for column in _COLUMNS))
        self._column_search_header.replace_columns(_COLUMNS)
        self._column_search_header.search_changed.connect(self._set_column_search)
        self._column_search_header.values_changed.connect(self._set_column_values)
        self.table.horizontalScrollBar().valueChanged.connect(self._column_search_header.update_positions)
        self._column_search_header.setToolTip("열 제목 옆 ▾: 검색과 값 선택 필터")
        self._column_search_header.setMinimumSectionSize(80)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for column, width in enumerate((120, 85, 100, 270, 110, 170)):
            self.table.setColumnWidth(column, width)
        self.table.setWordWrap(True)
        self.table.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        root.addWidget(self.table, 1)
        self.summary = QLabel()
        self.summary.setObjectName("sectionLabel")
        root.addWidget(self.summary)
        revision_note = QLabel("조회 당시 최신 운영 데이터 기준 · 새로고침으로 변경 내용을 반영합니다")
        revision_note.setObjectName("workspaceSubtitle")
        root.addWidget(revision_note)
        actions = QHBoxLayout()
        actions.addStretch(1)
        close = QPushButton("닫기")
        close.setObjectName("quietAction")
        close.clicked.connect(self.accept)
        actions.addWidget(close)
        root.addLayout(actions)
        self.refresh()

    def _shift_month(self, offset: int) -> None:
        absolute = self.year.value() * 12 + self.month.currentData() - 1 + offset
        year, month_zero = divmod(absolute, 12)
        if not 1900 <= year <= 9999:
            return
        with QSignalBlocker(self.year), QSignalBlocker(self.month):
            self.year.setValue(year)
            self.month.setCurrentIndex(month_zero)
        self.refresh()

    def refresh(self, *_args) -> None:
        self._loaded = False
        self.table.setRowCount(0)
        self.summary.clear()
        try:
            result = self._load(self.year.value(), self.month.currentData())
            if not isinstance(result, MonthlyMealHistory):
                raise TypeError("월별 식종 변경 이력 조회 결과가 올바르지 않습니다.")
            if (result.year, result.month) != (self.year.value(), self.month.currentData()):
                raise ValueError("조회한 달과 결과의 달이 다릅니다.")
        except Exception as error:
            self.error.setText("월별 식종 변경 이력을 불러오지 못했습니다. 다시 조회해주세요.\n" + str(error))
            self.error.show()
            return
        self.error.hide()
        self.table.setRowCount(len(result.entries))
        for row, item in enumerate(result.entries):
            start = item.period.starts_on.isoformat() if item.period.starts_on else "시작일 미지정"
            end = item.period.ends_on.isoformat() if item.period.ends_on else "종료일 없음"
            values = (item.name, item.meal.value, item.status.value,
                      f"{start} ~ {end}", item.actor, item.revision_code)
            for column, value in enumerate(values):
                cell = QTableWidgetItem(value)
                cell.setToolTip(value)
                self.table.setItem(row, column, cell)
        self._loaded = True
        self._apply_filters()

    def column_filter_values(self, key: str) -> tuple[str, ...]:
        column = next(index for index, item in enumerate(_COLUMNS) if item.key == key)
        return tuple(sorted({self.table.item(row, column).text()
                             for row in range(self.table.rowCount())}, key=str.casefold))

    def _set_column_search(self, key: str, text: str) -> None:
        query = " ".join(text.split()).casefold()
        if query:
            self._column_searches[key] = query
        else:
            self._column_searches.pop(key, None)
        self._apply_filters()

    def _set_column_values(self, key: str, values) -> None:
        if values is None:
            self._column_values.pop(key, None)
        else:
            self._column_values[key] = frozenset(values)
        self._apply_filters()

    def _clear_all_filters(self) -> None:
        self._column_searches.clear()
        self._column_values.clear()
        self._apply_filters()

    def _apply_filters(self) -> None:
        shown = 0
        for row in range(self.table.rowCount()):
            matches = True
            for column, definition in enumerate(_COLUMNS):
                value = self.table.item(row, column).text()
                query = self._column_searches.get(definition.key, "")
                selected = self._column_values.get(definition.key)
                if (query not in " ".join(value.split()).casefold()
                        or (selected is not None and value not in selected)):
                    matches = False
                    break
            self.table.setRowHidden(row, not matches)
            shown += matches
        active = set(self._column_searches) | set(self._column_values)
        self._column_search_header.refresh_buttons()
        self.clear_filters_button.setEnabled(bool(active))
        if not self._loaded:
            self.summary.clear()
            return
        total = self.table.rowCount()
        text = (f"조회 {shown:,}건 / 전체 {total:,}건 · 적용 시작일이 최근인 순서"
                if total or active else "선택한 달에 적용되는 식종 변경 이력이 없습니다.")
        if active:
            text += "\n필터: " + ", ".join(column.label for column in _COLUMNS if column.key in active)
        self.summary.setText(text)
