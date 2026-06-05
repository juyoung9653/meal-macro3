"""청구서 탭 — 명단 기반 실제 청구서 생성"""

import calendar
import os
import re
from datetime import date

from PyQt6.QtCore import QDate, QSettings, Qt
from PyQt6.QtWidgets import (
    QCalendarWidget,
    QCheckBox,
    QComboBox,
    QDialog,
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
from ..form_cache import load_form_cache, save_form_cache
from ..holiday_fetcher import get_kr_holidays
from ..invoice_generator import _to_bool, calc_delivery_days, make_invoice
from .mapping_dialog import MappingDialog


class InvoiceTab(QWidget):
    """청구서 생성 탭"""

    def __init__(self):
        super().__init__()
        self.invoices = []
        self.output_dir = None
        self._roster_data = []
        self._roster_headers = []
        self._roster_path = None
        self._col_mapping = dict(DEFAULT_ROSTER_TO_TEMPLATE)
        self._excluded_dates: set[date] = set()
        self._per_file_mode = True  # default: per-file cache on
        self._settings = QSettings("분당구", "급식비정산")
        self._setup_ui()
        self._restore_state()
        self._load_default_excluded_dates()

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

        # ── 배송 제외일 버튼 (달력) ──
        exclude_group = QGroupBox("배송 제외일")
        exclude_layout = QHBoxLayout(exclude_group)
        self.exclude_btn = QPushButton("📅 제외일 선택")
        self.exclude_btn.clicked.connect(self._open_exclude_dialog)
        self.exclude_label = QLabel("선택된 제외일 없음")
        self.exclude_label.setStyleSheet("color: #888;")
        exclude_layout.addWidget(self.exclude_btn)
        exclude_layout.addWidget(self.exclude_label, stretch=1)
        layout.addWidget(exclude_group)

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
        self.per_file_cb = QCheckBox("파일별 설정 저장")
        self.per_file_cb.setChecked(self._per_file_mode)
        self.per_file_cb.setToolTip(
            "켜기: 명단 파일별로 설정 저장/복원\n끄기: 전역 기본값 사용"
        )
        self.per_file_cb.toggled.connect(self._on_per_file_toggled)
        btn_layout.addWidget(self.mapping_btn)
        btn_layout.addWidget(self.per_file_cb)
        btn_layout.addStretch()
        btn_layout.addWidget(self.generate_btn)
        btn_layout.addWidget(self.clear_btn)
        layout.addLayout(btn_layout)

        # 값 변경 시 자동 저장 (전역 only) + 제외일 재계산
        self.year_spin.valueChanged.connect(self._save_settings)
        self.month_spin.valueChanged.connect(self._save_settings)
        self.year_spin.valueChanged.connect(self._reload_excluded_dates)
        self.month_spin.valueChanged.connect(self._reload_excluded_dates)
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
        # per-file mode
        self._per_file_mode = self._settings.value(
            "invoice/per_file_mode", True, type=bool
        )
        self.per_file_cb.setChecked(self._per_file_mode)

        # 출력 경로
        out = self._settings.value("invoice/output_dir", "")
        if out:
            self.output_dir = out
            self.path_label.setText(out)
            self.path_label.setStyleSheet("color: #000;")

        # ── Signal block: 모든 값을 한 번에 복원 후 마지막에 저장 ──
        self.year_spin.blockSignals(True)
        self.month_spin.blockSignals(True)
        self.breakfast_price.blockSignals(True)
        self.dinner_price.blockSignals(True)
        self.lunch_price.blockSignals(True)

        # 컬럼 매핑
        mapping = self._settings.value("invoice/col_mapping", None)
        if mapping is not None:
            self._col_mapping.update(mapping)

        # 제외일
        excluded = self._settings.value("invoice/excluded_dates", None)
        if excluded is not None and isinstance(excluded, list):
            self._excluded_dates = {date.fromisoformat(s) for s in excluded}
            self._update_exclude_label()

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

        self.year_spin.blockSignals(False)
        self.month_spin.blockSignals(False)
        self.breakfast_price.blockSignals(False)
        self.dinner_price.blockSignals(False)
        self.lunch_price.blockSignals(False)

        # 모든 값 복원 후 한 번에 저장
        self._save_settings()

    def _save_settings(self):
        self._settings.setValue("invoice/year", self.year_spin.value())
        self._settings.setValue("invoice/month", self.month_spin.value())
        self._settings.setValue("invoice/breakfast_price", self.breakfast_price.value())
        self._settings.setValue("invoice/dinner_price", self.dinner_price.value())
        self._settings.setValue("invoice/lunch_price", self.lunch_price.value())
        self._settings.setValue("invoice/col_mapping", self._col_mapping)
        self._settings.setValue(
            "invoice/excluded_dates", [d.isoformat() for d in self._excluded_dates]
        )
        if self.output_dir:
            self._settings.setValue("invoice/output_dir", self.output_dir)
        self._settings.sync()

        # ── Per-file cache: also persist to the roster-level cache ──
        if self._per_file_mode and self._roster_path:
            state = self._collect_form_state()
            save_form_cache(self._roster_path, state, "invoice")

    def _on_per_file_toggled(self, checked: bool):
        self._per_file_mode = checked
        self._settings.setValue("invoice/per_file_mode", checked)
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

    def _open_exclude_dialog(self):
        """배송 제외일 선택 다이얼로그 — 공휴일 기본 제외"""
        dlg = QDialog(self)
        dlg.setWindowTitle("배송 제외일 선택")
        dlg.setMinimumWidth(400)
        layout = QVBoxLayout(dlg)

        y = self.year_spin.value()
        m = self.month_spin.value()

        # 해당 월 공휴일을 임시 set에 자동 추가 (다이얼로그 수락 시에만 반영)
        # 토/일은 제외일에 포함하지 않음
        temp_excluded = {d for d in self._excluded_dates if d.weekday() < 5}
        last_day = calendar.monthrange(y, m)[1]
        for day in range(1, last_day + 1):
            d = date(y, m, day)
            if d in get_kr_holidays(y) and d.weekday() < 5:  # 토/일 제외
                temp_excluded.add(d)

        cal = QCalendarWidget()
        cal.setGridVisible(True)
        cal.setCurrentPage(y, m)

        # 제외일 하이라이트 — setDateTextFormat 직접 사용
        from PyQt6.QtGui import QTextCharFormat

        for d in temp_excluded:
            qd = QDate(d.year, d.month, d.day)
            f = QTextCharFormat()
            f.setBackground(Qt.GlobalColor.red)
            f.setForeground(Qt.GlobalColor.white)
            cal.setDateTextFormat(qd, f)

        def _fmt_local() -> str:
            if not temp_excluded:
                return "선택된 제외일 없음"
            parts = []
            for d in sorted(temp_excluded):
                parts.append(
                    f"{d.month:02d}/{d.day:02d} ({['월', '화', '수', '목', '금', '토', '일'][d.weekday()]})"
                )
            return "제외일: " + ", ".join(parts)

        def _toggle_local(qd):
            d_local = date(qd.year(), qd.month(), qd.day())
            if d_local.weekday() >= 5:  # 토/일 제외
                return
            if d_local in temp_excluded:
                temp_excluded.discard(d_local)
            else:
                temp_excluded.add(d_local)
            # 달력 전체 포맷 초기화 후 다시 칠함
            for day in range(1, last_day + 1):
                cal.setDateTextFormat(QDate(y, m, day), QTextCharFormat())
            for ed in temp_excluded:
                eqd = QDate(ed.year, ed.month, ed.day)
                f = QTextCharFormat()
                f.setBackground(Qt.GlobalColor.red)
                f.setForeground(Qt.GlobalColor.white)
                cal.setDateTextFormat(eqd, f)
            self._exclude_list_label.setText(_fmt_local())

        def _clear_local():
            temp_excluded.clear()
            for day in range(1, last_day + 1):
                cal.setDateTextFormat(QDate(y, m, day), QTextCharFormat())
            self._exclude_list_label.setText("선택된 제외일 없음")

        cal.clicked.connect(_toggle_local)
        layout.addWidget(cal)

        # 선택된 날짜 목록 표시
        self._exclude_list_label = QLabel(_fmt_local())
        self._exclude_list_label.setWordWrap(True)
        layout.addWidget(self._exclude_list_label)

        # 버튼
        btn_layout = QHBoxLayout()
        clear_exclude_btn = QPushButton("전체 해제")
        clear_exclude_btn.clicked.connect(_clear_local)
        close_btn = QPushButton("닫기")
        close_btn.clicked.connect(dlg.accept)
        btn_layout.addWidget(clear_exclude_btn)
        btn_layout.addStretch()
        btn_layout.addWidget(close_btn)
        layout.addLayout(btn_layout)

        dlg.exec()
        self._excluded_dates = temp_excluded
        self._update_exclude_label()
        self._save_settings()

    def _update_exclude_label(self):
        if not self._excluded_dates:
            self.exclude_label.setText("선택된 제외일 없음")
            self.exclude_label.setStyleSheet("color: #888;")
        else:
            parts = []
            for d in sorted(self._excluded_dates):
                parts.append(
                    f"{d.month:02d}/{d.day:02d} ({['월', '화', '수', '목', '금', '토', '일'][d.weekday()]})"
                )
            self.exclude_label.setText("제외일: " + ", ".join(parts))
            self.exclude_label.setStyleSheet("color: #c00; font-weight: bold;")

    def _load_default_excluded_dates(self):
        """현재 연월 공휴일을 제외일에 자동 추가 (토/일 제외)"""
        y = self.year_spin.value()
        m = self.month_spin.value()
        for day in range(1, calendar.monthrange(y, m)[1] + 1):
            d = date(y, m, day)
            if d in get_kr_holidays(y) and d.weekday() < 5:
                self._excluded_dates.add(d)
        self._update_exclude_label()

    def _reload_excluded_dates(self):
        """연/월 변경 시 제외일 재계산"""
        self._excluded_dates.clear()
        self._load_default_excluded_dates()

    def _collect_form_state(self) -> dict:
        """Collect all GUI values into a serializable dict."""
        return {
            "year": self.year_spin.value(),
            "month": self.month_spin.value(),
            "breakfast_price": self.breakfast_price.value(),
            "dinner_price": self.dinner_price.value(),
            "lunch_price": self.lunch_price.value(),
            "output_dir": self.output_dir or "",
            "col_mapping": dict(self._col_mapping),
            "excluded_dates": [d.isoformat() for d in sorted(self._excluded_dates)],
        }

    def _apply_form_state(self, state: dict):
        """Restore GUI values from a cached state dict."""
        if not state:
            return False
        try:
            if "year" in state:
                self.year_spin.blockSignals(True)
                self.year_spin.setValue(int(state["year"]))
                self.year_spin.blockSignals(False)
            if "month" in state:
                self.month_spin.blockSignals(True)
                self.month_spin.setValue(int(state["month"]))
                self.month_spin.blockSignals(False)
            if "breakfast_price" in state:
                self.breakfast_price.blockSignals(True)
                self.breakfast_price.setValue(int(state["breakfast_price"]))
                self.breakfast_price.blockSignals(False)
            if "dinner_price" in state:
                self.dinner_price.blockSignals(True)
                self.dinner_price.setValue(int(state["dinner_price"]))
                self.dinner_price.blockSignals(False)
            if "lunch_price" in state:
                self.lunch_price.blockSignals(True)
                self.lunch_price.setValue(int(state["lunch_price"]))
                self.lunch_price.blockSignals(False)
            if "output_dir" in state and state["output_dir"]:
                self.output_dir = state["output_dir"]
                self.path_label.setText(state["output_dir"])
                self.path_label.setStyleSheet("color: #000;")
            if "col_mapping" in state and isinstance(state["col_mapping"], dict):
                self._col_mapping.update(state["col_mapping"])
            if "excluded_dates" in state and isinstance(state["excluded_dates"], list):
                dates = set()
                for s in state["excluded_dates"]:
                    try:
                        dates.add(date.fromisoformat(s))
                    except (ValueError, TypeError):
                        pass
                self._excluded_dates = dates
                self._update_exclude_label()
            # 모든 값 restore 후 한 번에 저장 (signal cascade로 인한 부분저장 방지)
            self._save_settings()
            return True
        except (ValueError, TypeError, AttributeError):
            return False

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
        y_key = next(
            (h for h in self._roster_headers if "중지" in h or "중단" in h), None
        )

        enriched = []
        for person in self._roster_data:
            val_y = str(person.get(y_key, "")) if y_key else ""
            if "중지" in val_y or "중단" in val_y:
                continue
            note_val = str(person.get(note_col, ""))
            # 날짜 패턴 없는 비고는 비고를 비움 (행은 유지)
            if not re.search(r"\d{1,2}[/.]\d{1,2}", note_val):
                note_val = ""
            enriched.append(
                {
                    **person,
                    note_col: note_val,  # 원본 키 덮어써서 _map_person_to_row() 반영
                    "_breakfast": person.get(breakfast_col, 0),
                    "_dinner": person.get(dinner_col, 0),
                    "_lunch": person.get(vacation_col, 0),
                    "_note": note_val,
                }
            )

        year = self.year_spin.value()
        month = self.month_spin.value()
        self._save_settings()

        # ── Per-file cache: save all GUI state on generate ──
        if self._per_file_mode and self._roster_path:
            state = self._collect_form_state()
            save_form_cache(self._roster_path, state, "invoice")

        fname = f"청구서_{year:04d}{month:02d}.xlsx"
        out = f"{self.output_dir}/{fname}"

        # 템플릿 파일 존재 여부 확인
        template_path = "assets/청구서템플릿.xlsx"
        if not os.path.exists(template_path):
            QMessageBox.critical(
                self, "파일 없음", f"템플릿 파일을 찾을 수 없습니다:\n{template_path}"
            )
            return

        try:
            cnt = make_invoice(
                enriched,
                self._col_mapping,
                out,
                template_path="assets/청구서템플릿.xlsx",
                year=year,
                month=month,
                unit_price_breakfast=self.breakfast_price.value(),
                unit_price_dinner=self.dinner_price.value(),
                unit_price_lunch=self.lunch_price.value(),
                provider="분당구",
                excluded_dates=self._excluded_dates.copy()
                if self._excluded_dates
                else None,
            )
        except Exception as e:
            QMessageBox.critical(self, "생성 오류", f"청구서 생성 실패:\n{e}")
            return

        # 결과 테이블
        self.invoices = enriched
        self._update_result(enriched, year, month)
        QMessageBox.information(self, "완료", f"청구서 {cnt}건 생성 완료!\n{out}")

    def _update_result(self, data: list[dict], year: int, month: int):

        self.result_table.setRowCount(len(data))
        for i, p in enumerate(data):
            b = _to_bool(p.get("_breakfast", 0))
            d = _to_bool(p.get("_dinner", 0))
            l = _to_bool(p.get("_lunch", 0))
            n = p.get("_note", "")
            days = calc_delivery_days(
                year,
                month,
                b,
                d,
                l,
                n,
                excluded_dates=self._excluded_dates.copy()
                if self._excluded_dates
                else None,
            )
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

    def set_roster_data(
        self, data: list[dict], headers: list[str], roster_path: str | None = None
    ):
        self._roster_data = data
        self._roster_headers = headers
        self._roster_path = roster_path
        has = bool(data)
        self.mapping_btn.setEnabled(has)
        self.generate_btn.setEnabled(has)

        # ── Per-file cache auto-restore ──
        if has and self._per_file_mode and roster_path:
            cached = load_form_cache(roster_path, "invoice")
            if cached:
                self._apply_form_state(cached)

        if has:
            self.info_label.setText(f"명단 {len(data)}명 로드됨. 컬럼 매핑 → 생성")

    def get_invoices(self):
        return self.invoices

    def get_col_mapping(self):
        return dict(self._col_mapping)

    def get_year(self):
        return self.year_spin.value()

    def get_month(self):
        return self.month_spin.value()

    def get_excluded_dates(self) -> set[date]:
        return self._excluded_dates.copy()
