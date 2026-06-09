"""청구서 탭 — 명단 파일 임포트 + 청구서 생성"""

import calendar
import csv
import io
import os
import re
from datetime import date

import msoffcrypto
from PyQt6.QtCore import QDate, QSettings, Qt
from PyQt6.QtWidgets import (
    QCalendarWidget,
    QDialog,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from python_calamine import CalamineWorkbook, PasswordError

from ..config import auto_detect_semantic_columns, read_template_columns
from ..delivery_calculator import _to_bool, count_delivery_days
from ..holiday_fetcher import get_kr_holidays
from ..invoice_generator import make_invoice
from ..settings import get_settings
from .mapping_dialog import MappingDialog


class InvoiceTab(QWidget):
    """청구서 생성 탭 (명단 임포트 포함)"""

    MAX_RESULT_ROWS = 10

    def __init__(self):
        super().__init__()
        self.invoices = []
        self.output_dir = None
        self.roster_path = None
        self.roster_data = []
        self.roster_headers = []
        self._s = get_settings()
        self._template_path = self._s.get(
            "templates", "invoice_path", default="assets/청구서템플릿.xlsx"
        )
        self._template_columns = read_template_columns(self._template_path)
        # 템플릿 컬럼 → 명단 컬럼 매핑
        self._col_mapping = {col: "" for col in self._template_columns}
        # 의미론적 컬럼 (auto-detect → GUI override 가능)
        _sem = auto_detect_semantic_columns(self._template_columns)
        self._col_mapping.update(_sem)
        # 배송 플래그
        self._col_mapping.update(
            {"__breakfast": "", "__dinner": "", "__vacation": "", "__note": ""}
        )
        self._excluded_dates: set[date] = set()
        org = self._s.get("app", "organization", default="분당구")
        app_title = self._s.get("app", "title", default="급식비정산")
        self._settings = QSettings(org, app_title)
        self._setup_ui()
        self._restore_state()
        self._load_default_excluded_dates()

    # ── UI ──

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        # ── 명단 파일 임포트 ──
        import_group = QGroupBox("① 명단 파일 임포트")
        import_layout = QHBoxLayout(import_group)
        self.file_label = QLabel("선택된 파일 없음")
        self.file_label.setStyleSheet("color: #888;")
        self.select_btn = QPushButton("파일 탐색")
        self.select_btn.clicked.connect(self._select_file)
        import_layout.addWidget(self.file_label, stretch=1)
        import_layout.addWidget(self.select_btn)
        layout.addWidget(import_group)

        # ── 청구 기준 ──
        config_group = QGroupBox("② 청구 기준")
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

        config_row.addWidget(QLabel("조식단가:"))
        self.breakfast_price = QSpinBox()
        self.breakfast_price.setRange(0, 99999)
        self.breakfast_price.setValue(self._s.get("pricing", "breakfast", default=9500))
        self.breakfast_price.setSuffix("원")
        config_row.addWidget(self.breakfast_price)

        config_row.addWidget(QLabel("석식단가:"))
        self.dinner_price = QSpinBox()
        self.dinner_price.setRange(0, 99999)
        self.dinner_price.setValue(self._s.get("pricing", "dinner", default=9500))
        self.dinner_price.setSuffix("원")
        config_row.addWidget(self.dinner_price)

        config_row.addWidget(QLabel("중식단가:"))
        self.lunch_price = QSpinBox()
        self.lunch_price.setRange(0, 99999)
        self.lunch_price.setValue(self._s.get("pricing", "lunch", default=9500))
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

        # ── 배송 제외일 ──
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
        btn_layout.addWidget(self.mapping_btn)
        btn_layout.addStretch()
        btn_layout.addWidget(self.generate_btn)
        btn_layout.addWidget(self.clear_btn)
        layout.addLayout(btn_layout)

        # 값 변경 시 자동 저장
        self.year_spin.valueChanged.connect(self._save_settings)
        self.month_spin.valueChanged.connect(self._save_settings)
        self.year_spin.valueChanged.connect(self._reload_excluded_dates)
        self.month_spin.valueChanged.connect(self._reload_excluded_dates)
        self.breakfast_price.valueChanged.connect(self._save_settings)
        self.dinner_price.valueChanged.connect(self._save_settings)
        self.lunch_price.valueChanged.connect(self._save_settings)

        # ── 결과 테이블 (최대 10행) ──
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
        self.info_label = QLabel("생성된 청구서: 0건")
        self.info_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        table_layout.addWidget(self.info_label)
        layout.addWidget(table_group, stretch=1)

    # ── 명단 파일 로드 (RosterTab에서 이동) ──

    def _select_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "명단 파일 선택",
            "",
            "지원 파일 (*.xlsx *.xls *.csv *.txt);;Excel (*.xlsx *.xls);;CSV (*.csv);;텍스트 (*.txt);;모든 파일 (*.*)",
        )
        if path:
            self.roster_path = path
            self.file_label.setText(os.path.basename(path))
            self.file_label.setStyleSheet("color: #000;")
            self._settings.setValue("roster/last_path", path)
            self._settings.sync()
            self._load_roster(path)

    def _restore_state(self):
        # 마지막 명단 경로 복원
        last = self._settings.value("roster/last_path", "")
        if last and os.path.exists(last):
            self.roster_path = last
            self.file_label.setText(os.path.basename(last))
            self.file_label.setStyleSheet("color: #000;")
            self._load_roster(last)

        # 출력 경로 복원
        out = self._settings.value("invoice/output_dir", "")
        if out:
            self.output_dir = out
            self.path_label.setText(out)
            self.path_label.setStyleSheet("color: #000;")

        # ── Signal block ──
        self.year_spin.blockSignals(True)
        self.month_spin.blockSignals(True)
        self.breakfast_price.blockSignals(True)
        self.dinner_price.blockSignals(True)
        self.lunch_price.blockSignals(True)

        # 컬럼 매핑
        mapping = self._settings.value("invoice/col_mapping", None)
        if mapping is not None:
            self._col_mapping = {
                k: mapping.get(k, v) for k, v in self._col_mapping.items()
            }

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

        self._save_settings()

    def _load_roster(self, path: str):
        ext = os.path.splitext(path)[1].lower()
        try:
            if ext in (".xlsx", ".xls", ".xlsb", ".ods"):
                raw = self._load_excel()
            elif ext == ".csv":
                raw = self._load_csv(path)
            elif ext == ".txt":
                raw = self._load_txt(path)
            else:
                QMessageBox.warning(self, "알림", f"지원하지 않는 형식: {ext}")
                return
        except Exception as e:
            QMessageBox.critical(self, "오류", f"파일 읽기 실패:\n{e}")
            return

        if not raw:
            QMessageBox.information(self, "알림", "데이터가 비어 있습니다.")
            return

        self.roster_headers, self.roster_data = raw
        has = bool(self.roster_data)
        self.mapping_btn.setEnabled(has)
        self.generate_btn.setEnabled(has)
        if has:
            self.info_label.setText(
                f"명단 {len(self.roster_data)}명 로드됨. 컬럼 매핑 → 생성"
            )

    def _load_excel(self):
        try:
            wb = CalamineWorkbook.from_path(self.roster_path)
        except PasswordError:
            pw = self._prompt_password()
            if pw is None:
                return None
            wb = self._open_encrypted(self.roster_path, pw)
            if wb is None:
                return None
        sheet = wb.get_sheet_by_index(0)
        rows = list(sheet.iter_rows())
        return self._parse_rows(rows)

    def _load_csv(self, path: str):
        with open(path, "r", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            rows = list(reader)
        return self._parse_rows(rows)

    def _load_txt(self, path: str):
        with open(path, "r", encoding="utf-8") as f:
            lines = [line.strip() for line in f if line.strip()]
        headers = ["이름"]
        data = [{"이름": line} for line in lines]
        return headers, data

    def _prompt_password(self) -> str | None:
        dlg = QInputDialog(self)
        dlg.setWindowTitle("암호 해제")
        dlg.setLabelText("이 파일은 암호로 보호되어 있습니다.\n암호를 입력하세요:")
        dlg.setTextEchoMode(QLineEdit.EchoMode.Password)
        dlg.resize(300, 120)
        return dlg.textValue().strip() if dlg.exec() else None

    def _open_encrypted(self, path: str, password: str):
        attempts = 0
        max_attempts = 10
        while attempts < max_attempts:
            try:
                with open(path, "rb") as f:
                    off = msoffcrypto.OfficeFile(f)
                    off.load_key(password=password)
                    buf = io.BytesIO()
                    off.decrypt(buf)
                buf.seek(0)
                return CalamineWorkbook.from_filelike(buf)
            except Exception as e:
                msg = str(e).lower()
                if "password" in msg or "key" in msg or "wrong" in msg:
                    QMessageBox.warning(self, "암호 오류", "암호가 올바르지 않습니다.")
                    pw = self._prompt_password()
                    if pw is None:
                        return None
                    password = pw
                    attempts += 1
                    continue
                raise
        QMessageBox.warning(self, "암호 오류", "최대 재시도 횟수를 초과했습니다.")
        return None

    @staticmethod
    def _col_letter(idx: int) -> str:
        """0-based column index → Excel letter (A, B, ..., Z, AA, ...)."""
        result = ""
        while True:
            result = chr(idx % 26 + 65) + result
            idx = idx // 26 - 1
            if idx < 0:
                break
        return result

    @staticmethod
    def _is_name(v: str) -> bool:
        return bool(
            v
            and v.strip()
            and not re.fullmatch(r"\d+(\.\d+)?", v.strip())
            and not re.fullmatch(r"\d{2}\.\d{2}(\.\d{2,4})?", v.strip())
            and len(v.strip()) > 1
        )

    def _parse_rows(self, rows: list) -> tuple:
        if not rows:
            return [], []

        raw_headers = [str(c) for c in rows[0]]
        headers = []
        seen = {}
        for h in raw_headers:
            h = h.strip()
            if not h:
                h = self._col_letter(len(headers))
            if h in seen:
                seen[h] += 1
                h = f"{h}_{seen[h]}"
            else:
                seen[h] = 1
            headers.append(h)

        name_idx = self._resolve_name_col(raw_headers)

        data = []
        for row in rows[1:]:
            vals = [str(c) for c in row]
            name_val = vals[name_idx] if name_idx < len(vals) else ""
            if not self._is_name(name_val):
                continue
            entry = {
                headers[i]: vals[i] if i < len(vals) else ""
                for i in range(len(headers))
            }
            data.append(entry)

        return headers, data

    @staticmethod
    def _resolve_name_col(header: list) -> int:
        keywords = get_settings().get(
            "roster",
            "name_keywords",
            default=["성명", "이름", "성함", "학생명", "학생", "name", "Name"],
        )
        for i, col in enumerate(header):
            c = str(col).strip()
            if any(c == kw or c.startswith(kw) for kw in keywords):
                return i
        return 0

    # ── 설정 저장/복원 ──

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

    def _select_output_dir(self):
        d = QFileDialog.getExistingDirectory(self, "출력 폴더 선택")
        if d:
            self.output_dir = d
            self.path_label.setText(d)
            self.path_label.setStyleSheet("color: #000;")
            self._settings.setValue("invoice/output_dir", d)
            self._settings.sync()

    def _open_mapping(self):
        if not self.roster_headers:
            QMessageBox.warning(self, "알림", "먼저 명단 파일을 불러오세요.")
            return
        dlg = MappingDialog(self.roster_headers, self._template_columns, self)
        dlg.set_values(self._col_mapping)
        if not any(
            v for k, v in self._col_mapping.items() if k in self._template_columns
        ):
            dlg._reset_defaults()
        if dlg.exec():
            self._col_mapping = dlg.get_values()
            # 계산값 입력 대상은 auto-detect → GUI 불필요
            _sem = auto_detect_semantic_columns(self._template_columns)
            self._col_mapping.update(_sem)
            self._save_settings()
            self.update()

    def _open_exclude_dialog(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("배송 제외일 선택")
        dlg.setMinimumWidth(400)
        layout = QVBoxLayout(dlg)

        y = self.year_spin.value()
        m = self.month_spin.value()

        temp_excluded = {d for d in self._excluded_dates if d.weekday() < 5}
        last_day = calendar.monthrange(y, m)[1]
        for day in range(1, last_day + 1):
            d = date(y, m, day)
            if d in get_kr_holidays(y) and d.weekday() < 5:
                temp_excluded.add(d)

        cal = QCalendarWidget()
        cal.setGridVisible(True)
        cal.setCurrentPage(y, m)

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
            if d_local.weekday() >= 5:
                return
            if d_local in temp_excluded:
                temp_excluded.discard(d_local)
            else:
                temp_excluded.add(d_local)
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

        self._exclude_list_label = QLabel(_fmt_local())
        self._exclude_list_label.setWordWrap(True)
        layout.addWidget(self._exclude_list_label)

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
        y = self.year_spin.value()
        m = self.month_spin.value()
        for day in range(1, calendar.monthrange(y, m)[1] + 1):
            d = date(y, m, day)
            if d in get_kr_holidays(y) and d.weekday() < 5:
                self._excluded_dates.add(d)
        self._update_exclude_label()

    def _reload_excluded_dates(self):
        self._excluded_dates.clear()
        self._load_default_excluded_dates()

    # ── 청구서 생성 ──

    def _generate_invoices(self):
        if not self.output_dir:
            QMessageBox.warning(self, "알림", "출력 경로를 먼저 선택하세요.")
            return
        if not self.roster_data:
            QMessageBox.warning(self, "알림", "명단 데이터가 없습니다.")
            return

        breakfast_col = self._col_mapping.get("__breakfast", "")
        dinner_col = self._col_mapping.get("__dinner", "")
        vacation_col = self._col_mapping.get("__vacation", "")
        note_col = self._col_mapping.get("__note", "")

        y_key = next(
            (h for h in self.roster_headers if "중지" in h or "중단" in h), None
        )

        enriched = []
        for person in self.roster_data:
            val_y = str(person.get(y_key, "")) if y_key else ""
            if "중지" in val_y or "중단" in val_y:
                continue
            note_val = str(person.get(note_col, ""))
            # 비고에 날짜 또는 매주패턴이 없으면 계산 대상 아님
            if not re.search(r"\d{1,2}[/.]\d{1,2}|매주", note_val):
                note_val = ""
            enriched.append(
                {
                    **person,
                    note_col: note_val,
                    "_breakfast": person.get(breakfast_col, 0),
                    "_dinner": person.get(dinner_col, 0),
                    "_lunch": person.get(vacation_col, 0),
                    "_note": note_val,
                }
            )

        year = self.year_spin.value()
        month = self.month_spin.value()
        self._save_settings()

        fname = self._s.get(
            "invoice",
            "output_filename_format",
            default="청구서_{year:04d}{month:02d}.xlsx",
        ).format(year=year, month=month)
        out = f"{self.output_dir}/{fname}"

        if not os.path.exists(self._template_path):
            QMessageBox.critical(
                self,
                "파일 없음",
                f"템플릿 파일을 찾을 수 없습니다:\n{self._template_path}",
            )
            return

        try:
            # 계산값 입력 대상은 항상 auto-detect (GUI 불필요)
            _sem = auto_detect_semantic_columns(self._template_columns)
            col_mapping = {**self._col_mapping, **_sem}

            cnt = make_invoice(
                enriched,
                col_mapping,
                out,
                template_path=self._template_path,
                year=year,
                month=month,
                unit_price_breakfast=self.breakfast_price.value(),
                unit_price_dinner=self.dinner_price.value(),
                unit_price_lunch=self.lunch_price.value(),
                excluded_dates=self._excluded_dates.copy()
                if self._excluded_dates
                else None,
            )
        except Exception as e:
            QMessageBox.critical(self, "생성 오류", f"청구서 생성 실패:\n{e}")
            return

        # 결과 표시용 계산
        for p in enriched:
            flags = {
                k: _to_bool(p.get(f"_{k}", 0)) for k in ("breakfast", "dinner", "lunch")
            }
            days = count_delivery_days(
                year,
                month,
                flags["breakfast"],
                flags["dinner"],
                flags["lunch"],
                p.get("_note", ""),
                excluded_dates=self._excluded_dates.copy()
                if self._excluded_dates
                else None,
            )
            b_cnt, d_cnt, l_cnt = days["breakfast"], days["dinner"], days["lunch"]
            p["_total"] = b_cnt + d_cnt + l_cnt
            p["_amount"] = (
                f"{b_cnt * self.breakfast_price.value() + d_cnt * self.dinner_price.value() + l_cnt * self.lunch_price.value():,}원"
            )

        self.invoices = enriched
        self._update_result(enriched)
        QMessageBox.information(self, "완료", f"청구서 {cnt}건 생성 완료!\n{out}")

    def _update_result(self, data: list[dict]):
        """결과 중 최대 10건만 직접 표시"""
        show = data[: self.MAX_RESULT_ROWS]
        self.result_table.setRowCount(len(show))
        name_col = self._col_mapping.get("성명", "성명")
        dept_col = self._col_mapping.get("학교명", "학교명")
        for i, p in enumerate(show):
            self.result_table.setItem(i, 0, QTableWidgetItem(p.get(name_col, "")))
            self.result_table.setItem(i, 1, QTableWidgetItem(p.get(dept_col, "")))
            self.result_table.setItem(i, 2, QTableWidgetItem(str(p.get("_total", ""))))
            self.result_table.setItem(i, 3, QTableWidgetItem(p.get("_amount", "")))
        self.info_label.setText(
            f"생성된 청구서: {len(data)}건 (처음 {len(show)}건 표시)"
        )

    def _clear(self):
        self.invoices.clear()
        self.result_table.setRowCount(0)
        self.info_label.setText("생성된 청구서: 0건")

    # ── 외부 접근 ──

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

    def get_roster_headers(self):
        return self.roster_headers

    def get_roster_path(self):
        return self.roster_path
