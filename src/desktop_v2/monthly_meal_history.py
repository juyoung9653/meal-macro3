"""Read-only monthly meal action history work window."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
import re

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
    ("intake_channel", "접수 경로"), ("period", "적용 기간"), ("note", "비고"),
    ("actor", "변경자"), ("revision", "변경번호"),
))


def _revision_sort_key(value):
    return tuple((0, int(part)) if part.isdigit() else (1, part.casefold())
                 for part in re.split(r"(\d+)", value))


def _entry_values(item):
    start = item.period.starts_on.isoformat() if item.period.starts_on else "시작일 미지정"
    end = item.period.ends_on.isoformat() if item.period.ends_on else ""
    return (item.name, item.meal.value, item.status.value, item.intake_channel or "-",
            f"{start} ~ {end}", item.note, item.actor, item.revision_code)


class MonthlyMealHistoryDialog(QDialog):
    def __init__(self, selected_day: date, load: Callable[[int, int], MonthlyMealHistory], parent: QWidget | None = None, *, embedded=False):
        super().__init__(parent)
        register_application_fonts()
        self.setStyleSheet(application_stylesheet())
        self.setWindowTitle("월별 식종 변경 이력")
        self.resize(900, 680)
        self.setMinimumSize(760, 480)
        if embedded:
            self.setWindowFlags(Qt.WindowType.Widget)
            self.setMinimumSize(0, 0)
        self._load = load
        self._column_searches: dict[str, str] = {}
        self._column_values: dict[str, frozenset[str]] = {}
        self._loaded = False
        self._entries = ()
        self._sort_criteria = ()
        root = QVBoxLayout(self)
        title = QLabel("월별 식종 변경 이력")
        title.setObjectName("workspaceTitle")
        root.addWidget(title)
        title.setVisible(not embedded)
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
        period_host = QWidget()
        period_host.setLayout(period)
        root.addWidget(period_host)
        if embedded:
            period.removeWidget(self.clear_filters_button)
            root.addWidget(self.clear_filters_button)
            period_host.hide()
        self.year.valueChanged.connect(self.refresh)
        self.month.currentIndexChanged.connect(self.refresh)
        self.error = QLabel()
        self.error.setObjectName("errorMessage")
        self.error.setWordWrap(True)
        self.error.hide()
        root.addWidget(self.error)
        self.table = QTableWidget(0, len(_COLUMNS))
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
        self._column_search_header.setSectionsClickable(True)
        self._column_search_header.sectionClicked.connect(self._cycle_sort)
        self.table.horizontalScrollBar().valueChanged.connect(self._column_search_header.update_positions)
        self._column_search_header.setToolTip("열 제목 클릭: 오름차순 → 내림차순 → 해제 · 먼저 선택한 열부터 우선 정렬 · ▾: 검색과 값 필터")
        self._column_search_header.setMinimumSectionSize(80)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for column, width in enumerate((120, 85, 100, 100, 235, 220, 110, 170)):
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
        close.setVisible(not embedded)
        root.addLayout(actions)
        if not embedded:
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
        self._entries = ()
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
        self._entries = tuple(sorted(result.entries,
            key=lambda item: _revision_sort_key(item.revision_code), reverse=True))
        self._loaded = True
        self._render_sorted_entries()

    def _cycle_sort(self, column):
        if not 0 <= column < len(_COLUMNS):
            return
        key = _COLUMNS[column].key
        criteria = list(self._sort_criteria)
        found = next((index for index, (name, _) in enumerate(criteria) if name == key), None)
        if found is None:
            criteria.append((key, Qt.SortOrder.AscendingOrder))
        elif criteria[found][1] == Qt.SortOrder.AscendingOrder:
            criteria[found] = (key, Qt.SortOrder.DescendingOrder)
        else:
            criteria.pop(found)
        self._sort_criteria = tuple(criteria)
        self._render_sorted_entries()

    def _render_sorted_entries(self):
        entries = list(self._entries)
        for key, order in reversed(self._sort_criteria):
            column = next(index for index, item in enumerate(_COLUMNS) if item.key == key)
            def sort_value(item):
                if key == "revision":
                    return _revision_sort_key(item.revision_code)
                if key == "period":
                    return (item.period.starts_on or date.min, item.period.ends_on or date.max)
                return _entry_values(item)[column].casefold()
            entries.sort(key=sort_value, reverse=order == Qt.SortOrder.DescendingOrder)
        self.table.setRowCount(len(entries))
        for row, item in enumerate(entries):
            for column, value in enumerate(_entry_values(item)):
                cell = QTableWidgetItem(value)
                cell.setToolTip(value)
                self.table.setItem(row, column, cell)
        self._column_search_header.set_sort_criteria(
            self._sort_criteria or (("revision", Qt.SortOrder.DescendingOrder),))
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
        sort_text = "선택한 열 기준 정렬" if self._sort_criteria else "최신 변경번호 순서"
        text = (f"조회 {shown:,}건 / 전체 {total:,}건 · {sort_text}"
                if total or active else "선택한 달에 적용되는 식종 변경 이력이 없습니다.")
        if active:
            text += "\n필터: " + ", ".join(column.label for column in _COLUMNS if column.key in active)
        self.summary.setText(text)
