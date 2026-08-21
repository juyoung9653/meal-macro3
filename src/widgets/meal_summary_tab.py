"""선택 월의 일자별·식종별 식수를 보여주는 탭."""

from __future__ import annotations

from datetime import datetime

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core.invoice_data import InvoiceData
from ..services.meal_summary import build_monthly_meal_summary


class MealSummaryTab(QWidget):
    """현재 명단을 선택 월 기준 일자·식종 식수로 집계한다."""

    month_changed = pyqtSignal(int, int)
    WEEKDAY_NAMES = ("월", "화", "수", "목", "금", "토", "일")

    def __init__(self):
        super().__init__()
        self._data = InvoiceData()
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        intro = QLabel(
            "청구서 명단과 확인서 계산 기준으로 날짜별 조식·석식·중식 식수를 확인합니다."
        )
        intro.setStyleSheet("color: #666;")
        layout.addWidget(intro)

        month_group = QGroupBox("조회 월")
        month_layout = QHBoxLayout(month_group)
        today = datetime.now().astimezone().date()

        self.year_spin = QSpinBox()
        self.year_spin.setRange(2020, 2100)
        self.year_spin.setValue(today.year)
        self.month_spin = QSpinBox()
        self.month_spin.setRange(1, 12)
        self.month_spin.setValue(today.month)

        month_layout.addWidget(self.year_spin)
        month_layout.addWidget(QLabel("년"))
        month_layout.addWidget(self.month_spin)
        month_layout.addWidget(QLabel("월"))
        month_layout.addStretch()
        layout.addWidget(month_group)

        self.status_label = QLabel("청구서 탭에서 명단을 불러오면 식수가 표시됩니다.")
        self.status_label.setStyleSheet("color: #666;")
        layout.addWidget(self.status_label)

        table_group = QGroupBox("일자별 식수")
        table_layout = QVBoxLayout(table_group)
        self.table = QTableWidget()
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels(
            ["일자", "요일", "조식", "석식", "중식", "합계"]
        )
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        table_layout.addWidget(self.table)
        layout.addWidget(table_group, stretch=1)

        self.year_spin.valueChanged.connect(self._on_month_changed)
        self.month_spin.valueChanged.connect(self._on_month_changed)

    def selected_month(self) -> tuple[int, int]:
        return self.year_spin.value(), self.month_spin.value()

    def set_month(self, year: int, month: int):
        self.year_spin.blockSignals(True)
        self.month_spin.blockSignals(True)
        self.year_spin.setValue(year)
        self.month_spin.setValue(month)
        self.year_spin.blockSignals(False)
        self.month_spin.blockSignals(False)

    def _on_month_changed(self):
        self.month_changed.emit(*self.selected_month())

    @staticmethod
    def _number_item(value: int) -> QTableWidgetItem:
        item = QTableWidgetItem(f"{value:,}")
        item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        return item

    def set_invoice_data(self, data: InvoiceData):
        self._data = data
        self._refresh_table()

    def _refresh_table(self):
        if self._data.preparation_error:
            self.table.setRowCount(0)
            self.status_label.setText(self._data.preparation_error)
            self.status_label.setStyleSheet("color: #b42318; font-weight: bold;")
            return
        if not self._data.invoices:
            self.table.setRowCount(0)
            self.status_label.setText(
                "표시할 명단이 없습니다. 청구서 탭에서 명단을 먼저 불러오세요."
            )
            self.status_label.setStyleSheet("color: #666;")
            return

        year, month = self.selected_month()
        rows = build_monthly_meal_summary(
            self._data.invoices,
            year,
            month,
            self._data.excluded_dates,
        )
        self.table.setRowCount(len(rows) + 1)

        totals = [0, 0, 0, 0]
        for row_index, meal_count in enumerate(rows):
            target = meal_count.delivery_date
            date_item = QTableWidgetItem(target.strftime("%m/%d"))
            weekday_item = QTableWidgetItem(self.WEEKDAY_NAMES[target.weekday()])
            date_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            weekday_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            if target.weekday() == 5:
                date_item.setForeground(QColor("#2563a6"))
                weekday_item.setForeground(QColor("#2563a6"))
            elif target.weekday() == 6:
                date_item.setForeground(QColor("#b42318"))
                weekday_item.setForeground(QColor("#b42318"))

            values = (
                meal_count.breakfast,
                meal_count.dinner,
                meal_count.lunch,
                meal_count.total,
            )
            self.table.setItem(row_index, 0, date_item)
            self.table.setItem(row_index, 1, weekday_item)
            for column, value in enumerate(values, start=2):
                self.table.setItem(row_index, column, self._number_item(value))
            totals = [total + value for total, value in zip(totals, values)]

        total_row = len(rows)
        total_label = QTableWidgetItem("월 합계")
        total_label.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        self.table.setItem(total_row, 0, total_label)
        self.table.setSpan(total_row, 0, 1, 2)
        for column, value in enumerate(totals, start=2):
            self.table.setItem(total_row, column, self._number_item(value))
        for column in range(self.table.columnCount()):
            item = self.table.item(total_row, column)
            if item is not None:
                item.setBackground(QColor("#dbeafe"))
                font = item.font()
                font.setBold(True)
                item.setFont(font)

        warning_text = ""
        if self._data.preparation_warnings:
            warning_text = f" · 확인 필요 {len(self._data.preparation_warnings)}건"
        self.status_label.setText(
            f"{year}년 {month}월 · 대상 {len(self._data.invoices):,}명{warning_text}"
        )
        self.status_label.setStyleSheet("color: #245b45; font-weight: bold;")
        self.table.scrollToTop()
