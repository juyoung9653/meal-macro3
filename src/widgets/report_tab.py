"""연간 배송 장부 기반 배송 리포트 탭."""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timezone
from typing import ClassVar

from PyQt6.QtCore import QDate, QSettings, QSize, Qt, QUrl
from PyQt6.QtGui import QDesktopServices, QTextDocument
from PyQt6.QtWidgets import (
    QCheckBox,
    QDateEdit,
    QFileDialog,
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
from ..generators.report_generator import export_multi
from ..services.delivery_service import DeliveryService
from ..widgets.order_editor import StaffOrderDialog

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

        org = self._s.get("app", "organization")
        app_title = self._s.get("app", "title")
        self._settings = QSettings(org, app_title)

        self._book: AnnualDeliveryBook | None = None
        self._service: DeliveryService | None = None
        self._last_results: list[tuple[str, str, list[list[object]]]] = []
        self._last_export_path: str | None = None

        self._setup_ui()
        self.set_annual_book(annual_book)

    def _load_staff_sort(self) -> dict[str, list[str]]:
        import json

        result: dict[str, list[str]] = {}
        for meal in ["조식", "중식", "석식"]:
            key = f"report/staff_order_{meal}"
            val = self._settings.value(key)
            if val is not None:
                try:
                    if isinstance(val, str):
                        loaded = json.loads(val)
                    elif isinstance(val, list):
                        loaded = val
                    else:
                        continue
                    if isinstance(loaded, list):
                        result[meal] = [
                            item for item in loaded if isinstance(item, str)
                        ]
                except (json.JSONDecodeError, TypeError) as error:
                    log.warning("배송순서 설정을 읽지 못했습니다 (%s): %s", meal, error)
        return result

    # ── UI ──

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        # ── ① 날짜 범위 ──
        date_group = QGroupBox("① 날짜 범위")
        date_layout = QHBoxLayout(date_group)

        self.date_start = QDateEdit()
        self.date_start.setCalendarPopup(True)
        self.date_start.setDate(QDate.currentDate())
        self.date_start.setDisplayFormat("yyyy-MM-dd")
        date_layout.addWidget(QLabel("시작:"))
        date_layout.addWidget(self.date_start)

        self.date_end = QDateEdit()
        self.date_end.setCalendarPopup(True)
        self.date_end.setDate(QDate.currentDate())
        self.date_end.setDisplayFormat("yyyy-MM-dd")
        date_layout.addWidget(QLabel("종료:"))
        date_layout.addWidget(self.date_end)

        self._default_date_min = self.date_start.minimumDate()
        self._default_date_max = self.date_start.maximumDate()

        self.lunch_check = QCheckBox("중식만")
        self.lunch_check.setToolTip(
            "체크: 중식만 | 해제: 매트릭스에서 실제 1이 있는 모든 식종"
        )
        date_layout.addWidget(self.lunch_check)
        date_layout.addStretch()
        layout.addWidget(date_group)

        # ── 버튼 ──
        btn_layout = QHBoxLayout()
        self.build_btn = QPushButton("리포트 생성")
        self.build_btn.setEnabled(False)
        self.build_btn.setMinimumHeight(32)
        self.build_btn.clicked.connect(self._on_build)

        self.export_btn = QPushButton("엑셀로 저장")
        self.export_btn.setEnabled(False)
        self.export_btn.setMinimumHeight(32)
        self.export_btn.clicked.connect(self._on_export)

        self.open_btn = QPushButton("파일 열기")
        self.open_btn.setEnabled(False)
        self.open_btn.setMinimumHeight(32)
        self.open_btn.clicked.connect(self._on_open)

        self.order_btn = QPushButton("배송순서 편집")
        self.order_btn.setMinimumHeight(32)
        self.order_btn.clicked.connect(self._on_edit_order)

        btn_layout.addStretch()
        btn_layout.addWidget(self.build_btn)
        btn_layout.addWidget(self.export_btn)
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
        self.build_btn.setEnabled(has_dates)
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
                self._set_status("연간 배송 장부를 불러오세요.")
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
        self._last_export_path = None
        self.export_btn.setEnabled(False)
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

    # ── 리포트 생성 ──

    def _on_build(self) -> None:
        service = self._service
        if self._book is None or service is None:
            self._reset_output_state()
            self._set_status("연간 배송 장부를 먼저 불러오세요.")
            QMessageBox.warning(self, "알림", "연간 배송 장부를 먼저 불러오세요.")
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

        meal_filter = "중식" if self.lunch_check.isChecked() else None
        log.debug(
            "[ReportTab] _on_build: %s ~ %s meal_filter=%s",
            start,
            end,
            meal_filter,
        )

        self._reset_output_state()
        try:
            results = service.build_all(start, end, meal_filter=meal_filter)
            self._last_results = results
            self._update_results(results)
            self.export_btn.setEnabled(bool(results))
            if results:
                self._set_status(f"{len(results)}개 리포트 생성 완료")
            else:
                self._set_status("생성된 리포트 없음")
        except Exception as e:
            log.exception("리포트 생성 실패")
            self._reset_output_state()
            self._set_status("리포트 생성 실패")
            QMessageBox.critical(self, "오류", f"리포트 생성 실패:\n{e}")

    def _update_results(
        self, results: list[tuple[str, str, list[list[object]]]]
    ) -> None:
        self.result_tabs.clear()

        if not results:
            return

        for date_str, meal, table in results:
            tab = QWidget()
            tab_layout = QVBoxLayout(tab)

            tw = QTableWidget()
            if table and len(table) > 0:
                headers = [str(value) for value in table[0]]
                tw.setColumnCount(len(headers))
                tw.setHorizontalHeaderLabels(headers)
                tw.setRowCount(len(table) - 1)

                horizontal_header = tw.horizontalHeader()
                if horizontal_header is not None:
                    header_font = horizontal_header.font()
                    header_font.setBold(True)
                    horizontal_header.setFont(header_font)

                for ri, row in enumerate(table[1:]):
                    for ci, val in enumerate(row):
                        item = QTableWidgetItem(str(val) if val is not None else "")
                        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                        tw.setItem(ri, ci, item)

                if horizontal_header is not None:
                    horizontal_header.setStretchLastSection(True)
                    horizontal_header.setSectionResizeMode(
                        QHeaderView.ResizeMode.Stretch
                    )
                tw.setAlternatingRowColors(True)
                tw.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)

                # 비고 컬럼 delegate (index 7)
                if len(headers) > 7 and headers[7] == "비고":
                    tw.setItemDelegateForColumn(7, MemoHighlightDelegate(tw))

                tw.resizeRowsToContents()

            tab_layout.addWidget(tw)

            title = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}({meal})"
            self.result_tabs.addTab(tab, title)

    # ── 내보내기 ──

    def _on_export(self) -> None:
        if not self._last_results:
            QMessageBox.warning(self, "알림", "먼저 리포트를 생성하세요.")
            return

        dir_hint = self._settings.value("output_dir", "")
        out_dir = QFileDialog.getExistingDirectory(self, "저장 폴더 선택", dir_hint)
        if not out_dir:
            return
        self._settings.setValue("output_dir", out_dir)

        # 기본 파일명
        qd = self.date_start.date()
        fname = f"배송리포트_{qd.year():04d}{qd.month():02d}{qd.day():02d}.xlsx"
        out_path = os.path.join(out_dir, fname)

        self._last_export_path = None
        self.open_btn.setEnabled(False)
        try:
            export_multi(self._last_results, out_path)
            if not os.path.isfile(out_path):
                raise FileNotFoundError(
                    f"리포트 파일이 생성되지 않았습니다: {out_path}"
                )
            self._last_export_path = out_path
            self.open_btn.setEnabled(True)
            self._set_status(f"저장 완료: {os.path.basename(out_path)}")
            QMessageBox.information(self, "완료", f"리포트 저장 완료:\n{out_path}")
        except Exception as e:
            log.exception("리포트 저장 실패")
            self._set_status("리포트 저장 실패")
            QMessageBox.critical(self, "오류", f"리포트 저장 실패:\n{e}")

    def _on_open(self) -> None:
        if self._last_export_path and os.path.exists(self._last_export_path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(self._last_export_path))

    # ── 배송순서 편집 ──

    def _on_edit_order(self) -> None:
        book = self._book
        if book is None:
            QMessageBox.warning(self, "알림", "연간 배송 장부를 먼저 불러오세요.")
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
