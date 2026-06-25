"""청구서 탭 — db.xlsx 기반 단순화. 파일 로드는 DbReader 위임."""

import logging
import os
from datetime import date

from PyQt6.QtCore import QSettings, Qt, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core.invoice_data import InvoiceData
from ..core.settings import get_settings
from ..excel.db_reader import get_db_reader
from ..services.invoice_controller import InvoiceGenerationConfig, InvoiceService
from ..templates.config import auto_detect_semantic_columns, read_template_columns

log = logging.getLogger(__name__)


class InvoiceTab(QWidget):
    """청구서 생성 탭 — db.xlsx 기반, 외부 파일 로드 완전 제거."""

    MAX_RESULT_ROWS = 10

    def __init__(self, meal_type: str = ""):
        super().__init__()
        self._s = get_settings()
        self._meal_type = meal_type or self._s.get_meal_type()
        self._template_path = self._s.get("templates", "invoice_path")
        self._template_columns = read_template_columns(
            self._template_path, meal_type=self._meal_type
        )
        self._service = InvoiceService()
        self.invoices: list[dict] = []
        self.output_dir: str | None = None
        self.roster_headers: list[str] = []
        self.roster_path: str | None = None
        self._col_mapping: dict = {col: "" for col in self._template_columns}
        _sem = auto_detect_semantic_columns(self._template_columns)
        self._col_mapping.update(_sem)
        self._excluded_dates: set[date] = set()
        self._year: int = 0
        self._month: int = 0
        self._db_path: str = self._s.get("db", "path")
        self._db_reader = get_db_reader(self._db_path)
        org = self._s.get("app", "organization")
        app_title = self._s.get("app", "title")
        self._settings = QSettings(org, app_title)
        self._last_output_path: str | None = None
        self._roster_loaded = False
        self._setup_ui()
        self._restore_ui_state()
        self._load_default_excluded_dates()

    # ── UI ──

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        # ── 청구 기준 ──
        config_group = QGroupBox("① 청구 기준 (엑셀에서 자동 감지)")
        config_row = QHBoxLayout(config_group)

        config_row.addWidget(QLabel("귀속 연월:"))
        self.year_label = QLabel("----")
        self.year_label.setStyleSheet("font-weight: bold; font-size: 14px;")
        config_row.addWidget(self.year_label)
        config_row.addWidget(QLabel("년"))
        self.month_label = QLabel("--")
        self.month_label.setStyleSheet("font-weight: bold; font-size: 14px;")
        config_row.addWidget(self.month_label)
        config_row.addWidget(QLabel("월"))
        config_row.addSpacing(20)

        config_row.addStretch()
        layout.addWidget(config_group)

        # ── 출력 경로 ──
        path_group = QGroupBox("② 출력 경로")
        path_layout = QHBoxLayout(path_group)
        self.path_label = QLabel("선택된 경로 없음")
        self.path_label.setStyleSheet("color: #888;")
        self.path_btn = QPushButton("폴더 선택")
        self.path_btn.clicked.connect(self._select_output_dir)
        path_layout.addWidget(self.path_label, stretch=1)
        path_layout.addWidget(self.path_btn)
        layout.addWidget(path_group)

        # ── 배송 제외일 (배송휴무 시트에서 자동 로드) ──
        exclude_group = QGroupBox("배송 제외일 (배송휴무 시트)")
        exclude_layout = QHBoxLayout(exclude_group)
        self.exclude_label = QLabel("로드 대기 중")
        self.exclude_label.setStyleSheet("color: #888;")
        self.exclude_label.setWordWrap(True)
        exclude_layout.addWidget(self.exclude_label, stretch=1)
        layout.addWidget(exclude_group)

        # ── 버튼 ──
        btn_layout = QHBoxLayout()
        self.generate_btn = QPushButton("청구서 생성")
        self.generate_btn.setEnabled(False)
        self.generate_btn.clicked.connect(self._generate_invoices)
        self.open_file_btn = QPushButton("파일 열기")
        self.open_file_btn.setEnabled(False)
        self.open_file_btn.clicked.connect(self._open_output_file)
        self.clear_btn = QPushButton("초기화")
        self.clear_btn.clicked.connect(self._clear)
        btn_layout.addStretch()
        btn_layout.addWidget(self.generate_btn)
        btn_layout.addWidget(self.open_file_btn)
        btn_layout.addWidget(self.clear_btn)
        layout.addLayout(btn_layout)

        # ── 결과 테이블 ──
        table_group = QGroupBox("③ 생성 결과")
        table_layout = QVBoxLayout(table_group)
        self.result_table = QTableWidget()
        self.result_table.setColumnCount(4)
        self.result_table.setHorizontalHeaderLabels(
            ["이름", "소속", "총급식수", "청구금액"]
        )
        self.result_table.horizontalHeader().setStretchLastSection(True)
        self.result_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self.result_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.result_table.setAlternatingRowColors(True)
        table_layout.addWidget(self.result_table)
        self.info_label = QLabel("명단을 불러오세요.")
        self.info_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        table_layout.addWidget(self.info_label)
        layout.addWidget(table_group, stretch=1)

    # ── 명단 변경 처리 ──

    def on_roster_changed(self):
        """MainWindow에서 명단 파일 변경 시 호출."""
        try:
            roster = self._db_reader.read_roster()
            if not roster:
                QMessageBox.warning(
                    self, "알림", "db.xlsx에서 명단 데이터를 찾을 수 없습니다."
                )
                return

            headers = list(roster[0].keys()) if roster else []
            self.roster_headers = list(headers)
            self.roster_path = self._db_reader.db_path
            self._auto_map_columns()

            self._roster_loaded = True
            self.generate_btn.setEnabled(True)
            self.info_label.setText(
                f"명단 {len(roster)}명 로드됨. 청구서 생성 준비 완료"
            )

            self._auto_detect_month()
            self._save_settings()
        except FileNotFoundError:
            QMessageBox.warning(
                self, "오류", f"db.xlsx 파일 없음: {self._db_reader.db_path}"
            )
        except Exception as e:
            QMessageBox.critical(self, "오류", f"db.xlsx 로드 실패:\n{e}")

    def _auto_detect_month(self):
        """db.xlsx 배송전체 시트에서 연월 자동 감지 → 라벨 + 컨트롤러 설정."""
        try:
            y, m = self._db_reader.detect_month()
            self._year = y
            self._month = m
            self.year_label.setText(str(y))
            self.year_label.setStyleSheet(
                "font-weight: bold; font-size: 14px; color: #0a0;"
            )
            self.month_label.setText(str(m))
            self.month_label.setStyleSheet(
                "font-weight: bold; font-size: 14px; color: #0a0;"
            )
            self._load_default_excluded_dates()
        except Exception as e:
            log.warning("[InvoiceTab] 월 자동감지 실패: %s", e)

    # ── 설정 저장/복원 ──

    def _restore_ui_state(self):
        out = self._settings.value("output_dir", "")
        if out:
            self.output_dir = out
            self.path_label.setText(out)
            self.path_label.setStyleSheet("color: #000;")

        # 컬럼 매핑
        mapping = self._settings.value("invoice/col_mapping", None)
        if mapping is not None:
            self._col_mapping = {
                k: mapping.get(k, v) for k, v in self._col_mapping.items()
            }

        # db.xlsx 경로 복원
        db_path = self._settings.value("db/last_path", "")
        if db_path and db_path != self._db_reader.db_path:
            if os.path.exists(db_path):
                self._db_reader.db_path = db_path
                self._db_path = db_path

    def _save_settings(self):
        self._settings.setValue("invoice/col_mapping", dict(self._col_mapping))
        if self.output_dir:
            self._settings.setValue("output_dir", self.output_dir)
        if self._db_reader.db_path:
            self._settings.setValue("db/last_path", self._db_reader.db_path)
        self._settings.sync()

    def _select_output_dir(self):
        d = QFileDialog.getExistingDirectory(self, "출력 폴더 선택")
        if d:
            self.output_dir = d
            self.path_label.setText(d)
            self.path_label.setStyleSheet("color: #000;")
            self._settings.setValue("output_dir", d)
            self._settings.sync()

    def _load_default_excluded_dates(self):
        """db.xlsx 배송휴무 시트에서 제외일 로드 → 컨트롤러 + 라벨 표시."""
        y = self._year
        m = self._month
        if not y or not m:
            return

        self._excluded_dates.clear()
        holidays = self._db_reader.get_holiday_set()
        month_excluded: list[date] = []
        for d in sorted(holidays):
            if d.year == y and d.month == m and d.weekday() < 5:
                self._excluded_dates.add(d)
                month_excluded.append(d)

        if month_excluded:
            parts = []
            for d in month_excluded:
                parts.append(
                    f"{d.month:02d}/{d.day:02d}"
                    f"({['월', '화', '수', '목', '금', '토', '일'][d.weekday()]})"
                )
            self.exclude_label.setText("제외일: " + ", ".join(parts))
            self.exclude_label.setStyleSheet("color: #c00; font-weight: bold;")
        else:
            self.exclude_label.setText("해당 월 배송휴무 없음")
            self.exclude_label.setStyleSheet("color: #888;")

    # ── 청구서 생성 ──

    def _generate_invoices(self):
        if not self.output_dir:
            QMessageBox.warning(self, "알림", "출력 경로를 먼저 선택하세요.")
            return

        if not self._year or not self._month:
            QMessageBox.warning(self, "알림", "db.xlsx를 먼저 로드하세요.")
            return

        self._save_settings()

        fname = self._s.get("invoice", "output_filename_format").format(
            year=self._year, month=self._month
        )
        out = os.path.join(self.output_dir, fname)

        try:
            roster = self._db_reader.read_roster()
        except Exception as e:
            QMessageBox.critical(self, "오류", f"db.xlsx 읽기 실패:\n{e}")
            return

        if not roster:
            QMessageBox.warning(
                self, "알림", "명단 데이터가 없습니다. 먼저 데이터를 로드하세요."
            )
            return

        config = InvoiceGenerationConfig(
            roster_data=roster,
            output_path=out,
            year=self._year,
            month=self._month,
            template_path=self._template_path,
            col_mapping=self._col_mapping,
            excluded_dates=self._excluded_dates,
            roster_headers=self.roster_headers,
            template_columns=self._template_columns,
            db_path=self._db_path,
            meal_type=self._meal_type,
        )
        result = self._service.generate(config)

        for w in result.warnings:
            QMessageBox.warning(self, w.code, w.message)

        if not result.success:
            if result.error:
                QMessageBox.critical(self, "오류", result.error)
            return

        self.invoices = result.enriched

        self._last_output_path = out
        self.open_file_btn.setEnabled(True)
        self._update_result(result.enriched)

        QMessageBox.information(
            self, "완료", f"청구서 {result.count}건 생성 완료!\n{out}"
        )

    def _update_result(self, data: list[dict]):
        show = data[: self.MAX_RESULT_ROWS]
        self.result_table.setRowCount(len(show))
        cm = dict(self._col_mapping)
        name_col = cm.get("성명", "성명")
        dept_col = cm.get("학교명", "학교명")
        for i, p in enumerate(show):
            self.result_table.setItem(i, 0, QTableWidgetItem(p.get(name_col, "")))
            self.result_table.setItem(i, 1, QTableWidgetItem(p.get(dept_col, "")))
            self.result_table.setItem(i, 2, QTableWidgetItem(str(p.get("_total", ""))))
            self.result_table.setItem(i, 3, QTableWidgetItem(p.get("_amount", "")))
        self.info_label.setText(
            f"생성된 청구서: {len(data)}건 (처음 {len(show)}건 표시)"
        )

    def _open_output_file(self):
        if self._last_output_path and os.path.exists(self._last_output_path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(self._last_output_path))
        else:
            QMessageBox.warning(
                self, "알림", "열 파일이 없습니다. 먼저 청구서를 생성하세요."
            )

    def _clear(self):
        self.invoices.clear()
        self.result_table.setRowCount(0)
        self.info_label.setText("명단을 불러오세요.")
        self._last_output_path = None
        self.open_file_btn.setEnabled(False)
        self._roster_loaded = False
        self.generate_btn.setEnabled(False)

    def _auto_map_columns(self):
        """템플릿 컬럼 ↔ 명단 컬럼 자동 매칭 (공백/개행 무시)."""
        if not self.roster_headers:
            return
        roster_normalized = {
            h.replace(" ", "").replace("\n", ""): h for h in self.roster_headers
        }
        for tcol in list(self._col_mapping.keys()):
            if tcol.startswith("__"):
                continue  # semantic keys are handled separately
            tnorm = tcol.replace(" ", "").replace("\n", "")
            if tnorm in roster_normalized:
                self._col_mapping[tcol] = roster_normalized[tnorm]

    # ── 외부 접근 ──

    def set_output_dir(self, path: str):
        """외부에서 출력 경로 설정 (탭 간 공유용)."""
        if path and path != self.output_dir:
            self.output_dir = path
            self.path_label.setText(path)
            self.path_label.setStyleSheet("color: #000;")
            self._save_settings()

    def set_meal_type(self, meal_type: str):
        """식사 유형 변경 → 템플릿 경로·컬럼 재로드."""
        if meal_type == self._meal_type:
            return
        self._meal_type = meal_type
        self._template_path = self._s.get("templates", "invoice_path")
        self._template_columns = read_template_columns(
            self._template_path, meal_type=self._meal_type
        )
        self._col_mapping = {col: "" for col in self._template_columns}
        _sem = auto_detect_semantic_columns(self._template_columns)
        self._col_mapping.update(_sem)
        self._auto_map_columns()

    def get_invoice_data(self):
        return InvoiceData(
            invoices=self.invoices,
            col_mapping=dict(self._col_mapping),
            year=self._year,
            month=self._month,
            excluded_dates=self._excluded_dates.copy(),
            roster_headers=self.roster_headers,
            roster_path=self.roster_path,
        )
