"""Read-only monthly meal statistics work window."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date

from PySide6.QtCore import QSignalBlocker, Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QHBoxLayout, QHeaderView, QLabel,
    QPushButton, QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from src.services.monthly_meal_statistics import MonthlyMealStatistics
from .theme import application_stylesheet, register_application_fonts


class MonthlyStatisticsDialog(QDialog):
    def __init__(self, selected_day: date, load: Callable[[int, int], MonthlyMealStatistics], parent: QWidget | None = None, *, embedded=False):
        super().__init__(parent)
        register_application_fonts()
        self.setStyleSheet(application_stylesheet())
        self.setWindowTitle("월별 식수")
        self.resize(640, 700)
        self.setMinimumSize(500, 500)
        if embedded:
            self.setWindowFlags(Qt.WindowType.Widget)
            self.setMinimumSize(0, 0)
        self._load = load
        root = QVBoxLayout(self)
        title = QLabel("월별 식수")
        title.setObjectName("workspaceTitle")
        root.addWidget(title)
        title.setVisible(not embedded)
        scope = QLabel("실제 배송일 기준 · 전체 명단")
        scope.setObjectName("workspaceSubtitle")
        root.addWidget(scope)
        period = QHBoxLayout()
        previous = QPushButton("이전 달")
        previous.setObjectName("quietAction")
        previous.clicked.connect(lambda: self._shift_month(-1))
        period.addWidget(previous)
        self.year = QSpinBox()
        self.year.setObjectName("monthlyStatisticsYear")
        self.year.setRange(1900, 9999)
        self.year.setValue(selected_day.year)
        self.year.setSuffix("년")
        period.addWidget(self.year)
        self.month = QComboBox()
        self.month.setObjectName("monthlyStatisticsMonth")
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
        period.addStretch(1)
        period_host = QWidget()
        period_host.setLayout(period)
        root.addWidget(period_host)
        period_host.setVisible(not embedded)
        self.year.valueChanged.connect(self.refresh)
        self.month.currentIndexChanged.connect(self.refresh)
        self.error = QLabel()
        self.error.setObjectName("errorMessage")
        self.error.setWordWrap(True)
        self.error.hide()
        root.addWidget(self.error)
        self.table = QTableWidget(0, 5)
        self.table.setObjectName("monthlyMealStatisticsTable")
        self.table.setHorizontalHeaderLabels(("일자", "석식", "조식", "중식", "합계"))
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        root.addWidget(self.table, 1)
        self.summary = QLabel()
        self.summary.setObjectName("sectionLabel")
        root.addWidget(self.summary)
        self.revision_note = QLabel()
        self.revision_note.setObjectName("workspaceSubtitle")
        root.addWidget(self.revision_note)
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
        self.table.setRowCount(0)
        self.summary.clear()
        self.revision_note.clear()
        try:
            result = self._load(self.year.value(), self.month.currentData())
            if not isinstance(result, MonthlyMealStatistics):
                raise TypeError("월별 식수 계산 결과가 올바르지 않습니다.")
            if (result.year, result.month) != (self.year.value(), self.month.currentData()):
                raise ValueError("조회한 달과 계산 결과의 달이 다릅니다.")
        except Exception as error:
            self.error.setText("월별 식수를 불러오지 못했습니다. 운영 데이터를 확인한 뒤 다시 조회해주세요.\n" + str(error))
            self.error.show()
            return
        self.error.hide()
        self.table.setRowCount(len(result.days) + 1)
        for row, item in enumerate(result.days):
            self._set_row(row, (item.day.isoformat(), item.dinner, item.breakfast, item.lunch, item.total))
        self._set_row(len(result.days), ("월 합계", result.dinner_total, result.breakfast_total, result.lunch_total, result.total))
        self.summary.setText(f"월 합계  석식 {result.dinner_total:,} · 조식 {result.breakfast_total:,} · 중식 {result.lunch_total:,} · 전체 {result.total:,}")
        next_month = "포함" if result.include_next_month else "제외"
        self.revision_note.setText(f"다음 달 식수 {next_month} · 설정 → 식수·청구서 설정에서 변경\n조회 당시 최신 운영 데이터 기준 · 새로고침으로 변경 내용을 반영합니다")

    def _set_row(self, row: int, values) -> None:
        for column, value in enumerate(values):
            cell = QTableWidgetItem(str(value))
            cell.setTextAlignment(Qt.AlignmentFlag.AlignVCenter | (Qt.AlignmentFlag.AlignLeft if column == 0 else Qt.AlignmentFlag.AlignRight))
            self.table.setItem(row, column, cell)
