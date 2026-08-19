"""청구서 탭 — UI 셸. 파일 로드 + 생성 로직은 RosterLoader/InvoiceController 위임."""

import calendar
import logging
import os
from datetime import date, datetime
from typing import ClassVar

from PyQt6.QtCore import QDate, QSettings, Qt, QUrl
from PyQt6.QtGui import QDesktopServices, QTextCharFormat
from PyQt6.QtWidgets import (
    QCalendarWidget,
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

from ..core.invoice_data import InvoiceData
from ..core.settings import get_settings
from ..generators.daily_delivery_generator import prepare_daily_delivery_people
from ..services.holiday_fetcher import get_kr_holidays
from ..services.invoice_controller import InvoiceController
from ..services.lunch_data import LunchRoster, LunchSchoolEntry, parse_lunch_boolean
from ..services.lunch_parser import parse_lunch_note
from ..services.roster_loader import FileReader, RosterLoader
from ..templates.config import auto_detect_semantic_columns, read_template_columns
from .lunch_mapping_dialog import LunchMappingDialog
from .mapping_dialog import MappingDialog

log = logging.getLogger(__name__)


class InvoiceTab(QWidget):
    """청구서 생성 탭 (명단 임포트 포함) — UI 전용, 로직은 Controller/Loader 위임."""

    MAX_RESULT_ROWS = 10
    EXCEL_EXTENSIONS: ClassVar[set[str]] = {".xlsx", ".xls", ".xlsb", ".ods"}

    def __init__(self):
        super().__init__()
        self._s = get_settings()
        self._template_path = self._s.get_invoice_template_path()
        self._template_columns = read_template_columns(self._template_path)
        self._controller = InvoiceController(self, self._template_columns)
        self._loader = RosterLoader(self)
        org = self._s.get_app_organization()
        app_title = self._s.get_app_title()
        self._settings = QSettings(org, app_title)
        self._lunch_enabled = False
        self._lunch_roster: LunchRoster | None = None
        self._lunch_mapping: dict = {}
        self._lunch_file_path: str = ""
        self._lunch_sheet_name: str = ""
        self._roster_file_path: str = ""
        self._roster_sheet_name: str = ""
        self._last_output_path: str | None = None
        self._setup_ui()
        self._restore_ui_state()
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
        self.mapping_btn = QPushButton("컬럼 매핑")
        self.mapping_btn.setEnabled(False)
        self.mapping_btn.clicked.connect(self._open_mapping)
        self.roster_sheet_combo = QComboBox()
        self.roster_sheet_combo.setMinimumWidth(150)
        self.roster_sheet_combo.setEnabled(False)
        self.roster_sheet_combo.currentIndexChanged.connect(
            self._on_roster_sheet_changed
        )
        import_layout.addWidget(self.file_label, stretch=1)
        import_layout.addWidget(QLabel("시트:"))
        import_layout.addWidget(self.roster_sheet_combo)
        import_layout.addWidget(self.select_btn)
        import_layout.addWidget(self.mapping_btn)
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
        self.lunch_sheet_combo = QComboBox()
        self.lunch_sheet_combo.setMinimumWidth(150)
        self.lunch_sheet_combo.setEnabled(False)
        self.lunch_sheet_combo.currentIndexChanged.connect(
            self._on_lunch_sheet_changed
        )
        lunch_layout.addWidget(self.lunch_file_label, stretch=1)
        lunch_layout.addWidget(QLabel("시트:"))
        lunch_layout.addWidget(self.lunch_sheet_combo)
        lunch_layout.addWidget(self.lunch_file_btn)
        lunch_layout.addWidget(self.lunch_mapping_btn)
        layout.addWidget(lunch_group)

        # ── 청구 기준 ──
        config_group = QGroupBox("② 청구 기준")
        config_row = QHBoxLayout(config_group)
        today = datetime.now().astimezone().date()

        config_row.addWidget(QLabel("귀속 연월:"))
        self.year_spin = QSpinBox()
        self.year_spin.setRange(2020, 2030)
        self.year_spin.setValue(today.year)
        config_row.addWidget(self.year_spin)
        config_row.addWidget(QLabel("년"))
        self.month_spin = QSpinBox()
        self.month_spin.setRange(1, 12)
        self.month_spin.setValue(today.month)
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

    @staticmethod
    def _populate_sheet_combo(
        combo: QComboBox, sheet_names: list[str], preferred: str = ""
    ) -> str:
        combo.blockSignals(True)
        combo.clear()
        if not sheet_names:
            combo.addItem("(해당 없음)")
            combo.setEnabled(False)
            selected = ""
        else:
            combo.addItems(sheet_names)
            index = combo.findText(preferred) if preferred else 0
            combo.setCurrentIndex(max(index, 0))
            combo.setEnabled(True)
            selected = combo.currentText()
        combo.blockSignals(False)
        return selected

    def _apply_roster_result(
        self,
        path: str,
        sheet_name: str,
        result: tuple[list[str], list[dict]],
        *,
        invalidate_generated: bool = True,
    ):
        headers, data = result
        if invalidate_generated:
            self._controller.invoices.clear()
            self.result_table.setRowCount(0)
            self._last_output_path = None
            self.open_file_btn.setEnabled(False)
        self._roster_file_path = path
        self._roster_sheet_name = sheet_name
        self._controller.set_roster_headers(headers)
        self._controller.set_roster_path(path)
        self.file_label.setText(os.path.basename(path))
        self.file_label.setStyleSheet("color: #000;")
        self._settings.setValue("roster/last_path", path)
        self._settings.setValue("roster/last_sheet", sheet_name)
        self._settings.sync()
        has_data = bool(data)
        self.mapping_btn.setEnabled(has_data)
        self.generate_btn.setEnabled(has_data)
        if has_data and (invalidate_generated or not self._controller.invoices):
            sheet_text = f" (시트: {sheet_name})" if sheet_name else ""
            self.info_label.setText(
                f"명단 {len(data)}명 로드됨{sheet_text}. 컬럼 매핑 → 생성"
            )

    def _load_roster_path(
        self,
        path: str,
        preferred_sheet: str = "",
        *,
        invalidate_generated: bool = True,
    ) -> bool:
        is_excel = os.path.splitext(path)[1].lower() in self.EXCEL_EXTENSIONS
        sheet_name = ""
        if is_excel:
            sheet_names = self._loader.list_sheets(path)
            if sheet_names is None:
                return False
            if not sheet_names:
                QMessageBox.warning(self, "알림", "선택한 파일에 시트가 없습니다.")
                return False
            sheet_name = self._populate_sheet_combo(
                self.roster_sheet_combo, sheet_names, preferred_sheet
            )
        else:
            self._populate_sheet_combo(self.roster_sheet_combo, [])

        result = self._loader.load(path, sheet_name=sheet_name or None)
        if not result:
            return False
        self._apply_roster_result(
            path,
            sheet_name,
            result,
            invalidate_generated=invalidate_generated,
        )
        return True

    def _on_roster_sheet_changed(self, index: int):
        if index < 0 or not self._roster_file_path:
            return
        sheet_name = self.roster_sheet_combo.currentText()
        if not sheet_name or sheet_name == self._roster_sheet_name:
            return
        result = self._loader.load(self._roster_file_path, sheet_name=sheet_name)
        if result:
            self._apply_roster_result(self._roster_file_path, sheet_name, result)
            return

        self.roster_sheet_combo.blockSignals(True)
        previous_index = self.roster_sheet_combo.findText(self._roster_sheet_name)
        self.roster_sheet_combo.setCurrentIndex(previous_index)
        self.roster_sheet_combo.blockSignals(False)

    def _select_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "명단 파일 선택",
            "",
            "지원 파일 (*.xlsx *.xls *.csv *.txt);;Excel (*.xlsx *.xls);;CSV (*.csv);;텍스트 (*.txt);;모든 파일 (*.*)",
        )
        if path:
            saved_path = str(self._settings.value("roster/last_path", "") or "")
            preferred = (
                str(self._settings.value("roster/last_sheet", "") or "")
                if saved_path
                and os.path.normcase(os.path.abspath(saved_path))
                == os.path.normcase(os.path.abspath(path))
                else ""
            )
            self._load_roster_path(path, preferred)

    # ── 중식 파일 로드 ──

    def _configure_lunch_file(
        self, path: str, preferred_sheet: str = "", *, persist: bool = True
    ) -> bool:
        try:
            sheet_names = FileReader.list_excel_sheets(path)
        except Exception as error:  # noqa: BLE001 - UI 경계에서 대화상자로 변환
            QMessageBox.critical(self, "오류", f"중식 시트 목록 읽기 실패:\n{error}")
            return False
        if sheet_names is None:
            QMessageBox.warning(
                self,
                "알림",
                "암호로 보호된 중식 명단은 시트 선택을 지원하지 않습니다.",
            )
            return False
        if not sheet_names:
            QMessageBox.warning(self, "알림", "중식 명단에 시트가 없습니다.")
            return False

        self._lunch_file_path = path
        self._lunch_sheet_name = self._populate_sheet_combo(
            self.lunch_sheet_combo, sheet_names, preferred_sheet
        )
        self._lunch_roster = None
        self._controller.set_lunch_roster(None, {}, path)
        self._lunch_enabled = True
        self.lunch_file_label.setText(os.path.basename(path))
        self.lunch_file_label.setStyleSheet("color: #000;")
        self.lunch_mapping_btn.setEnabled(True)
        if persist:
            self._settings.setValue("lunch/file_path", path)
            self._settings.setValue("lunch/sheet_name", self._lunch_sheet_name)
            self._settings.sync()
        return True

    def _on_lunch_sheet_changed(self, index: int):
        if index < 0 or not self._lunch_file_path:
            return
        sheet_name = self.lunch_sheet_combo.currentText()
        if not sheet_name or sheet_name == self._lunch_sheet_name:
            return
        self._lunch_sheet_name = sheet_name
        self._lunch_roster = None
        self._controller.set_lunch_roster(None, {}, self._lunch_file_path)
        self._settings.setValue("lunch/sheet_name", sheet_name)
        self._settings.sync()
        if self._lunch_mapping:
            self._reload_lunch_roster()

    def _select_lunch_file(self):
        """중식 명단 파일 로드. 로드 후 컬럼 매핑 버튼 활성화."""
        path, _ = QFileDialog.getOpenFileName(
            self,
            "중식 명단 파일 선택",
            "",
            "Excel (*.xlsx *.xls *.xlsb *.ods);;모든 파일 (*.*)",
        )
        if path:
            saved_path = str(self._settings.value("lunch/file_path", "") or "")
            preferred = (
                str(self._settings.value("lunch/sheet_name", "") or "")
                if saved_path
                and os.path.normcase(os.path.abspath(saved_path))
                == os.path.normcase(os.path.abspath(path))
                else ""
            )
            if not self._configure_lunch_file(path, preferred):
                return
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
        loaded = _load_lunch_file(
            self._lunch_file_path, sheet_name=self._lunch_sheet_name or None
        )
        if not loaded:
            QMessageBox.warning(
                self, "알림", "선택한 중식 시트에서 데이터를 읽을 수 없습니다."
            )
            return
        lunch_headers, lunch_data = loaded

        roster_headers = self._controller.get_roster_headers() or []

        # 방학_ 시트 컬럼도 포함 (배송시작/종료 컬럼은 방학 시트에만 있을 수 있음)
        all_tpl_cols = list(self._template_columns)
        for vac_sheet in ("방학_총괄", "방학_학기중", "방학_방중"):
            try:
                vac_cols = read_template_columns(
                    self._template_path, sheet_name=vac_sheet
                )
                for c in vac_cols:
                    if c not in all_tpl_cols:
                        all_tpl_cols.append(c)
            except Exception:
                log.debug("선택 템플릿 시트 '%s' 읽기 건너뜀", vac_sheet, exc_info=True)

        dlg = LunchMappingDialog(lunch_headers, roster_headers, all_tpl_cols, self)

        saved = self._settings.value("lunch/col_mapping", None)
        if saved and isinstance(saved, dict):
            dlg.set_values(saved)
        else:
            dlg._reset_defaults()

        if not dlg.exec():
            return

        self._lunch_mapping = dlg.get_values()
        self._sync_delivery_cols_to_mapping()

        roster = _build_lunch_roster(
            lunch_headers,
            lunch_data,
            self._lunch_mapping,
            self._lunch_file_path,
            self.year_spin.value(),
        )

        self._lunch_roster = roster
        self._controller.set_lunch_roster(
            roster, self._lunch_mapping, self._lunch_file_path
        )

        self._settings.setValue("lunch/col_mapping", self._lunch_mapping)
        self._settings.setValue("lunch/file_path", self._lunch_file_path)
        self._settings.setValue("lunch/sheet_name", self._lunch_sheet_name)
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

    def _sync_delivery_cols_to_mapping(self):
        """lunch_mapping의 delivery_start_col/end_col → controller col_mapping 동기화."""
        if not self._lunch_mapping:
            return
        for lunch_key, col_key in [
            ("delivery_start_col", "__delivery_start"),
            ("delivery_end_col", "__delivery_end"),
        ]:
            col_name = self._lunch_mapping.get(lunch_key, "")
            if col_name:
                self._controller._col_mapping[col_key] = col_name

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

        loaded = _load_lunch_file(
            self._lunch_file_path, sheet_name=self._lunch_sheet_name or None
        )
        if not loaded:
            log.warning("[Lunch] 재로드 실패: 선택 시트 데이터 없음")
            return
        lunch_headers, lunch_data = loaded

        roster = _build_lunch_roster(
            lunch_headers,
            lunch_data,
            self._lunch_mapping,
            self._lunch_file_path,
            self.year_spin.value(),
        )

        self._lunch_roster = roster
        self._sync_delivery_cols_to_mapping()
        self._controller.set_lunch_roster(
            roster, self._lunch_mapping, self._lunch_file_path
        )
        log.info(
            "[Lunch] 재로드 완료: %d개 학교 (파일: %s)",
            len(roster.entries),
            os.path.basename(self._lunch_file_path),
        )

    # ── 설정 저장/복원 ──

    def _restore_ui_state(self):
        """복원 UI 상태 (I/O 없음, 위젯 생성 중 호출 가능)."""
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
            self._controller.set_col_mapping(
                {
                    k: mapping.get(k, v)
                    for k, v in self._controller.get_col_mapping().items()
                }
            )

        # 제외일
        excluded = self._settings.value("invoice/excluded_dates", None)
        if excluded is not None and isinstance(excluded, list):
            self._controller.set_excluded_dates(
                {date.fromisoformat(s) for s in excluded}
            )
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

        # 중식 매핑/경로 복원 (로드는 post_init_load 에서)
        lunch_mapping = self._settings.value("lunch/col_mapping", None)
        if lunch_mapping and isinstance(lunch_mapping, dict):
            self._lunch_mapping = lunch_mapping
        lunch_file = self._settings.value("lunch/file_path", "")
        if lunch_file and os.path.exists(lunch_file):
            self._lunch_file_path = lunch_file
            self._lunch_sheet_name = str(
                self._settings.value("lunch/sheet_name", "") or ""
            )
            self._lunch_enabled = True
            self.lunch_file_label.setText(os.path.basename(lunch_file))
            self.lunch_file_label.setStyleSheet("color: #000;")
            self.lunch_mapping_btn.setEnabled(True)

        self._save_settings()

        # ── 캐시된 InvoiceData 복원 (청구서 재생성 없이 확인서 탭 사용 가능) ──
        cached = self._load_cached_invoice_data()
        if cached and cached.invoices:
            self._controller.invoices = cached.invoices
            self._controller.set_col_mapping(cached.col_mapping)
            self._controller._year = cached.year
            self._controller._month = cached.month
            self._controller.set_excluded_dates(cached.excluded_dates)
            self._controller.roster_headers = cached.roster_headers
            self._controller.roster_path = cached.roster_path
            self._update_result(cached.invoices)
            self._update_exclude_label()

    def _try_auto_load_roster(self):
        """저장된 명단 자동 로드 (I/O 가능, 생성 후 호출)."""
        last = self._settings.value("roster/last_path", "")
        if last and os.path.exists(last):
            preferred = str(
                self._settings.value("roster/last_sheet", "") or ""
            )
            self._load_roster_path(
                str(last), preferred, invalidate_generated=False
            )

        # 중식명단 자동 재로드
        lunch_mapping = self._settings.value("lunch/col_mapping", None)
        lunch_ready = bool(
            self._lunch_file_path
            and self._configure_lunch_file(
                self._lunch_file_path,
                self._lunch_sheet_name,
                persist=False,
            )
        )
        if lunch_ready and lunch_mapping and isinstance(lunch_mapping, dict):
            self._reload_lunch_roster()

    def post_init_load(self):
        """생성 후 I/O 로드. MainWindow show() 후 호출."""
        self._try_auto_load_roster()

    def _save_settings(self):
        self._settings.setValue("invoice/year", self.year_spin.value())
        self._settings.setValue("invoice/month", self.month_spin.value())
        self._settings.setValue("invoice/breakfast_price", self.breakfast_price.value())
        self._settings.setValue("invoice/dinner_price", self.dinner_price.value())
        self._settings.setValue("invoice/lunch_price", self.lunch_price.value())
        self._settings.setValue(
            "invoice/col_mapping", self._controller.get_col_mapping()
        )
        self._settings.setValue(
            "invoice/excluded_dates",
            [d.isoformat() for d in self._controller.get_excluded_dates()],
        )
        if self._controller.output_dir:
            self._settings.setValue("invoice/output_dir", self._controller.output_dir)
        self._settings.sync()

    def _cache_invoice_data(self):
        """생성된 InvoiceData를 JSON 캐시 파일로 저장 (확인서 탭 재사용)."""
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

    def _load_cached_invoice_data(self):  # -> InvoiceData | None
        """캐시 파일에서 InvoiceData 복원. 없으면 None."""
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

    def _open_mapping(self):
        if not self._controller.get_roster_headers():
            QMessageBox.warning(self, "알림", "먼저 명단 파일을 불러오세요.")
            return
        dlg = MappingDialog(
            self._controller.get_roster_headers(), self._template_columns, self
        )
        dlg.set_values(self._controller.get_col_mapping())
        if not any(
            v
            for k, v in self._controller.get_col_mapping().items()
            if k in self._template_columns
        ):
            dlg._reset_defaults()
        if dlg.exec():
            mapping = dlg.get_values()
            _sem = auto_detect_semantic_columns(self._template_columns)
            mapping.update(_sem)
            self._controller.set_col_mapping(mapping)
            self._save_settings()
            self.update()

    def _open_exclude_dialog(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("배송 제외일 선택")
        dlg.setMinimumWidth(400)
        layout = QVBoxLayout(dlg)

        y = self.year_spin.value()
        m = self.month_spin.value()

        temp_excluded = {
            d for d in self._controller.get_excluded_dates() if d.weekday() < 5
        }
        last_day = calendar.monthrange(y, m)[1]
        # 공휴일은 _load_default_excluded_dates() 에서 이미 추가됨 — 중복 제거

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
        self._controller.set_excluded_dates(temp_excluded)
        self._update_exclude_label()
        self._save_settings()

    def _update_exclude_label(self):
        if not self._controller.get_excluded_dates():
            self.exclude_label.setText("선택된 제외일 없음")
            self.exclude_label.setStyleSheet("color: #888;")
        else:
            parts = []
            for d in sorted(self._controller.get_excluded_dates()):
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
                self._controller.add_excluded_date(d)
        self._update_exclude_label()

    def _reload_excluded_dates(self):
        self._controller.clear_excluded_dates()
        self._load_default_excluded_dates()

    # ── 청구서 생성 ──

    def _generate_invoices(self):
        if not self._controller.output_dir:
            QMessageBox.warning(self, "알림", "출력 경로를 먼저 선택하세요.")
            return

        year = self.year_spin.value()
        month = self.month_spin.value()
        self._save_settings()

        fname = self._s.get_invoice_output_filename_format().format(
            year=year, month=month
        )
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

        self._last_output_path = out
        self.open_file_btn.setEnabled(True)
        self._update_result(enriched)

        # ── InvoiceData 캐시 저장 (확인서 탭에서 재사용) ──
        self._cache_invoice_data()

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
        self.info_label.setText("생성된 청구서: 0건")
        self._last_output_path = None
        self.open_file_btn.setEnabled(False)
        self._lunch_enabled = False
        self._lunch_roster = None
        self._lunch_mapping = {}
        self._lunch_file_path = ""
        self._lunch_sheet_name = ""
        self._controller.set_lunch_roster(None, {}, "")
        self._populate_sheet_combo(self.lunch_sheet_combo, [])
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

    @staticmethod
    def _find_roster_column(headers: list[str], candidates: list[str]) -> str:
        for candidate in candidates:
            if candidate in headers:
                return candidate
        for candidate in candidates:
            for header in headers:
                if candidate in header:
                    return header
        return ""

    def _daily_delivery_mapping(self) -> dict:
        """현재 명단에 유효한 조식·석식·비고 매핑을 반환한다."""

        mapping = self.get_col_mapping()
        headers = self.get_roster_headers()
        search_terms = self._s.get_flag_search_terms()
        for key in ("__breakfast", "__dinner", "__note"):
            selected = str(mapping.get(key, "") or "")
            if selected not in headers:
                selected = self._find_roster_column(
                    headers, list(search_terms.get(key, [key]))
                )
            mapping[key] = selected
        return mapping

    def get_daily_delivery_data(self) -> InvoiceData:
        """청구서를 만들지 않아도 현재 명단으로 일일배송 데이터를 만든다."""

        mapping = self._daily_delivery_mapping()
        roster_data = self._loader.roster_data
        if roster_data:
            breakfast_col = mapping.get("__breakfast", "")
            dinner_col = mapping.get("__dinner", "")
            note_col = mapping.get("__note", "")
            stop_col = next(
                (
                    header
                    for header in self.get_roster_headers()
                    if "중지" in header or "중단" in header
                ),
                None,
            )
            daily_people = prepare_daily_delivery_people(
                roster_data,
                breakfast_col=breakfast_col,
                dinner_col=dinner_col,
                note_col=note_col,
                stop_col=stop_col or "",
            )
        else:
            daily_people = list(self.get_invoices())

        return InvoiceData(
            invoices=daily_people,
            col_mapping=mapping,
            year=self.get_year(),
            month=self.get_month(),
            excluded_dates=self.get_excluded_dates(),
            roster_headers=self.get_roster_headers(),
            roster_path=self.get_roster_path(),
        )

    def get_invoice_data(self):

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


def _load_lunch_file(
    file_path: str, sheet_name: str | None = None
) -> tuple[list[str], list[dict]] | None:
    """중식 명단 파일을 이름 필터 없이 로드. 타이틀 행 자동 감지."""
    ext = os.path.splitext(file_path)[1].lower()
    if ext not in (".xlsx", ".xls", ".xlsb", ".ods"):
        return None

    try:
        rows = FileReader.read_excel(file_path, sheet_name=sheet_name)
    except Exception:  # noqa: BLE001 - 읽기 실패를 호출자용 None으로 변환
        return None
    if rows is None:
        return None
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


def _build_lunch_roster(
    lunch_headers: list[str],
    lunch_data: list[dict],
    lunch_mapping: dict,
    lunch_file_path: str,
    ref_year: int,
) -> LunchRoster:
    """중식 명단 데이터 → LunchRoster 생성 (공통 헬퍼).

    _open_lunch_mapping()과 _reload_lunch_roster()에서 중복 코드 제거.
    """
    lunch_school_col = lunch_mapping.get("lunch_school_name", "")
    lunch_vac_date_col = lunch_mapping.get("lunch_vacation_date", "")
    lunch_open_date_col = lunch_mapping.get("lunch_opening_date", "")
    lunch_vac_meal_col = lunch_mapping.get("lunch_vacation_meal", "")
    lunch_open_meal_col = lunch_mapping.get("lunch_opening_meal", "")
    lunch_note_col = lunch_mapping.get("lunch_note", "")

    roster = LunchRoster(headers=lunch_headers, file_path=lunch_file_path)
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

    return roster


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
