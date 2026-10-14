"""연간 배송 장부 기반 배송 리포트 탭."""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timezone
from typing import ClassVar

from PyQt6.QtCore import QDate, QSize, Qt
from PyQt6.QtGui import QTextDocument
from PyQt6.QtWidgets import (
    QCheckBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QStyle,
    QStyledItemDelegate,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..core.delivery_matrix import ALLOWED_MEALS, AnnualDeliveryBook, MonthState
from ..core.settings import get_settings
from ..core.user_preferences import UserPreferences
from ..excel.report_viewer import open_excel
from ..excel.report_viewer import show_excel as show_report_excel
from ..services.delivery_service import REPORT_HEADERS, DeliveryReport, DeliveryService
from ..widgets.order_editor import StaffOrderDialog
from .date_edit import WheelSafeDateEdit

log = logging.getLogger(__name__)


class MemoHighlightDelegate(QStyledItemDelegate):
    """비고 셀: 줄바꿈 기준 홀수 줄 빨간색."""

    @staticmethod
    def _memo_html(text: str) -> str:
        parts = ["<div style='white-space:pre-wrap;'>"]
        for i, line in enumerate(text.split("\n")):
            c = "red" if i % 2 == 0 else "black"
            parts.append(f"<span style='color:{c};font-weight:bold;'>{line}</span><br>")
        parts.append("</div>")
        return "".join(parts)

    def paint(self, painter, option, index) -> None:
        if painter is None:
            return
        painter.save()
        if option.state & QStyle.StateFlag.State_Selected:
            painter.fillRect(option.rect, option.palette.highlight())
        elif option.state & QStyle.StateFlag.State_MouseOver:
            painter.fillRect(option.rect, option.palette.light())

        html = self._memo_html(index.data(Qt.ItemDataRole.DisplayRole) or "")
        doc = QTextDocument()
        doc.setHtml(html)
        doc.setDefaultFont(option.font)
        doc.setTextWidth(option.rect.width())
        painter.translate(option.rect.topLeft())
        doc.drawContents(painter)
        painter.restore()

    def sizeHint(self, option, index) -> QSize:
        html = self._memo_html(index.data(Qt.ItemDataRole.DisplayRole) or "")
        doc = QTextDocument()
        doc.setHtml(html)
        doc.setDefaultFont(option.font)
        return QSize(int(doc.idealWidth()), int(doc.size().height()))


class ReportTab(QWidget):
    """배송 리포트 ③번째 탭."""

    TABLE_COLS: ClassVar[list[str]] = [
        "배송담당",
        "1",
        "2",
        "3",
        "4",
        "5",
        "Total",
        "비고",
        "참고",
    ]

    def __init__(
        self,
        meal_type: str = "",
        annual_book: AnnualDeliveryBook | None = None,
    ) -> None:
        super().__init__()
        self._s = get_settings()
        self._meal_type = meal_type or self._s.get_meal_type()

        self._preferences = UserPreferences(self._s)

        self._book: AnnualDeliveryBook | None = None
        self._service: DeliveryService | None = None
        self._last_results: list[DeliveryReport] = []
        self._last_excel_path: str | None = None

        self._setup_ui()
        self.set_annual_book(annual_book)

    def _load_staff_sort(self) -> dict[str, list[str]]:
        return {
            meal: order
            for meal in ["조식", "중식", "석식"]
            if (order := self._preferences.get_staff_order(meal))
        }

    # ── UI ──

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        # ── ① 날짜 범위 ──
        date_group = QGroupBox("① 날짜 범위")
        date_layout = QHBoxLayout(date_group)

        self.date_start = WheelSafeDateEdit()
        self.date_start.setCalendarPopup(True)
        self.date_start.setDate(QDate.currentDate())
        self.date_start.setDisplayFormat("yyyy-MM-dd")
        date_layout.addWidget(QLabel("시작:"))
        date_layout.addWidget(self.date_start)

        self.date_end = WheelSafeDateEdit()
        self.date_end.setCalendarPopup(True)
        self.date_end.setDate(QDate.currentDate())
        self.date_end.setDisplayFormat("yyyy-MM-dd")
        date_layout.addWidget(QLabel("종료:"))
        date_layout.addWidget(self.date_end)

        self._default_date_min = self.date_start.minimumDate()
        self._default_date_max = self.date_start.maximumDate()

        date_layout.addWidget(QLabel("식종:"))
        self.lunch_check = QCheckBox("중식")
        self.lunch_check.setChecked(True)
        self.lunch_check.setToolTip(
            "체크하면 중식 리포트를 포함합니다."
        )
        date_layout.addWidget(self.lunch_check)

        # Qt는 단일 '&'를 단축키 표시로 해석하므로 '&&'로 실제 문자를 표시한다.
        self.breakfast_dinner_check = QCheckBox("석식&&조식")
        self.breakfast_dinner_check.setChecked(True)
        self.breakfast_dinner_check.setToolTip(
            "체크하면 석식과 조식 리포트를 포함합니다."
        )
        date_layout.addWidget(self.breakfast_dinner_check)
        date_layout.addStretch()
        layout.addWidget(date_group)

        # ── 버튼 ──
        btn_layout = QHBoxLayout()
        self.build_btn = QPushButton("리포트 생성")
        self.build_btn.setEnabled(False)
        self.build_btn.setMinimumHeight(32)
        self.build_btn.clicked.connect(self._on_build)

        self.open_btn = QPushButton("파일 열기")
        self.open_btn.setEnabled(False)
        self.open_btn.setMinimumHeight(32)
        self.open_btn.clicked.connect(self._on_open)

        self.order_btn = QPushButton("배송순서 편집")
        self.order_btn.setMinimumHeight(32)
        self.order_btn.clicked.connect(self._on_edit_order)

        self.lunch_check.toggled.connect(self._update_build_enabled)
        self.breakfast_dinner_check.toggled.connect(self._update_build_enabled)

        btn_layout.addStretch()
        btn_layout.addWidget(self.build_btn)
        btn_layout.addWidget(self.open_btn)
        btn_layout.addWidget(self.order_btn)
        layout.addLayout(btn_layout)

        # ── 결과 ──
        result_group = QGroupBox("② 결과")
        result_layout = QVBoxLayout(result_group)
        self.result_tabs = QTabWidget()
        self.result_tabs.setTabPosition(QTabWidget.TabPosition.North)
        result_layout.addWidget(self.result_tabs)
        self.info_label = QLabel("연도: - | 명단: 0명 | 범위: 없음")
        self.info_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        result_layout.addWidget(self.info_label)
        layout.addWidget(result_group, stretch=1)

    # ── 연간 배송 장부 ──

    def set_annual_book(self, book: AnnualDeliveryBook | None) -> None:
        """연간 배송 장부를 교체하고 장부에 종속된 UI 상태를 초기화한다."""
        self._book = book
        self._service = (
            DeliveryService(book, self._load_staff_sort())
            if book is not None
            else None
        )
        self._reset_output_state()

        has_dates = book is not None and bool(book.dates)
        self._update_build_enabled()
        self.order_btn.setEnabled(book is not None)
        self.date_start.setEnabled(has_dates)
        self.date_end.setEnabled(has_dates)

        local_today = datetime.now(timezone.utc).astimezone().date()
        if book is None or not book.dates:
            self._restore_default_date_limits()
            today = self._to_qdate(local_today)
            self.date_start.setDate(today)
            self.date_end.setDate(today)
            if book is None:
                self._set_status("db 파일을 불러오세요.")
            else:
                self._set_status("배송 날짜 헤더가 없습니다.")
            return

        first_day = min(book.dates)
        last_day = max(book.dates)
        q_first = self._to_qdate(first_day)
        q_last = self._to_qdate(last_day)
        self.date_start.setDateRange(q_first, q_last)
        self.date_end.setDateRange(q_first, q_last)

        default_range = self._default_date_range(book, local_today)
        if default_range is None:
            default_range = first_day, last_day
        self.date_start.setDate(self._to_qdate(default_range[0]))
        self.date_end.setDate(self._to_qdate(default_range[1]))
        self._set_status("리포트를 생성할 날짜를 선택하세요.")

    @staticmethod
    def _default_month(book: AnnualDeliveryBook, today: date) -> int | None:
        """장부와 기준일만으로 기본 월을 결정한다."""
        months = book.available_months
        if not months:
            return None
        if (
            book.year == today.year
            and today.month in months
            and book.month_state(book.year, today.month) is MonthState.ENTERED
        ):
            return today.month

        entered_months = [
            month
            for month in months
            if book.month_state(book.year, month) is MonthState.ENTERED
        ]
        return max(entered_months) if entered_months else max(months)

    @classmethod
    def _default_date_range(
        cls, book: AnnualDeliveryBook, today: date
    ) -> tuple[date, date] | None:
        """기본 월에 실제 존재하는 날짜 헤더의 최소·최대 범위를 반환한다."""
        month = cls._default_month(book, today)
        if month is None:
            return None
        month_dates = [day for day in book.dates if day.month == month]
        return min(month_dates), max(month_dates)

    @staticmethod
    def _to_qdate(day: date) -> QDate:
        return QDate(day.year, day.month, day.day)

    def _restore_default_date_limits(self) -> None:
        self.date_start.setDateRange(self._default_date_min, self._default_date_max)
        self.date_end.setDateRange(self._default_date_min, self._default_date_max)

    def _reset_output_state(self) -> None:
        self.result_tabs.clear()
        self._last_results = []
        self._last_excel_path = None
        self.open_btn.setEnabled(False)

    def _book_status_text(self) -> str:
        book = self._book
        if book is None:
            return "연도: - | 명단: 0명 | 범위: 없음"
        if not book.dates:
            return f"연도: {book.year}년 | 명단: {len(book.roster)}명 | 범위: 없음"
        return (
            f"연도: {book.year}년 | 명단: {len(book.roster)}명 | "
            f"범위: {min(book.dates):%Y-%m-%d} ~ {max(book.dates):%Y-%m-%d}"
        )

    def _set_status(self, message: str = "") -> None:
        status = self._book_status_text()
        self.info_label.setText(f"{status} | {message}" if message else status)

    def set_meal_type(self, meal_type: str) -> None:
        """식사 유형 변경 (현재 리포트 탭에서는 단순 저장)."""
        self._meal_type = meal_type

    def _selected_meals(self) -> tuple[str, ...]:
        selected: set[str] = set()
        if self.lunch_check.isChecked():
            selected.add("중식")
        if self.breakfast_dinner_check.isChecked():
            selected.update(("조식", "석식"))
        return tuple(meal for meal in ALLOWED_MEALS if meal in selected)

    def _update_build_enabled(self, _checked: bool | None = None) -> None:
        has_dates = self._book is not None and bool(self._book.dates)
        self.build_btn.setEnabled(has_dates and bool(self._selected_meals()))

    # ── 리포트 생성 ──

    def _on_build(self) -> None:
        service = self._service
        if self._book is None or service is None:
            self._reset_output_state()
            self._set_status("db 파일을 먼저 불러오세요.")
            QMessageBox.warning(self, "알림", "db 파일을 먼저 불러오세요.")
            return

        qd_start = self.date_start.date()
        qd_end = self.date_end.date()
        start = date(qd_start.year(), qd_start.month(), qd_start.day())
        end = date(qd_end.year(), qd_end.month(), qd_end.day())
        if start > end:
            self._reset_output_state()
            self._set_status("날짜 범위를 확인하세요.")
            QMessageBox.warning(self, "알림", "시작일은 종료일보다 늦을 수 없습니다.")
            return

        selected_meals = self._selected_meals()
        if not selected_meals:
            self._set_status("식종을 하나 이상 선택하세요.")
            QMessageBox.warning(self, "알림", "식종을 하나 이상 선택하세요.")
            self._update_build_enabled()
            return

        log.debug(
            "[ReportTab] _on_build: %s ~ %s selected_meals=%s",
            start,
            end,
            selected_meals,
        )

        self._reset_output_state()
        try:
            results = service.build_all(start, end, meal_filter=selected_meals)
        except Exception as e:
            log.exception("리포트 생성 실패")
            self._reset_output_state()
            self._set_status("리포트 생성 실패")
            QMessageBox.critical(self, "오류", f"리포트 생성 실패:\n{e}")
            return

        self._last_results = results
        self._update_results(results)
        if not results:
            self._set_status("생성된 리포트 없음")
            return

        try:
            self._last_excel_path = show_report_excel(results)
        except Exception as e:
            log.exception("리포트 Excel 출력 실패")
            self._last_excel_path = None
            self.open_btn.setEnabled(False)
            self._set_status(
                f"{len(results)}개 리포트 생성 완료 · Excel 출력 실패"
            )
            QMessageBox.critical(
                self,
                "오류",
                f"리포트는 생성했지만 Excel 출력에 실패했습니다:\n{e}",
            )
            return

        self.open_btn.setEnabled(True)
        self._set_status(f"{len(results)}개 리포트 생성 및 Excel 실행 완료")

    def _update_results(self, results: list[DeliveryReport]) -> None:
        self.result_tabs.clear()

        for report in results:
            tab = QWidget()
            tab_layout = QVBoxLayout(tab)

            tw = QTableWidget()
            headers = list(REPORT_HEADERS)
            tw.setColumnCount(len(headers))
            tw.setHorizontalHeaderLabels(headers)
            tw.setRowCount(len(report.rows) + 1)

            horizontal_header = tw.horizontalHeader()
            if horizontal_header is not None:
                header_font = horizontal_header.font()
                header_font.setBold(True)
                horizontal_header.setFont(header_font)

            for row_index, row in enumerate(report.rows):
                values = (
                    row.staff_label,
                    *row.household_size_cells,
                    row.person_total,
                    row.memo,
                    row.reference_areas,
                )
                for column_index, value in enumerate(values):
                    item = QTableWidgetItem(str(value))
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                    tw.setItem(row_index, column_index, item)

            totals = report.totals
            summary_values = (
                "합계",
                *totals.household_counts,
                totals.person_total,
                "",
                "",
            )
            for column_index, value in enumerate(summary_values):
                item = QTableWidgetItem(str(value))
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                tw.setItem(len(report.rows), column_index, item)

            if horizontal_header is not None:
                horizontal_header.setStretchLastSection(True)
                horizontal_header.setSectionResizeMode(
                    QHeaderView.ResizeMode.Stretch
                )
            tw.setAlternatingRowColors(True)
            tw.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)

            # 비고 컬럼 delegate (index 7)
            tw.setItemDelegateForColumn(7, MemoHighlightDelegate(tw))
            tw.resizeRowsToContents()

            tab_layout.addWidget(tw)
            self.result_tabs.addTab(tab, f"{report.day:%Y-%m-%d}({report.meal})")

    def _on_open(self) -> None:
        if not self._last_excel_path or not os.path.exists(self._last_excel_path):
            return
        try:
            open_excel(self._last_excel_path)
        except Exception as e:
            log.exception("리포트 Excel 다시 열기 실패")
            QMessageBox.critical(self, "오류", f"Excel을 열지 못했습니다:\n{e}")

    # ── 배송순서 편집 ──

    def _on_edit_order(self) -> None:
        book = self._book
        if book is None:
            QMessageBox.warning(self, "알림", "db 파일을 먼저 불러오세요.")
            return

        staff_sets: dict[str, set[str]] = {meal: set() for meal in ALLOWED_MEALS}
        for row in book.delivery_rows:
            staff = row.staff.strip()
            if staff:
                staff_sets[row.meal].add(staff)
        staff_by_meal = {
            meal: sorted(staff_sets[meal], key=lambda name: (name.casefold(), name))
            for meal in ALLOWED_MEALS
        }

        dlg = StaffOrderDialog(staff_by_meal, parent=self)
        if dlg.exec() == StaffOrderDialog.DialogCode.Accepted:
            self._service = DeliveryService(book, dlg.staff_sort)
            self._reset_output_state()
            self._set_status("배송순서를 저장했습니다. 리포트를 다시 생성하세요.")
