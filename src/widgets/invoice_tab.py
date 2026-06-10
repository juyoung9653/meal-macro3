"""청구서 탭 — UI 셸. 파일 로드 + 생성 로직은 RosterLoader/InvoiceController 위임."""

import calendar
import logging
import os
from datetime import date

from PyQt6.QtCore import QDate, QSettings, Qt
from PyQt6.QtGui import QTextCharFormat
from PyQt6.QtWidgets import (
    QCalendarWidget,
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

from ..core.settings import get_settings
from ..services.holiday_fetcher import get_kr_holidays
from ..services.invoice_controller import InvoiceController
from ..services.lunch_data import LunchRoster, LunchSchoolEntry, parse_lunch_boolean
from ..services.lunch_parser import parse_lunch_note
from ..services.roster_loader import RosterLoader
from ..templates.config import auto_detect_semantic_columns, read_template_columns
from .lunch_mapping_dialog import LunchMappingDialog
from .mapping_dialog import MappingDialog

log = logging.getLogger(__name__)


class InvoiceTab(QWidget):
    """청구서 생성 탭 (명단 임포트 포함) — UI 전용, 로직은 Controller/Loader 위임."""

    MAX_RESULT_ROWS = 10

    def __init__(self):
        super().__init__()
        self._s = get_settings()
        self._template_path = self._s.get(
            "templates", "invoice_path", default="assets/청구서템플릿.xlsx"
        )
        self._template_columns = read_template_columns(self._template_path)
        self._controller = InvoiceController(self, self._template_columns)
        self._loader = RosterLoader(self)
        org = self._s.get("app", "organization", default="분당구")
        app_title = self._s.get("app", "title", default="급식비정산")
        self._settings = QSettings(org, app_title)
        self._lunch_enabled = False
        self._lunch_roster: LunchRoster | None = None
        self._lunch_mapping: dict = {}
        self._lunch_file_path: str = ""
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

        # ── 중식 명단 파일 임포트 ──
        lunch_group = QGroupBox("중식 명단 파일 임포트 (선택)")
        lunch_layout = QHBoxLayout(lunch_group)
        self.lunch_file_label = QLabel("불러오지 않음")
        self.lunch_file_label.setStyleSheet("color: #888;")
        self.lunch_file_btn = QPushButton("중식 파일 탐색")
        self.lunch_file_btn.clicked.connect(self._select_lunch_file)
        self.lunch_mapping_btn = QPushButton("중식 컬럼 매핑")
        self.lunch_mapping_btn.setEnabled(False)
        self.lunch_mapping_btn.clicked.connect(self._open_lunch_mapping)
        lunch_layout.addWidget(self.lunch_file_label, stretch=1)
        lunch_layout.addWidget(self.lunch_file_btn)
        lunch_layout.addWidget(self.lunch_mapping_btn)
        layout.addWidget(lunch_group)

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

    # ── 명단 파일 로드 ──

    def _select_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "명단 파일 선택",
            "",
            "지원 파일 (*.xlsx *.xls *.csv *.txt);;Excel (*.xlsx *.xls);;CSV (*.csv);;텍스트 (*.txt);;모든 파일 (*.*)",
        )
        if path:
            result = self._loader.load(path)
            if result:
                headers, data = result
                self._controller.roster_headers = headers
                self._controller.roster_path = path
                self.file_label.setText(os.path.basename(path))
                self.file_label.setStyleSheet("color: #000;")
                self._settings.setValue("roster/last_path", path)
                self._settings.sync()
                has = bool(data)
                self.mapping_btn.setEnabled(has)
                self.generate_btn.setEnabled(has)
                if has:
                    self.info_label.setText(
                        f"명단 {len(data)}명 로드됨. 컬럼 매핑 → 생성"
                    )

    # ── 중식 파일 로드 ──

    def _select_lunch_file(self):
        """중식 명단 파일 로드. 로드 후 컬럼 매핑 버튼 활성화."""
        path, _ = QFileDialog.getOpenFileName(
            self,
            "중식 명단 파일 선택",
            "",
            "지원 파일 (*.xlsx *.xls *.csv *.txt);;Excel (*.xlsx *.xls);;CSV (*.csv);;텍스트 (*.txt);;모든 파일 (*.*)",
        )
        if path:
            self.lunch_file_label.setText(os.path.basename(path))
            self.lunch_file_label.setStyleSheet("color: #000;")
            self._lunch_file_path = path
            self._lunch_enabled = True
            self.lunch_mapping_btn.setEnabled(True)
            # 저장된 매핑 있으면 자동 로드, 없으면 매핑 버튼 눌러야 함
            saved_mapping = self._settings.value("lunch/col_mapping", None)
            if saved_mapping and isinstance(saved_mapping, dict):
                self._lunch_mapping = saved_mapping
                self._reload_lunch_roster()

    def _open_lunch_mapping(self):
        """중식 명단 컬럼 매핑 다이얼로그 열기."""
        if not self._lunch_file_path:
            QMessageBox.warning(self, "알림", "먼저 중식 명단 파일을 불러오세요.")
            return

        # 중식 명단 전용 로딩 (RosterLoader는 이름 필터가 있어서 적합하지 않음)
        lunch_headers, lunch_data = _load_lunch_file(self._lunch_file_path)

        roster_headers = self._controller.roster_headers or []
        dlg = LunchMappingDialog(lunch_headers, roster_headers, self)

        saved = self._settings.value("lunch/col_mapping", None)
        if saved and isinstance(saved, dict):
            dlg.set_values(saved)
        else:
            dlg._reset_defaults()

        if not dlg.exec():
            return

        self._lunch_mapping = dlg.get_values()

        lunch_school_col = self._lunch_mapping.get("lunch_school_name", "")
        lunch_vac_date_col = self._lunch_mapping.get("lunch_vacation_date", "")
        lunch_open_date_col = self._lunch_mapping.get("lunch_opening_date", "")
        lunch_vac_meal_col = self._lunch_mapping.get("lunch_vacation_meal", "")
        lunch_open_meal_col = self._lunch_mapping.get("lunch_opening_meal", "")
        lunch_note_col = self._lunch_mapping.get("lunch_note", "")

        roster = LunchRoster(headers=lunch_headers, file_path=self._lunch_file_path)

        for row in lunch_data:
            school_name = str(row.get(lunch_school_col, "")).strip()
            if not school_name or school_name in ("학교명", "구분"):
                continue

            vac_date_str = row.get(lunch_vac_date_col, "")
            open_date_str = row.get(lunch_open_date_col, "")

            vac_date = _parse_lunch_date(vac_date_str)
            open_date = _parse_lunch_date(open_date_str)

            vac_meal = parse_lunch_boolean(row.get(lunch_vac_meal_col, ""))
            open_meal = parse_lunch_boolean(row.get(lunch_open_meal_col, ""))

            note_raw = str(row.get(lunch_note_col, "")).strip()
            if note_raw in (" ", "비고", ""):
                note_raw = ""

            type_from_first = str(
                row.get(lunch_headers[0] if lunch_headers else "", "")
            ).strip()

            entry = LunchSchoolEntry(
                school_name=school_name,
                school_type=type_from_first,
                vacation_date=vac_date,
                opening_date=open_date,
                vacation_meal=vac_meal,
                opening_meal=open_meal,
                note_raw=note_raw,
            )

            ref_year = self.year_spin.value()
            parse_lunch_note(entry, ref_year)
            roster.entries.append(entry)

        self._lunch_roster = roster
        self._controller.set_lunch_roster(
            roster, self._lunch_mapping, self._lunch_file_path
        )

        self._settings.setValue("lunch/col_mapping", self._lunch_mapping)
        self._settings.setValue("lunch/file_path", self._lunch_file_path)
        self._settings.sync()

        log.info(
            "[Lunch] 중식 명단 %d개 학교 로드 완료 (파일: %s)",
            len(roster.entries),
            os.path.basename(self._lunch_file_path),
        )
        QMessageBox.information(
            self,
            "로드 완료",
            f"중식 명단 {len(roster.entries)}개 학교 정보를 불러왔습니다.",
        )

    def _reload_lunch_roster(self):
        """저장된 매핑으로 중식명단 재로드 (프로그램 재시작 시 자동 복원)."""
        if not self._lunch_file_path:
            log.debug("[Lunch] 재로드 건너뜀: lunch_file_path 없음")
            return
        if not os.path.exists(self._lunch_file_path):
            log.warning("[Lunch] 재로드 실패: 파일 없음 — %s", self._lunch_file_path)
            return
        if not self._lunch_mapping:
            log.debug("[Lunch] 재로드 건너뜀: lunch_mapping 없음")
            return

        lunch_headers, lunch_data = _load_lunch_file(self._lunch_file_path)
        if not lunch_headers:
            log.warning("[Lunch] 재로드 실패: 헤더 없음")
            return

        lunch_school_col = self._lunch_mapping.get("lunch_school_name", "")
        lunch_vac_date_col = self._lunch_mapping.get("lunch_vacation_date", "")
        lunch_open_date_col = self._lunch_mapping.get("lunch_opening_date", "")
        lunch_vac_meal_col = self._lunch_mapping.get("lunch_vacation_meal", "")
        lunch_open_meal_col = self._lunch_mapping.get("lunch_opening_meal", "")
        lunch_note_col = self._lunch_mapping.get("lunch_note", "")

        roster = LunchRoster(headers=lunch_headers, file_path=self._lunch_file_path)

        for row in lunch_data:
            school_name = str(row.get(lunch_school_col, "")).strip()
            if not school_name or school_name in ("학교명", "구분"):
                continue

            vac_date_str = row.get(lunch_vac_date_col, "")
            open_date_str = row.get(lunch_open_date_col, "")

            vac_date = _parse_lunch_date(vac_date_str)
            open_date = _parse_lunch_date(open_date_str)

            vac_meal = parse_lunch_boolean(row.get(lunch_vac_meal_col, ""))
            open_meal = parse_lunch_boolean(row.get(lunch_open_meal_col, ""))

            note_raw = str(row.get(lunch_note_col, "")).strip()
            if note_raw in (" ", "비고", ""):
                note_raw = ""

            type_from_first = str(
                row.get(lunch_headers[0] if lunch_headers else "", "")
            ).strip()

            entry = LunchSchoolEntry(
                school_name=school_name,
                school_type=type_from_first,
                vacation_date=vac_date,
                opening_date=open_date,
                vacation_meal=vac_meal,
                opening_meal=open_meal,
                note_raw=note_raw,
            )

            ref_year = self.year_spin.value()
            parse_lunch_note(entry, ref_year)
            roster.entries.append(entry)

            log.debug(
                "[Lunch] %s: vac=%s open=%s v_meal=%s o_meal=%s",
                school_name,
                vac_date,
                open_date,
                vac_meal,
                open_meal,
            )

        self._lunch_roster = roster
        self._controller.set_lunch_roster(
            roster, self._lunch_mapping, self._lunch_file_path
        )
        log.info(
            "[Lunch] 재로드 완료: %d개 학교 (파일: %s)",
            len(roster.entries),
            os.path.basename(self._lunch_file_path),
        )

    # ── 설정 저장/복원 ──

    def _restore_state(self):
        # 마지막 명단 경로 복원
        last = self._settings.value("roster/last_path", "")
        if last and os.path.exists(last):
            self._controller.roster_path = last
            self.file_label.setText(os.path.basename(last))
            self.file_label.setStyleSheet("color: #000;")
            result = self._loader.load(last)
            if result:
                headers, data = result
                self._controller.roster_headers = headers
                has = bool(data)
                self.mapping_btn.setEnabled(has)
                self.generate_btn.setEnabled(has)

        # 출력 경로 복원
        out = self._settings.value("invoice/output_dir", "")
        if out:
            self._controller.output_dir = out
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
            self._controller._col_mapping = {
                k: mapping.get(k, v) for k, v in self._controller._col_mapping.items()
            }

        # 제외일
        excluded = self._settings.value("invoice/excluded_dates", None)
        if excluded is not None and isinstance(excluded, list):
            self._controller._excluded_dates = {date.fromisoformat(s) for s in excluded}
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

        # 중식 매핑 복원 + 자동 재로드
        lunch_mapping = self._settings.value("lunch/col_mapping", None)
        if lunch_mapping and isinstance(lunch_mapping, dict):
            self._lunch_mapping = lunch_mapping
        lunch_file = self._settings.value("lunch/file_path", "")
        if lunch_file and os.path.exists(lunch_file):
            self._lunch_file_path = lunch_file
            self._lunch_enabled = True
            self.lunch_file_label.setText(os.path.basename(lunch_file))
            self.lunch_file_label.setStyleSheet("color: #000;")
            self.lunch_mapping_btn.setEnabled(True)
            # 자동으로 중식명단 재로드 (매핑 정보 있으면)
            if lunch_mapping and isinstance(lunch_mapping, dict):
                self._reload_lunch_roster()

        self._save_settings()

    def _save_settings(self):
        self._settings.setValue("invoice/year", self.year_spin.value())
        self._settings.setValue("invoice/month", self.month_spin.value())
        self._settings.setValue("invoice/breakfast_price", self.breakfast_price.value())
        self._settings.setValue("invoice/dinner_price", self.dinner_price.value())
        self._settings.setValue("invoice/lunch_price", self.lunch_price.value())
        self._settings.setValue("invoice/col_mapping", self._controller._col_mapping)
        self._settings.setValue(
            "invoice/excluded_dates",
            [d.isoformat() for d in self._controller._excluded_dates],
        )
        if self._controller.output_dir:
            self._settings.setValue("invoice/output_dir", self._controller.output_dir)
        self._settings.sync()

    def _select_output_dir(self):
        d = QFileDialog.getExistingDirectory(self, "출력 폴더 선택")
        if d:
            self._controller.output_dir = d
            self.path_label.setText(d)
            self.path_label.setStyleSheet("color: #000;")
            self._settings.setValue("invoice/output_dir", d)
            self._settings.sync()

    def _open_mapping(self):
        if not self._controller.roster_headers:
            QMessageBox.warning(self, "알림", "먼저 명단 파일을 불러오세요.")
            return
        dlg = MappingDialog(
            self._controller.roster_headers, self._template_columns, self
        )
        dlg.set_values(self._controller._col_mapping)
        if not any(
            v
            for k, v in self._controller._col_mapping.items()
            if k in self._template_columns
        ):
            dlg._reset_defaults()
        if dlg.exec():
            self._controller._col_mapping = dlg.get_values()
            _sem = auto_detect_semantic_columns(self._template_columns)
            self._controller._col_mapping.update(_sem)
            self._save_settings()
            self.update()

    def _open_exclude_dialog(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("배송 제외일 선택")
        dlg.setMinimumWidth(400)
        layout = QVBoxLayout(dlg)

        y = self.year_spin.value()
        m = self.month_spin.value()

        temp_excluded = {d for d in self._controller._excluded_dates if d.weekday() < 5}
        last_day = calendar.monthrange(y, m)[1]
        for day in range(1, last_day + 1):
            d = date(y, m, day)
            if d in get_kr_holidays(y) and d.weekday() < 5:
                temp_excluded.add(d)

        cal = QCalendarWidget()
        cal.setGridVisible(True)
        cal.setCurrentPage(y, m)

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
        self._controller._excluded_dates = temp_excluded
        self._update_exclude_label()
        self._save_settings()

    def _update_exclude_label(self):
        if not self._controller._excluded_dates:
            self.exclude_label.setText("선택된 제외일 없음")
            self.exclude_label.setStyleSheet("color: #888;")
        else:
            parts = []
            for d in sorted(self._controller._excluded_dates):
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
                self._controller._excluded_dates.add(d)
        self._update_exclude_label()

    def _reload_excluded_dates(self):
        self._controller._excluded_dates.clear()
        self._load_default_excluded_dates()

    # ── 청구서 생성 ──

    def _generate_invoices(self):
        if not self._controller.output_dir:
            QMessageBox.warning(self, "알림", "출력 경로를 먼저 선택하세요.")
            return

        year = self.year_spin.value()
        month = self.month_spin.value()
        self._save_settings()

        fname = self._s.get(
            "invoice",
            "output_filename_format",
            default="청구서_{year:04d}{month:02d}.xlsx",
        ).format(year=year, month=month)
        out = os.path.join(self._controller.output_dir, fname)

        success, cnt, enriched = self._controller.generate(
            roster_data=self._loader.roster_data,
            output_path=out,
            year=year,
            month=month,
            breakfast_price=self.breakfast_price.value(),
            dinner_price=self.dinner_price.value(),
            lunch_price=self.lunch_price.value(),
            template_path=self._template_path,
            has_lunch_file=self._lunch_enabled,
        )
        if not success:
            return

        self._update_result(enriched)
        QMessageBox.information(self, "완료", f"청구서 {cnt}건 생성 완료!\n{out}")

    def _update_result(self, data: list[dict]):
        """결과 중 최대 10건만 직접 표시"""
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

    def _clear(self):
        self._controller.invoices.clear()
        self.result_table.setRowCount(0)
        self.info_label.setText("생성된 청구서: 0건")
        self._lunch_enabled = False
        self._lunch_roster = None
        self._lunch_mapping = {}
        self.lunch_file_label.setText("불러오지 않음")
        self.lunch_file_label.setStyleSheet("color: #888;")
        self.lunch_mapping_btn.setEnabled(False)

    # ── 외부 접근 ──

    def get_invoices(self):
        return self._controller.get_invoices()

    def get_col_mapping(self):
        return self._controller.get_col_mapping()

    def get_year(self):
        return self.year_spin.value()

    def get_month(self):
        return self.month_spin.value()

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


# ── 모듈 레벨 헬퍼 ──


def _load_lunch_file(file_path: str) -> tuple[list[str], list[dict]] | None:
    """중식 명단 파일을 이름 필터 없이 로드. 타이틀 행 자동 감지."""
    import re

    from python_calamine import CalamineWorkbook

    ext = os.path.splitext(file_path)[1].lower()
    if ext not in (".xlsx", ".xls", ".xlsb", ".ods"):
        return None

    try:
        wb = CalamineWorkbook.from_path(file_path)
    except Exception:
        return None

    sheet = wb.get_sheet_by_index(0)
    rows = list(sheet.iter_rows())
    if len(rows) < 2:
        return None

    # 타이틀 행 감지: 첫 셀이 "■"로 시작하거나 30자 이상
    first_cell = str(rows[0][0]).strip() if rows[0] else ""
    is_title_row = first_cell.startswith("■") or len(first_cell) > 30

    if is_title_row:
        # 두 번째 행이 실제 헤더
        header_row = rows[1]
        data_start = 2
    else:
        header_row = rows[0]
        data_start = 1

    # 헤더 정규화
    raw_headers = [str(c).replace(chr(10), " ").strip() for c in header_row]
    headers = []
    seen = {}
    for h in raw_headers:
        if not h:
            h = chr(65 + len(headers)) if len(headers) < 26 else f"Col{len(headers)}"
        if h in seen:
            seen[h] += 1
            h = f"{h}_{seen[h]}"
        else:
            seen[h] = 1
        headers.append(h)

    # 데이터 파싱 (이름 필터 없음)
    data = []
    for row in rows[data_start:]:
        vals = [str(c).strip() for c in row]
        # 빈 행 건너뛰기
        if all(not v or v == " " for v in vals):
            continue
        entry = {
            headers[i]: vals[i] if i < len(vals) else "" for i in range(len(headers))
        }
        data.append(entry)

    return headers, data


def _parse_lunch_date(val) -> date | None:
    """셀 값을 date로 변환. datetime 객체, '2025.12.31', '2025-12-31' 등 지원."""
    import datetime as _dt

    if val is None:
        return None

    # datetime/date 객체 직접 처리
    if isinstance(val, _dt.datetime):
        return val.date()
    if isinstance(val, _dt.date):
        return val

    # float → 엑셀 시리얼 넘버 가능성 (1900-01-01 = 1)
    if isinstance(val, (int, float)):
        if 40000 < val < 80000:  # 대략 2009~2118 범위
            try:
                from datetime import timedelta

                return _dt.date(1899, 12, 30) + timedelta(days=int(val))
            except (ValueError, OverflowError):
                pass
        return None

    # 문자열 처리
    s = str(val).strip().rstrip(".")
    if not s:
        return None

    # "2025-01-10 00:00:00" → "2025-01-10"
    s = s.split()[0] if " " in s else s

    for sep in (".", "-", "/"):
        parts = s.split(sep)
        if len(parts) == 3:
            try:
                y, m, d = int(parts[0]), int(parts[1]), int(parts[2])
                if 2000 <= y <= 2100 and 1 <= m <= 12 and 1 <= d <= 31:
                    return date(y, m, d)
            except (ValueError, OverflowError):
                pass
    return None
