"""배송 리포트 탭 — DbReader + DeliveryService + ReportGenerator 통합."""

from __future__ import annotations

import logging
import os
from datetime import date

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

from ..core.settings import get_settings
from ..excel.data_source import DataSource
from ..excel.db_reader import get_db_reader
from ..generators.report_generator import export_multi
from ..services.delivery_service import DeliveryService
from ..widgets.meal_calendar import MealCalendarWidget
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

    TABLE_COLS = ["배송담당", "1", "2", "3", "4", "5", "Total", "비고", "참고"]

    def __init__(self, meal_type: str = "", data_source: DataSource | None = None):
        super().__init__()
        self._s = get_settings()
        self._meal_type = meal_type or self._s.get_meal_type()

        org = self._s.get("app", "organization")
        app_title = self._s.get("app", "title")
        self._settings = QSettings(org, app_title)

        # DataSource (주입 or 기본)
        if data_source is not None:
            self._ds = data_source
            self._db_reader = None
        else:
            self._db_reader = get_db_reader(self._s.get("db", "path"))
            self._ds = self._db_reader

        # DeliveryService
        self._service = DeliveryService(self._ds, self._load_staff_sort())

        # State
        self._roster_loaded = False
        self._last_results: list[tuple[str, str, list[list]]] = []
        self._last_export_path: str | None = None

        self._setup_ui()

    def _load_staff_sort(self) -> dict[str, list[str]]:
        import json

        result: dict[str, list[str]] = {}
        for meal in ["조식", "중식", "석식"]:
            key = f"report/staff_order_{meal}"
            val = self._settings.value(key)
            if val is not None:
                try:
                    if isinstance(val, str):
                        result[meal] = json.loads(val)
                    elif isinstance(val, list):
                        result[meal] = val
                except Exception:
                    pass
        return result

    # ── UI ──

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        # ── ① 날짜 범위 ──
        date_group = QGroupBox("① 날짜 범위")
        date_layout = QHBoxLayout(date_group)

        self.date_start = QDateEdit()
        self.date_start.setCalendarPopup(True)
        self._cal_start = MealCalendarWidget()
        self.date_start.setCalendarWidget(self._cal_start)
        self.date_start.setDate(QDate.currentDate())
        self.date_start.setDisplayFormat("yyyy-MM-dd")
        date_layout.addWidget(QLabel("시작:"))
        date_layout.addWidget(self.date_start)

        self.date_end = QDateEdit()
        self.date_end.setCalendarPopup(True)
        self._cal_end = MealCalendarWidget()
        self.date_end.setCalendarWidget(self._cal_end)
        self.date_end.setDate(QDate.currentDate())
        self.date_end.setDisplayFormat("yyyy-MM-dd")
        date_layout.addWidget(QLabel("종료:"))
        date_layout.addWidget(self.date_end)

        self.lunch_check = QCheckBox("중식만")
        self.lunch_check.setToolTip(
            "체크: 중식만 | 해제: 요일별 자동 (월~목 석식, 금 조식)"
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
        self.info_label = QLabel("명단을 불러오고 날짜를 선택하세요.")
        self.info_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        result_layout.addWidget(self.info_label)
        layout.addWidget(result_group, stretch=1)

    # ── 명단 변경 처리 ──

    def on_roster_changed(self):
        """MainWindow에서 명단 파일 변경 시 호출."""
        try:
            roster = self._ds.read_roster()
            if not roster:
                QMessageBox.warning(
                    self, "알림", "db.xlsx에서 명단 데이터를 찾을 수 없습니다."
                )
                return

            self._roster_loaded = True
            self.build_btn.setEnabled(True)

            # 휴무일 로드 → 캘린더에 반영
            holiday_set = self._ds.get_holiday_set()
            self._cal_start.set_holidays(holiday_set)
            self._cal_end.set_holidays(holiday_set)

            # 연월 자동 감지 → 날짜 범위 설정
            try:
                y, m = self._ds.detect_month()
                dm = QDate(y, m, 1)
                today = QDate.currentDate()
                if today.year() == y and today.month() == m:
                    self.date_start.setDate(today)
                else:
                    self.date_start.setDate(dm)
                import calendar as _cal

                last_day = _cal.monthrange(y, m)[1]
                self.date_end.setDate(QDate(y, m, last_day))
            except Exception:
                pass

        except FileNotFoundError:
            QMessageBox.warning(self, "오류", f"명단 파일 없음: {self._ds.db_path}")
        except Exception as e:
            QMessageBox.critical(self, "오류", f"명단 파일 로드 실패:\n{e}")

    def set_meal_type(self, meal_type: str):
        """식사 유형 변경 (현재 리포트 탭에서는 단순 저장)."""
        self._meal_type = meal_type

    # ── 리포트 생성 ──

    def _on_build(self):
        try:
            qd_start = self.date_start.date()
            qd_end = self.date_end.date()
            start = date(qd_start.year(), qd_start.month(), qd_start.day())
            end = date(qd_end.year(), qd_end.month(), qd_end.day())

            meal_filter = "중식" if self.lunch_check.isChecked() else None

            # 리포트 생성
            results = self._service.build_all(start, end, meal_filter=meal_filter)
            # 오류(데이터 없음) 결과 제외
            results = [r for r in results if r[2] and len(r[2][0]) > 1]
            self._last_results = results

            # UI 갱신
            self._update_results(results)

            self.info_label.setText(f"{len(results)}개 리포트 생성 완료")
            self.export_btn.setEnabled(True)

        except Exception as e:
            log.exception("리포트 생성 실패")
            QMessageBox.critical(self, "오류", f"리포트 생성 실패:\n{e}")

    def _update_results(self, results: list[tuple[str, str, list[list]]]):
        self.result_tabs.clear()

        if not results:
            self.info_label.setText("생성된 리포트 없음")
            return

        for date_str, meal, table in results:
            tab = QWidget()
            tab_layout = QVBoxLayout(tab)

            tw = QTableWidget()
            if table and len(table) > 0:
                headers = table[0]
                tw.setColumnCount(len(headers))
                tw.setHorizontalHeaderLabels(headers)
                tw.setRowCount(len(table) - 1)

                header_font = tw.horizontalHeader().font()
                header_font.setBold(True)
                tw.horizontalHeader().setFont(header_font)

                for ri, row in enumerate(table[1:]):
                    for ci, val in enumerate(row):
                        item = QTableWidgetItem(str(val) if val is not None else "")
                        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                        tw.setItem(ri, ci, item)

                tw.horizontalHeader().setStretchLastSection(True)
                tw.horizontalHeader().setSectionResizeMode(
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

    def _on_export(self):
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

        try:
            export_multi(self._last_results, out_path)
            self._last_export_path = out_path
            self.open_btn.setEnabled(True)
            self.info_label.setText(f"저장 완료: {os.path.basename(out_path)}")
            QMessageBox.information(self, "완료", f"리포트 저장 완료:\n{out_path}")
        except Exception as e:
            log.exception("리포트 저장 실패")
            QMessageBox.critical(self, "오류", f"리포트 저장 실패:\n{e}")

    def _on_open(self):
        if self._last_export_path and os.path.exists(self._last_export_path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(self._last_export_path))

    # ── 배송순서 편집 ──

    def _on_edit_order(self):
        # 현재 서비스에서 담당자 목록 수집
        staff_by_meal: dict[str, list[str]] = {}
        try:
            staff_set = set()
            for row in self._ds.read_delivery_order():
                s = row.get("배송담당", "").strip()
                if s:
                    staff_set.add(s)

            # 간단히 모든 식종에 동일 담당자 할당
            for meal in ["조식", "중식", "석식"]:
                staff_by_meal[meal] = sorted(staff_set)
        except Exception:
            pass

        dlg = StaffOrderDialog(staff_by_meal, parent=self)
        if dlg.exec() == StaffOrderDialog.DialogCode.Accepted:
            new_sort = dlg.staff_sort
            self._service = DeliveryService(self._ds, new_sort)
