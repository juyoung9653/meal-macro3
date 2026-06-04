"""청구서 탭 — 명단 기반 실제 청구서 생성"""

from datetime import date

from PyQt6.QtCore import QSettings, Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
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

from ..config import DEFAULT_ROSTER_TO_TEMPLATE
from ..invoice_generator import make_invoice
from .mapping_dialog import MappingDialog


class InvoiceTab(QWidget):
    """청구서 생성 탭"""

    def __init__(self):
        super().__init__()
        self.invoices = []
        self.output_dir = None
        self._roster_data = []
        self._roster_headers = []
        self._col_mapping = dict(DEFAULT_ROSTER_TO_TEMPLATE)
        self._settings = QSettings("분당구", "급식비정산")
        self._setup_ui()
        self._restore_state()

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        # ── 안내 ──
        info_label = QLabel("명단 → 청구서 생성. 컬럼 매핑 후 생성 버튼을 누르세요.")
        info_label.setStyleSheet("color: #666; font-style: italic;")
        layout.addWidget(info_label)

        # ── 설정행: 연월 + 단가 ──
        config_group = QGroupBox("청구 기준")
        config_row = QHBoxLayout(config_group)

        config_row.addWidget(QLabel("귀속 연월:"))

        self.year_spin = QSpinBox()
        self.year_spin.setRange(2020, 2030)
        self.year_spin.setValue(date.today().year)
        config_row.addWidget(self.year_spin)

        config_row.addWidget(QLabel("년"))

        self.month_spin = QSpinBox()
        self.month_spin.setRange(1, 12)
        self.month_spin.setValue(date.today().month)
        config_row.addWidget(self.month_spin)

        config_row.addWidget(QLabel("월"))
        config_row.addSpacing(20)

        # 단가
        config_row.addWidget(QLabel("조식단가:"))
        self.breakfast_price = QSpinBox()
        self.breakfast_price.setRange(0, 99999)
        self.breakfast_price.setValue(4000)
        self.breakfast_price.setSuffix("원")
        config_row.addWidget(self.breakfast_price)

        config_row.addWidget(QLabel("석식단가:"))
        self.dinner_price = QSpinBox()
        self.dinner_price.setRange(0, 99999)
        self.dinner_price.setValue(2000)
        self.dinner_price.setSuffix("원")
        config_row.addWidget(self.dinner_price)

        config_row.addWidget(QLabel("중식단가:"))
        self.lunch_price = QSpinBox()
        self.lunch_price.setRange(0, 99999)
        self.lunch_price.setValue(4000)
        self.lunch_price.setSuffix("원")
        config_row.addWidget(self.lunch_price)

        config_row.addStretch()
        layout.addWidget(config_group)

        # ── 출력 경로 ──
        path_group = QGroupBox("출력 경로")
        path_layout = QHBoxLayout(path_group)
        self.path_label = QLabel("선택된 경로 없음")
        self.path_label.setStyleSheet("color: #888;")
        self.path_btn = QPushButton("폴더 선택")
        self.path_btn.clicked.connect(self._select_output_dir)
        path_layout.addWidget(self.path_label, stretch=1)
        path_layout.addWidget(self.path_btn)
        layout.addWidget(path_group)

        # ── 버튼 ──
        btn_layout = QHBoxLayout()
        self.mapping_btn = QPushButton("컬럼 매핑")
        self.mapping_btn.setEnabled(False)
        self.mapping_btn.clicked.connect(self._open_mapping)
        self.generate_btn = QPushButton("청구서 생성")
        self.generate_btn.setEnabled(False)
        self.generate_btn.clicked.connect(self._generate_invoices)
        self.clear_btn = QPushButton("초기화")
        self.clear_btn.clicked.connect(self._clear)
        btn_layout.addWidget(self.mapping_btn)
        btn_layout.addStretch()
        btn_layout.addWidget(self.generate_btn)
        btn_layout.addWidget(self.clear_btn)
        layout.addLayout(btn_layout)

        # 값 변경 시 자동 저장
        self.year_spin.valueChanged.connect(self._save_settings)
        self.month_spin.valueChanged.connect(self._save_settings)
        self.breakfast_price.valueChanged.connect(self._save_settings)
        self.dinner_price.valueChanged.connect(self._save_settings)
        self.lunch_price.valueChanged.connect(self._save_settings)

        # ── 결과 테이블 ──
        table_group = QGroupBox("생성 결과")
        table_layout = QVBoxLayout(table_group)
        self.result_table = QTableWidget()
        self.result_table.setColumnCount(5)
        self.result_table.setHorizontalHeaderLabels(
            ["번호", "이름", "소속", "총급식수", "청구금액"]
        )
        self.result_table.horizontalHeader().setStretchLastSection(True)
        self.result_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self.result_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.result_table.setAlternatingRowColors(True)
        table_layout.addWidget(self.result_table)
        self.info_label = QLabel("생성된 청구서: 0건")
        self.info_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        table_layout.addWidget(self.info_label)
        layout.addWidget(table_group, stretch=1)

    # ── 액션 ──

    def _restore_state(self):
        # 출력 경로
        out = self._settings.value("invoice/output_dir", "")
        if out:
            self.output_dir = out
            self.path_label.setText(out)
            self.path_label.setStyleSheet("color: #000;")

        # 연월
        y = self._settings.value("invoice/year", None)
        if y is not None:
            self.year_spin.setValue(int(y))
        m = self._settings.value("invoice/month", None)
        if m is not None:
            self.month_spin.setValue(int(m))

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

        # 컬럼 매핑
        mapping = self._settings.value("invoice/col_mapping", None)
        if mapping is not None:
            self._col_mapping.update(mapping)

    def _save_settings(self):
        self._settings.setValue("invoice/year", self.year_spin.value())
        self._settings.setValue("invoice/month", self.month_spin.value())
        self._settings.setValue("invoice/breakfast_price", self.breakfast_price.value())
        self._settings.setValue("invoice/dinner_price", self.dinner_price.value())
        self._settings.setValue("invoice/lunch_price", self.lunch_price.value())
        self._settings.setValue("invoice/col_mapping", self._col_mapping)
        if self.output_dir:
            self._settings.setValue("invoice/output_dir", self.output_dir)
        self._settings.sync()

    def _select_output_dir(self):
        d = QFileDialog.getExistingDirectory(self, "출력 폴더 선택")
        if d:
            self.output_dir = d
            self.path_label.setText(d)
            self.path_label.setStyleSheet("color: #000;")
            self._settings.setValue("invoice/output_dir", d)
            self._settings.sync()

    def _open_mapping(self):
        if not self._roster_headers:
            QMessageBox.warning(self, "알림", "먼저 명단 파일을 불러오세요.")
            return
        dlg = MappingDialog(self._roster_headers, self)
        dlg.set_values(self._col_mapping)
        if dlg.exec():
            self._col_mapping = dlg.get_values()
            self._save_settings()
            self.update()

    def _generate_invoices(self):
        if not self.output_dir:
            QMessageBox.warning(self, "알림", "출력 경로를 먼저 선택하세요.")
            return
        if not self._roster_data:
            QMessageBox.warning(self, "알림", "명단 데이터가 없습니다.")
            return

        # 매핑에서 플래그 컬럼명 가져오기
        breakfast_col = self._col_mapping.get("roster_breakfast_col", "")
        dinner_col = self._col_mapping.get("roster_dinner_col", "")
        vacation_col = self._col_mapping.get("roster_vacation_col", "")
        note_col = self._col_mapping.get("roster_note_col", "")

        # Y, Z열 (인덱스 24,25) — "중지" 포함 행 제외
        y_key = self._roster_headers[24] if len(self._roster_headers) > 24 else None
        z_key = self._roster_headers[25] if len(self._roster_headers) > 25 else None

        enriched = []
        for person in self._roster_data:
            val_y = str(person.get(y_key, "")) if y_key else ""
            val_z = str(person.get(z_key, "")) if z_key else ""
            if "중지" in val_y or "중지" in val_z:
                continue
            enriched.append(
                {
                    **person,
                    "_breakfast": person.get(breakfast_col, 0),
                    "_dinner": person.get(dinner_col, 0),
                    "_lunch": person.get(vacation_col, 0),
                    "_note": person.get(note_col, ""),
                }
            )

        year = self.year_spin.value()
        month = self.month_spin.value()
        self._save_settings()
        fname = f"청구서_{year:04d}{month:02d}.xlsx"
        out = f"{self.output_dir}/{fname}"

        try:
            cnt = make_invoice(
                enriched,
                self._col_mapping,
                out,
                template_path="청구서템플릿.xlsx",
                year=year,
                month=month,
                unit_price_breakfast=self.breakfast_price.value(),
                unit_price_dinner=self.dinner_price.value(),
                unit_price_lunch=self.lunch_price.value(),
                provider="분당구",
            )
        except Exception as e:
            QMessageBox.critical(self, "생성 오류", f"청구서 생성 실패:\n{e}")
            return

        # 결과 테이블
        self.invoices = enriched
        self._update_result(enriched, year, month)
        QMessageBox.information(self, "완료", f"청구서 {cnt}건 생성 완료!\n{out}")

    def _update_result(self, data: list[dict], year: int, month: int):
        from ..invoice_generator import _to_bool, calc_delivery_days

        self.result_table.setRowCount(len(data))
        for i, p in enumerate(data):
            b = _to_bool(p.get("_breakfast", 0))
            d = _to_bool(p.get("_dinner", 0))
            l = _to_bool(p.get("_lunch", 0))
            n = p.get("_note", "")
            days = calc_delivery_days(year, month, b, d, l, n)
            breakfast_cnt = days["breakfast"] * 3
            dinner_cnt = days["dinner"]
            lunch_cnt = days["lunch"]
            total = breakfast_cnt + dinner_cnt + lunch_cnt
            amount = (
                breakfast_cnt * self.breakfast_price.value()
                + dinner_cnt * self.dinner_price.value()
                + lunch_cnt * self.lunch_price.value()
            )
            name_col = self._col_mapping.get("roster_name_col", "성명")
            dept_col = self._col_mapping.get("roster_dept_col", "학교명")
            self.result_table.setItem(i, 0, QTableWidgetItem(str(i + 1)))
            self.result_table.setItem(i, 1, QTableWidgetItem(p.get(name_col, "")))
            self.result_table.setItem(i, 2, QTableWidgetItem(p.get(dept_col, "")))
            self.result_table.setItem(i, 3, QTableWidgetItem(str(total)))
            self.result_table.setItem(i, 4, QTableWidgetItem(f"{amount:,}원"))
        self.info_label.setText(f"생성된 청구서: {len(data)}건")

    def _clear(self):
        self.invoices.clear()
        self.result_table.setRowCount(0)
        self.info_label.setText("생성된 청구서: 0건")

    def set_roster_data(self, data: list[dict], headers: list[str]):
        self._roster_data = data
        self._roster_headers = headers
        has = bool(data)
        self.mapping_btn.setEnabled(has)
        self.generate_btn.setEnabled(has)
        if has:
            self.info_label.setText(f"명단 {len(data)}명 로드됨. 컬럼 매핑 → 생성")

    def get_invoices(self):
        return self.invoices
