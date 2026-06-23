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
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core.settings import get_settings
from ..excel.db_reader import get_db_reader
from ..services.invoice_controller import InvoiceController
from ..templates.config import read_template_columns

log = logging.getLogger(__name__)


class InvoiceTab(QWidget):
    """청구서 생성 탭 — db.xlsx 기반, 외부 파일 로드 완전 제거."""

    MAX_RESULT_ROWS = 10

    def __init__(self):
        super().__init__()
        self._s = get_settings()
        self._template_path = self._s.get_invoice_template_path()
        self._template_columns = read_template_columns(self._template_path)
        self._controller = InvoiceController(self, self._template_columns)
        self._db_reader = get_db_reader(self._s.get_db_path())
        org = self._s.get_app_organization()
        app_title = self._s.get_app_title()
        self._settings = QSettings(org, app_title)
        self._last_output_path: str | None = None
        self._roster_loaded = False
        self._setup_ui()
        self._restore_ui_state()
        self._load_default_excluded_dates()

    # ── UI ──

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        # ── db.xlsx 명단 로드 ──
        import_group = QGroupBox("① db.xlsx 명단 불러오기")
        import_layout = QHBoxLayout(import_group)
        self.file_label = QLabel("db.xlsx 로드 대기 중")
        self.file_label.setStyleSheet("color: #888;")
        self.select_btn = QPushButton("db.xlsx 선택")
        self.select_btn.clicked.connect(self._select_db_file)
        self.load_btn = QPushButton("데이터 로드")
        self.load_btn.clicked.connect(self._load_db_data)
        import_layout.addWidget(self.file_label, stretch=1)
        import_layout.addWidget(self.select_btn)
        import_layout.addWidget(self.load_btn)
        layout.addWidget(import_group)

        # ── 청구 기준 ──
        config_group = QGroupBox("② 청구 기준 (엑셀에서 자동 감지)")
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

        config_row.addWidget(QLabel("조식단가:"))
        self.breakfast_price = QSpinBox()
        self.breakfast_price.setRange(0, 99999)
        self.breakfast_price.setValue(9500)
        self.breakfast_price.setSuffix("원")
        config_row.addWidget(self.breakfast_price)

        config_row.addWidget(QLabel("석식단가:"))
        self.dinner_price = QSpinBox()
        self.dinner_price.setRange(0, 99999)
        self.dinner_price.setValue(9500)
        self.dinner_price.setSuffix("원")
        config_row.addWidget(self.dinner_price)

        config_row.addWidget(QLabel("중식단가:"))
        self.lunch_price = QSpinBox()
        self.lunch_price.setRange(0, 99999)
        self.lunch_price.setValue(9500)
        self.lunch_price.setSuffix("원")
        config_row.addWidget(self.lunch_price)
        config_row.addStretch()
        layout.addWidget(config_group)

        # ── 출력 경로 ──
        path_group = QGroupBox("③ 출력 경로")
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

        # 값 변경 시 자동 저장
        self.breakfast_price.valueChanged.connect(self._save_settings)
        self.dinner_price.valueChanged.connect(self._save_settings)
        self.lunch_price.valueChanged.connect(self._save_settings)

        # ── 결과 테이블 ──
        table_group = QGroupBox("④ 생성 결과")
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
        self.info_label = QLabel("db.xlsx에서 데이터를 불러오세요.")
        self.info_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        table_layout.addWidget(self.info_label)
        layout.addWidget(table_group, stretch=1)

    # ── db.xlsx 파일 선택/로드 ──

    def _select_db_file(self):
        """db.xlsx 파일 경로 선택."""
        path, _ = QFileDialog.getOpenFileName(
            self,
            "db.xlsx 파일 선택",
            self._db_reader.db_path,
            "Excel (*.xlsx *.xls);;모든 파일 (*.*)",
        )
        if path:
            self._db_reader.db_path = path
            self._controller.set_db_path(path)
            fname = os.path.basename(path)
            self.file_label.setText(f"📁 {fname}")
            self.file_label.setStyleSheet("color: #000;")
            self._settings.setValue("db/last_path", path)
            self._settings.sync()
            # 설정에도 저장
            self._s.set("db", "path", value=path)
            from ..core.settings import save_settings

            save_settings()

    def _auto_detect_month(self):
        """db.xlsx 배송전체 시트에서 연월 자동 감지 → 라벨 + 컨트롤러 설정."""
        try:
            y, m = self._db_reader.detect_month()
            self._controller._year = y
            self._controller._month = m
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

    def _load_db_data(self):
        """db.xlsx에서 명단 데이터 로드."""
        try:
            roster = self._db_reader.read_roster()
            if not roster:
                QMessageBox.warning(
                    self,
                    "알림",
                    "db.xlsx에서 명단 데이터를 찾을 수 없습니다.\n'명단' 시트가 있는지 확인하세요.",
                )
                return

            # 헤더 추출
            headers = list(roster[0].keys()) if roster else []
            self._controller.set_roster_headers(headers)
            self._controller.set_roster_path(self._db_reader.db_path)

            self._roster_loaded = True
            self.generate_btn.setEnabled(True)
            self.file_label.setText(
                f"✅ {os.path.basename(self._db_reader.db_path)} ({len(roster)}명)"
            )
            self.file_label.setStyleSheet("color: #0a0; font-weight: bold;")
            self.info_label.setText(
                f"명단 {len(roster)}명 로드됨. 청구서 생성 준비 완료"
            )

            # 엑셀에서 연월 자동 감지
            self._auto_detect_month()

            self._save_settings()
        except FileNotFoundError:
            QMessageBox.warning(
                self,
                "오류",
                f"db.xlsx 파일을 찾을 수 없습니다.\n'{self._db_reader.db_path}' 경로를 확인하세요.",
            )
        except Exception as e:
            QMessageBox.critical(self, "오류", f"db.xlsx 로드 실패:\n{e}")

    # ── 설정 저장/복원 ──

    def _restore_ui_state(self):
        out = self._settings.value("invoice/output_dir", "")
        if out:
            self._controller.output_dir = out
            self.path_label.setText(out)
            self.path_label.setStyleSheet("color: #000;")

        self.breakfast_price.blockSignals(True)
        self.dinner_price.blockSignals(True)
        self.lunch_price.blockSignals(True)

        # 컬럼 매핑
        mapping = self._settings.value("invoice/col_mapping", None)
        if mapping is not None:
            self._controller.set_col_mapping(
                {
                    k: mapping.get(k, v)
                    for k, v in self._controller.get_col_mapping().items()
                }
            )

        # 단가
        bp = self._settings.value("invoice/breakfast_price", None)
        if bp is not None:
            self.breakfast_price.setValue(int(bp))
        dp = self._settings.value("invoice/dinner_price", None)
        if dp is not None:
            self.dinner_price.setValue(int(dp))
        lp = self._settings.value("invoice/lunch_price", None)
        if lp is not None:
            self.lunch_price.setValue(int(lp))

        self.breakfast_price.blockSignals(False)
        self.dinner_price.blockSignals(False)
        self.lunch_price.blockSignals(False)

        # db.xlsx 경로 복원
        db_path = self._settings.value("db/last_path", "")
        if db_path and db_path != self._db_reader.db_path:
            if os.path.exists(db_path):
                self._db_reader.db_path = db_path
                self._controller.set_db_path(db_path)
                fname = os.path.basename(db_path)
                self.file_label.setText(f"📁 {fname}")
                self.file_label.setStyleSheet("color: #000;")
            elif os.path.exists(self._s.get_db_path()):
                fname = os.path.basename(self._s.get_db_path())
                self.file_label.setText(f"📁 {fname}")
                self.file_label.setStyleSheet("color: #000;")

        # 캐시된 InvoiceData 복원
        cached = self._load_cached_invoice_data()
        if cached and cached.invoices:
            self._controller.invoices = cached.invoices
            self._controller.set_col_mapping(cached.col_mapping)
            self._controller._year = cached.year
            self._controller._month = cached.month
            self._controller.set_excluded_dates(cached.excluded_dates)
            self._controller.roster_headers = cached.roster_headers
            self._controller.roster_path = cached.roster_path
            self._roster_loaded = True
            self.generate_btn.setEnabled(True)
            self._update_result(cached.invoices)
            # 연월 라벨 복원
            if cached.year:
                self.year_label.setText(str(cached.year))
                self.year_label.setStyleSheet(
                    "font-weight: bold; font-size: 14px; color: #0a0;"
                )
            if cached.month:
                self.month_label.setText(str(cached.month))
                self.month_label.setStyleSheet(
                    "font-weight: bold; font-size: 14px; color: #0a0;"
                )
            self._load_default_excluded_dates()

    def _try_auto_load_roster(self):
        """저장된 db.xlsx 자동 로드."""
        db_path = self._settings.value("db/last_path", "")
        if not db_path:
            db_path = self._s.get_db_path()
        if db_path and os.path.exists(db_path):
            self._db_reader.db_path = db_path
            self._controller.set_db_path(db_path)
            fname = os.path.basename(db_path)
            self.file_label.setText(f"📁 {fname}")
            self.file_label.setStyleSheet("color: #000;")
            # 자동 로드
            try:
                roster = self._db_reader.read_roster()
                if roster:
                    headers = list(roster[0].keys()) if roster else []
                    self._controller.set_roster_headers(headers)
                    self._controller.set_roster_path(db_path)
                    self._roster_loaded = True
                    self.generate_btn.setEnabled(True)
                    self.file_label.setText(f"✅ {fname} ({len(roster)}명)")
                    self.file_label.setStyleSheet("color: #0a0; font-weight: bold;")
                    self.info_label.setText(
                        f"명단 {len(roster)}명 자동 로드됨. 청구서 생성 준비 완료"
                    )
            except Exception:
                pass

    def post_init_load(self):
        """생성 후 I/O 로드. MainWindow show() 후 호출."""
        self._try_auto_load_roster()

    def _save_settings(self):
        self._settings.setValue("invoice/breakfast_price", self.breakfast_price.value())
        self._settings.setValue("invoice/dinner_price", self.dinner_price.value())
        self._settings.setValue("invoice/lunch_price", self.lunch_price.value())
        self._settings.setValue(
            "invoice/col_mapping", self._controller.get_col_mapping()
        )
        if self._controller.output_dir:
            self._settings.setValue("invoice/output_dir", self._controller.output_dir)
        if self._db_reader.db_path:
            self._settings.setValue("db/last_path", self._db_reader.db_path)
        self._settings.sync()

    def _cache_invoice_data(self):
        import json

        data = self.get_invoice_data()
        cache_path = os.path.join(
            self._controller.output_dir, ".invoice_data_cache.json"
        )
        try:
            payload = {
                "invoices": data.invoices,
                "col_mapping": data.col_mapping,
                "year": data.year,
                "month": data.month,
                "excluded_dates": [d.isoformat() for d in data.excluded_dates],
                "roster_headers": data.roster_headers,
                "roster_path": data.roster_path,
            }
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def _load_cached_invoice_data(self):
        import json

        out_dir = self._settings.value("invoice/output_dir", "")
        if not out_dir:
            return None
        cache_path = os.path.join(out_dir, ".invoice_data_cache.json")
        if not os.path.exists(cache_path):
            return None
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            from ..core.invoice_data import InvoiceData

            return InvoiceData(
                invoices=payload.get("invoices", []),
                col_mapping=payload.get("col_mapping", {}),
                year=payload.get("year", 0),
                month=payload.get("month", 0),
                excluded_dates={
                    date.fromisoformat(d) for d in payload.get("excluded_dates", [])
                },
                roster_headers=payload.get("roster_headers", []),
                roster_path=payload.get("roster_path"),
            )
        except (OSError, json.JSONDecodeError, ValueError):
            return None

    def _select_output_dir(self):
        d = QFileDialog.getExistingDirectory(self, "출력 폴더 선택")
        if d:
            self._controller.output_dir = d
            self.path_label.setText(d)
            self.path_label.setStyleSheet("color: #000;")
            self._settings.setValue("invoice/output_dir", d)
            self._settings.sync()

    def _load_default_excluded_dates(self):
        """db.xlsx 배송휴무 시트에서 제외일 로드 → 컨트롤러 + 라벨 표시."""
        y = self._controller._year
        m = self._controller._month
        if not y or not m:
            return

        self._controller.clear_excluded_dates()
        holidays = self._db_reader.get_holiday_set()
        month_excluded: list[date] = []
        for d in sorted(holidays):
            if d.year == y and d.month == m and d.weekday() < 5:
                self._controller.add_excluded_date(d)
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
        if not self._controller.output_dir:
            QMessageBox.warning(self, "알림", "출력 경로를 먼저 선택하세요.")
            return

        year = self._controller._year
        month = self._controller._month
        if not year or not month:
            QMessageBox.warning(self, "알림", "db.xlsx를 먼저 로드하세요.")
            return

        self._save_settings()

        fname = self._s.get_invoice_output_filename_format().format(
            year=year, month=month
        )
        out = os.path.join(self._controller.output_dir, fname)

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

        success, cnt, enriched = self._controller.generate(
            roster_data=roster,
            output_path=out,
            year=year,
            month=month,
            breakfast_price=self.breakfast_price.value(),
            dinner_price=self.dinner_price.value(),
            lunch_price=self.lunch_price.value(),
            template_path=self._template_path,
        )
        if not success:
            return

        self._last_output_path = out
        self.open_file_btn.setEnabled(True)
        self._update_result(enriched)

        self._cache_invoice_data()

        QMessageBox.information(self, "완료", f"청구서 {cnt}건 생성 완료!\n{out}")

    def _update_result(self, data: list[dict]):
        show = data[: self.MAX_RESULT_ROWS]
        self.result_table.setRowCount(len(show))
        cm = self._controller.get_col_mapping()
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
        self._controller.invoices.clear()
        self.result_table.setRowCount(0)
        self.info_label.setText("db.xlsx에서 데이터를 불러오세요.")
        self._last_output_path = None
        self.open_file_btn.setEnabled(False)
        self._roster_loaded = False
        self.generate_btn.setEnabled(False)

    # ── 외부 접근 ──

    def get_invoices(self):
        return self._controller.get_invoices()

    def get_col_mapping(self):
        return self._controller.get_col_mapping()

    def get_year(self):
        return self._controller._year

    def get_month(self):
        return self._controller._month

    def get_excluded_dates(self) -> set[date]:
        return self._controller.get_excluded_dates()

    def get_roster_headers(self):
        return self._controller.get_roster_headers()

    def get_roster_path(self):
        return self._controller.get_roster_path()

    def get_invoice_data(self):
        from ..core.invoice_data import InvoiceData

        return InvoiceData(
            invoices=self.get_invoices(),
            col_mapping=self.get_col_mapping(),
            year=self.get_year(),
            month=self.get_month(),
            excluded_dates=self.get_excluded_dates(),
            roster_headers=self.get_roster_headers(),
            roster_path=self.get_roster_path(),
        )
