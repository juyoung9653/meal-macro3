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
    QLineEdit,
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
from ..services.holiday_fetcher import get_kr_holidays
from ..services.invoice_controller import (
    LUNCH_DAILY_MAPPING_FIELDS,
    LUNCH_DAILY_REQUIRED_FIELDS,
    LUNCH_PERSON_REQUIRED_FIELDS,
    InvoiceController,
)
from ..services.lunch_data import LunchRoster, LunchSchoolEntry
from ..services.roster_loader import FileReader, RosterLoader
from ..templates.config import auto_detect_semantic_columns, read_template_columns
from .lunch_mapping_dialog import LunchMappingDialog
from .mapping_dialog import MappingDialog

log = logging.getLogger(__name__)

_LUNCH_PERSON_REQUIRED_KEYS = (
    LUNCH_PERSON_REQUIRED_FIELDS + LUNCH_DAILY_REQUIRED_FIELDS
)
_SCHEDULE_REQUIRED_KEYS = ("school_name_col", "start_date_col", "end_date_col")


def _sanitize_column_mapping(mapping: dict, headers: list[str]) -> dict:
    """현재 파일에 없는 예전 컬럼 선택값을 비운다."""

    header_set = set(headers)
    sanitized: dict = {}
    for key, value in mapping.items():
        selected = str(value or "")
        is_output_mapping = key.startswith("__calc_") or key in {
            "__delivery_start",
            "__delivery_end",
        }
        sanitized[key] = selected if is_output_mapping or selected in header_set else ""
    return sanitized


def _source_mapping_columns(template_columns: list[str]) -> list[str]:
    """명단에서 사용자가 직접 선택해야 하는 컬럼만 반환한다."""

    calculated_columns = {
        column
        for column in auto_detect_semantic_columns(template_columns).values()
        if column
    }
    result: list[str] = []
    for column in template_columns:
        compact = "".join(str(column).split())
        if column == "제공기관" or column in calculated_columns:
            continue
        if "비고" in compact:
            # 비고는 내부 계산용 __note 선택 하나로 출력까지 함께 연결한다.
            continue
        if "배송시작" in compact or "배송종료" in compact:
            continue
        result.append(column)
    return result


def _unify_note_mapping(mapping: dict, template_columns: list[str]) -> dict:
    """비고 선택 하나를 계산용 키와 템플릿 비고 컬럼에 함께 적용한다."""

    unified = dict(mapping)
    note_columns = [
        column
        for column in template_columns
        if "비고" in "".join(str(column).split())
    ]
    selected = str(unified.get("__note", "") or "")
    if not selected:
        selected = next(
            (
                str(unified.get(column, "") or "")
                for column in note_columns
                if unified.get(column)
            ),
            "",
        )

    unified["__note"] = selected
    for column in note_columns:
        unified[column] = selected
    return unified


def _has_required_mapping(
    mapping: dict,
    headers: list[str],
    required_keys: tuple[str, ...],
) -> bool:
    header_set = set(headers)
    selected_columns = [str(mapping.get(key, "") or "") for key in required_keys]
    return all(selected and selected in header_set for selected in selected_columns)


class InvoiceTab(QWidget):
    """청구서 생성 탭 (명단 임포트 포함) — UI 전용, 로직은 Controller/Loader 위임."""

    MAX_RESULT_ROWS = 10
    EXCEL_EXTENSIONS: ClassVar[set[str]] = {".xlsx", ".xls", ".xlsb", ".ods"}

    def __init__(self):
        super().__init__()
        self._s = get_settings()
        self._template_path = self._s.get_invoice_template_path()
        self._template_columns = read_template_columns(self._template_path)
        self._all_template_columns = list(self._template_columns)
        for sheet_name in ("방학_총괄", "방학_학기중", "방학_방중"):
            try:
                for column in read_template_columns(
                    self._template_path, sheet_name=sheet_name
                ):
                    if column not in self._all_template_columns:
                        self._all_template_columns.append(column)
            except Exception:
                log.debug("템플릿 시트 '%s' 컬럼 읽기 건너뜀", sheet_name, exc_info=True)
        self._source_mapping_columns = _source_mapping_columns(
            self._all_template_columns
        )
        self._controller = InvoiceController(self, self._all_template_columns)
        self._loader = RosterLoader(self)
        self._lunch_person_loader = RosterLoader(self)
        self._schedule_loader = RosterLoader(self)
        org = self._s.get_app_organization()
        app_title = self._s.get_app_title()
        self._settings = QSettings(org, app_title)
        self._lunch_enabled = False
        self._lunch_roster: LunchRoster | None = None
        self._lunch_mapping: dict = {}
        self._lunch_file_path: str = ""
        self._lunch_sheet_name: str = ""
        self._lunch_person_mapping: dict = {}
        self._lunch_person_file_path: str = ""
        self._lunch_person_sheet_name: str = ""
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
        import_group = QGroupBox("① 조식·석식 명단")
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

        # ── 중식 대상자 명단 ──
        lunch_person_group = QGroupBox("② 중식 명단")
        lunch_person_layout = QHBoxLayout(lunch_person_group)
        self.lunch_person_file_label = QLabel("불러오지 않음")
        self.lunch_person_file_label.setStyleSheet("color: #888;")
        self.lunch_person_file_btn = QPushButton("파일 탐색")
        self.lunch_person_file_btn.clicked.connect(self._select_lunch_person_file)
        self.lunch_person_mapping_btn = QPushButton("컬럼 매핑")
        self.lunch_person_mapping_btn.setEnabled(False)
        self.lunch_person_mapping_btn.clicked.connect(
            self._open_lunch_person_mapping
        )
        self.lunch_person_sheet_combo = QComboBox()
        self.lunch_person_sheet_combo.setMinimumWidth(150)
        self.lunch_person_sheet_combo.setEnabled(False)
        self.lunch_person_sheet_combo.currentIndexChanged.connect(
            self._on_lunch_person_sheet_changed
        )
        lunch_person_layout.addWidget(self.lunch_person_file_label, stretch=1)
        lunch_person_layout.addWidget(QLabel("시트:"))
        lunch_person_layout.addWidget(self.lunch_person_sheet_combo)
        lunch_person_layout.addWidget(self.lunch_person_file_btn)
        lunch_person_layout.addWidget(self.lunch_person_mapping_btn)
        layout.addWidget(lunch_person_group)

        # ── 학교별 방학 일정 파일 ──
        lunch_group = QGroupBox("③ 학교별 방학 일정 (학교명·시작일·종료일)")
        lunch_layout = QHBoxLayout(lunch_group)
        self.lunch_file_label = QLabel("불러오지 않음")
        self.lunch_file_label.setStyleSheet("color: #888;")
        self.lunch_file_btn = QPushButton("일정 파일 탐색")
        self.lunch_file_btn.clicked.connect(self._select_lunch_file)
        self.lunch_mapping_btn = QPushButton("일정 컬럼 매핑")
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
        config_group = QGroupBox("④ 청구 기준")
        config_layout = QVBoxLayout(config_group)
        config_row = QHBoxLayout()
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
        config_layout.addLayout(config_row)

        provider_row = QHBoxLayout()
        provider_row.addWidget(QLabel("제공기관:"))
        self.provider_name_edit = QLineEdit()
        self.provider_name_edit.setPlaceholderText("모든 청구 행에 동일하게 입력")
        self.provider_name_edit.setText("행복도시락")
        provider_row.addWidget(self.provider_name_edit, stretch=1)
        config_layout.addLayout(provider_row)
        layout.addWidget(config_group)

        # ── 출력 경로 ──
        path_group = QGroupBox("⑤ 출력 경로")
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
        self.provider_name_edit.textChanged.connect(self._save_settings)

        # ── 결과 테이블 (최대 10행) ──
        table_group = QGroupBox("⑥ 생성 결과")
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
        current_mapping = _sanitize_column_mapping(
            self._controller.get_col_mapping(), headers
        )
        self._controller.set_col_mapping(current_mapping)
        self.file_label.setText(os.path.basename(path))
        self.file_label.setStyleSheet("color: #000;")
        self._settings.setValue("roster/last_path", path)
        self._settings.setValue("roster/last_sheet", sheet_name)
        self._settings.sync()
        has_data = bool(data)
        self.mapping_btn.setEnabled(has_data)
        self._update_generate_enabled()
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

    def _update_generate_enabled(self):
        """조식·석식 또는 중식 명단 중 하나라도 있으면 생성을 허용한다."""

        self.generate_btn.setEnabled(
            bool(self._loader.roster_data or self._lunch_person_loader.roster_data)
        )

    # ── 중식 대상자 명단 로드 ──

    def _apply_lunch_person_result(
        self,
        path: str,
        sheet_name: str,
        result: tuple[list[str], list[dict]],
    ):
        headers, data = result
        self._controller.invoices.clear()
        self.result_table.setRowCount(0)
        self._last_output_path = None
        self.open_file_btn.setEnabled(False)
        self._lunch_person_file_path = path
        self._lunch_person_sheet_name = sheet_name
        self._lunch_person_mapping = _sanitize_column_mapping(
            self._lunch_person_mapping, headers
        )
        self.lunch_person_file_label.setText(os.path.basename(path))
        self.lunch_person_file_label.setStyleSheet("color: #000;")
        self.lunch_person_mapping_btn.setEnabled(bool(data))
        self._controller.set_lunch_people(
            data,
            headers,
            self._lunch_person_mapping,
            path,
        )
        self._settings.setValue("lunch_roster/file_path", path)
        self._settings.setValue("lunch_roster/sheet_name", sheet_name)
        self._settings.sync()
        self._update_generate_enabled()
        if data:
            sheet_text = f" (시트: {sheet_name})" if sheet_name else ""
            self.info_label.setText(
                f"중식 명단 {len(data)}명 로드됨{sheet_text}. 컬럼 매핑을 확인하세요."
            )

    def _load_lunch_person_path(
        self, path: str, preferred_sheet: str = ""
    ) -> bool:
        is_excel = os.path.splitext(path)[1].lower() in self.EXCEL_EXTENSIONS
        sheet_name = ""
        if is_excel:
            sheet_names = self._lunch_person_loader.list_sheets(path)
            if sheet_names is None:
                return False
            if not sheet_names:
                QMessageBox.warning(self, "알림", "중식 명단에 시트가 없습니다.")
                return False
            sheet_name = self._populate_sheet_combo(
                self.lunch_person_sheet_combo, sheet_names, preferred_sheet
            )
        else:
            self._populate_sheet_combo(self.lunch_person_sheet_combo, [])

        result = self._lunch_person_loader.load(
            path, sheet_name=sheet_name or None
        )
        if not result:
            return False
        self._apply_lunch_person_result(path, sheet_name, result)
        return True

    def _on_lunch_person_sheet_changed(self, index: int):
        if index < 0 or not self._lunch_person_file_path:
            return
        sheet_name = self.lunch_person_sheet_combo.currentText()
        if not sheet_name or sheet_name == self._lunch_person_sheet_name:
            return
        result = self._lunch_person_loader.load(
            self._lunch_person_file_path, sheet_name=sheet_name
        )
        if result:
            self._apply_lunch_person_result(
                self._lunch_person_file_path, sheet_name, result
            )

    def _select_lunch_person_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "중식 명단 파일 선택",
            "",
            "지원 파일 (*.xlsx *.xls *.xlsb *.ods *.csv *.txt);;모든 파일 (*.*)",
        )
        if not path:
            return
        saved_path = str(
            self._settings.value("lunch_roster/file_path", "") or ""
        )
        preferred = (
            str(self._settings.value("lunch_roster/sheet_name", "") or "")
            if saved_path
            and os.path.normcase(os.path.abspath(saved_path))
            == os.path.normcase(os.path.abspath(path))
            else ""
        )
        self._load_lunch_person_path(path, preferred)

    def _open_lunch_person_mapping(self):
        headers = self._lunch_person_loader.roster_headers
        if not headers:
            QMessageBox.warning(self, "알림", "먼저 중식 명단 파일을 불러오세요.")
            return

        dlg = MappingDialog(
            headers,
            self._source_mapping_columns,
            self,
            title="중식 명단 컬럼 매핑",
            flag_keys=LUNCH_DAILY_MAPPING_FIELDS + ("__note",),
            required_keys=_LUNCH_PERSON_REQUIRED_KEYS,
            info_text=(
                "별표가 붙은 개인정보·배송 담당·1순위 구역 컬럼을 모두 "
                "연결하세요. 2순위 구역과 비고는 선택사항이며, 이 명단의 "
                "모든 행은 중식 대상으로 계산됩니다."
            ),
        )
        dlg._reset_defaults()
        dlg.set_values(self._lunch_person_mapping)
        if not dlg.exec():
            return

        self._lunch_person_mapping = _unify_note_mapping(
            dlg.get_values(), self._all_template_columns
        )
        self._lunch_person_mapping.update(
            auto_detect_semantic_columns(self._all_template_columns)
        )
        self._controller.set_lunch_people(
            self._lunch_person_loader.roster_data,
            headers,
            self._lunch_person_mapping,
            self._lunch_person_file_path,
        )
        self._settings.setValue(
            "lunch_roster/col_mapping", self._lunch_person_mapping
        )
        self._settings.sync()

    # ── 학교별 방학 일정 파일 로드 ──

    def _configure_lunch_file(
        self, path: str, preferred_sheet: str = "", *, persist: bool = True
    ) -> bool:
        sheet_names = self._schedule_loader.list_sheets(path)
        if sheet_names is None:
            return False
        if not sheet_names:
            QMessageBox.warning(self, "알림", "방학 일정 파일에 시트가 없습니다.")
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
            self._settings.setValue("school_schedule/file_path", path)
            self._settings.setValue(
                "school_schedule/sheet_name", self._lunch_sheet_name
            )
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
        self._settings.setValue("school_schedule/sheet_name", sheet_name)
        self._settings.sync()
        if self._lunch_mapping:
            self._reload_lunch_roster()

    def _select_lunch_file(self):
        """학교별 방학 일정 파일을 선택한다."""
        path, _ = QFileDialog.getOpenFileName(
            self,
            "학교별 방학 일정 파일 선택",
            "",
            "Excel (*.xlsx *.xls *.xlsb *.ods);;모든 파일 (*.*)",
        )
        if path:
            saved_path = str(
                self._settings.value("school_schedule/file_path", "") or ""
            )
            preferred = (
                str(
                    self._settings.value("school_schedule/sheet_name", "") or ""
                )
                if saved_path
                and os.path.normcase(os.path.abspath(saved_path))
                == os.path.normcase(os.path.abspath(path))
                else ""
            )
            if not self._configure_lunch_file(path, preferred):
                return
            # 저장된 매핑 있으면 자동 로드, 없으면 매핑 버튼 눌러야 함
            self._lunch_mapping = {}
            saved_mapping = self._settings.value(
                "school_schedule/col_mapping", None
            )
            if saved_mapping and isinstance(saved_mapping, dict):
                self._lunch_mapping = saved_mapping
                self._reload_lunch_roster()

    def _open_lunch_mapping(self):
        """학교별 방학 일정의 세 컬럼 매핑 다이얼로그를 연다."""
        if not self._lunch_file_path:
            QMessageBox.warning(self, "알림", "먼저 방학 일정 파일을 불러오세요.")
            return

        loaded = self._schedule_loader.load(
            self._lunch_file_path,
            sheet_name=self._lunch_sheet_name or None,
            name_keywords=["학교명", "학교"],
        )
        if not loaded:
            QMessageBox.warning(
                self, "알림", "선택한 일정 시트에서 데이터를 읽을 수 없습니다."
            )
            return
        lunch_headers, lunch_data = loaded

        dlg = LunchMappingDialog(lunch_headers, self)
        dlg._reset_defaults()

        saved = self._settings.value("school_schedule/col_mapping", None)
        if saved and isinstance(saved, dict):
            dlg.set_values(_sanitize_column_mapping(saved, lunch_headers))

        if not dlg.exec():
            return

        self._lunch_mapping = dlg.get_values()

        roster = _build_lunch_roster(
            lunch_headers,
            lunch_data,
            self._lunch_mapping,
            self._lunch_file_path,
            self.year_spin.value(),
        )
        if lunch_data and not roster.entries:
            QMessageBox.warning(
                self,
                "일정 확인",
                (
                    "선택한 컬럼에서 유효한 학교 일정을 찾지 못했습니다.\n"
                    "학교명·시작일·종료일의 값과 날짜 형식을 확인하세요."
                ),
            )
            return

        self._lunch_roster = roster
        self._controller.set_lunch_roster(
            roster, self._lunch_mapping, self._lunch_file_path
        )

        self._settings.setValue(
            "school_schedule/col_mapping", self._lunch_mapping
        )
        self._settings.setValue("school_schedule/file_path", self._lunch_file_path)
        self._settings.setValue(
            "school_schedule/sheet_name", self._lunch_sheet_name
        )
        self._settings.sync()

        log.info(
            "[Lunch] 학교별 방학 일정 %d개 로드 완료 (파일: %s)",
            len(roster.entries),
            os.path.basename(self._lunch_file_path),
        )
        QMessageBox.information(
            self,
            "로드 완료",
            f"학교별 방학 일정 {len(roster.entries)}개를 불러왔습니다.",
        )

    def _reload_lunch_roster(self) -> bool:
        """저장된 매핑으로 중식명단 재로드 (프로그램 재시작 시 자동 복원)."""
        if not self._lunch_file_path:
            log.debug("[Lunch] 재로드 건너뜀: lunch_file_path 없음")
            return False
        if not os.path.exists(self._lunch_file_path):
            log.warning("[Lunch] 재로드 실패: 파일 없음 — %s", self._lunch_file_path)
            return False
        if not self._lunch_mapping:
            log.debug("[Lunch] 재로드 건너뜀: lunch_mapping 없음")
            return False

        loaded = self._schedule_loader.load(
            self._lunch_file_path,
            sheet_name=self._lunch_sheet_name or None,
            name_keywords=["학교명", "학교"],
        )
        if not loaded:
            log.warning("[Lunch] 재로드 실패: 선택 시트 데이터 없음")
            return False
        lunch_headers, lunch_data = loaded
        self._lunch_mapping = _sanitize_column_mapping(
            self._lunch_mapping, lunch_headers
        )
        if not _has_required_mapping(
            self._lunch_mapping, lunch_headers, _SCHEDULE_REQUIRED_KEYS
        ):
            self._lunch_roster = None
            self._controller.set_lunch_roster(None, {}, self._lunch_file_path)
            log.warning("[Lunch] 재로드 중단: 현재 시트와 저장된 컬럼 매핑이 다름")
            return False

        roster = _build_lunch_roster(
            lunch_headers,
            lunch_data,
            self._lunch_mapping,
            self._lunch_file_path,
            self.year_spin.value(),
        )
        if lunch_data and not roster.entries:
            self._lunch_roster = None
            self._controller.set_lunch_roster(None, {}, self._lunch_file_path)
            log.warning("[Lunch] 재로드 중단: 유효한 학교 일정이 0건")
            return False

        self._lunch_roster = roster
        self._controller.set_lunch_roster(
            roster, self._lunch_mapping, self._lunch_file_path
        )
        log.info(
            "[Lunch] 재로드 완료: %d개 학교 (파일: %s)",
            len(roster.entries),
            os.path.basename(self._lunch_file_path),
        )
        return True

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
        self.provider_name_edit.blockSignals(True)

        # 컬럼 매핑
        mapping = self._settings.value("invoice/col_mapping", None)
        mapping_is_dict = isinstance(mapping, dict)
        if mapping_is_dict:
            mapping = _unify_note_mapping(mapping, self._all_template_columns)
        saved_source_keys = (
            *self._source_mapping_columns,
            "__breakfast",
            "__dinner",
            "__note",
        )
        has_saved_mapping = mapping_is_dict and any(
            str(mapping.get(key, "") or "").strip() for key in saved_source_keys
        )
        if mapping_is_dict:
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
        provider_name = self._settings.value("invoice/provider_name", None)
        if provider_name is not None:
            self.provider_name_edit.setText(str(provider_name))

        self.year_spin.blockSignals(False)
        self.month_spin.blockSignals(False)
        self.breakfast_price.blockSignals(False)
        self.dinner_price.blockSignals(False)
        self.lunch_price.blockSignals(False)
        self.provider_name_edit.blockSignals(False)

        # 중식 대상자 명단 매핑/경로 복원 (로드는 post_init_load 에서)
        lunch_person_mapping = self._settings.value(
            "lunch_roster/col_mapping", None
        )
        if lunch_person_mapping and isinstance(lunch_person_mapping, dict):
            self._lunch_person_mapping = _unify_note_mapping(
                lunch_person_mapping, self._all_template_columns
            )
        lunch_person_file = self._settings.value("lunch_roster/file_path", "")
        if lunch_person_file and os.path.exists(lunch_person_file):
            self._lunch_person_file_path = str(lunch_person_file)
            self._lunch_person_sheet_name = str(
                self._settings.value("lunch_roster/sheet_name", "") or ""
            )
            self.lunch_person_file_label.setText(
                os.path.basename(str(lunch_person_file))
            )
            self.lunch_person_file_label.setStyleSheet("color: #000;")
            self.lunch_person_mapping_btn.setEnabled(True)

        # 학교별 방학 일정 복원. 예전 lunch/* 키는 한 번 호환해서 읽는다.
        lunch_mapping = self._settings.value("school_schedule/col_mapping", None)
        if not lunch_mapping:
            lunch_mapping = self._settings.value("lunch/col_mapping", None)
        if lunch_mapping and isinstance(lunch_mapping, dict):
            self._lunch_mapping = lunch_mapping
        lunch_file = self._settings.value("school_schedule/file_path", "")
        if not lunch_file:
            lunch_file = self._settings.value("lunch/file_path", "")
        if lunch_file and os.path.exists(lunch_file):
            self._lunch_file_path = lunch_file
            self._lunch_sheet_name = str(
                self._settings.value(
                    "school_schedule/sheet_name",
                    self._settings.value("lunch/sheet_name", ""),
                )
                or ""
            )
            self._lunch_enabled = True
            self.lunch_file_label.setText(os.path.basename(lunch_file))
            self.lunch_file_label.setStyleSheet("color: #000;")
            self.lunch_mapping_btn.setEnabled(True)

        # ── 캐시된 InvoiceData 복원 (청구서 재생성 없이 확인서 탭 사용 가능) ──
        cached = self._load_cached_invoice_data()
        if cached and cached.invoices:
            self._controller.invoices = cached.invoices
            # 결과 캐시는 과거 청구서를 다시 보여주기 위한 데이터다.
            # 사용자가 확정한 영구 매핑이 있으면 캐시의 옛 매핑으로 덮지 않는다.
            if not has_saved_mapping:
                self._controller.set_col_mapping(
                    _unify_note_mapping(
                        cached.col_mapping, self._all_template_columns
                    )
                )
            self._controller._year = cached.year
            self._controller._month = cached.month
            self._controller.set_excluded_dates(cached.excluded_dates)
            self._controller.roster_headers = cached.roster_headers
            self._controller.roster_path = cached.roster_path
            self._update_result(cached.invoices)
            self._update_exclude_label()

        # 캐시 fallback까지 반영한 최종 매핑을 다음 실행에도 유지한다.
        self._save_settings()

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

        # 중식 대상자 명단 자동 재로드
        if self._lunch_person_file_path:
            self._load_lunch_person_path(
                self._lunch_person_file_path,
                self._lunch_person_sheet_name,
            )

        # 학교별 방학 일정 자동 재로드
        lunch_mapping = self._settings.value(
            "school_schedule/col_mapping", None
        )
        if not lunch_mapping:
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
            self._lunch_mapping = lunch_mapping
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
            "invoice/provider_name", self.provider_name_edit.text().strip()
        )
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
            self._controller.get_roster_headers(),
            self._source_mapping_columns,
            self,
            title="조식·석식 명단 컬럼 매핑",
            flag_keys=("__breakfast", "__dinner", "__note"),
            required_keys=("성명", "생년월일"),
        )
        dlg._reset_defaults()
        dlg.set_values(self._controller.get_col_mapping())
        if dlg.exec():
            mapping = _unify_note_mapping(
                dlg.get_values(), self._all_template_columns
            )
            _sem = auto_detect_semantic_columns(self._all_template_columns)
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
            has_lunch_file=bool(self._lunch_person_loader.roster_data),
            provider_name=self.provider_name_edit.text().strip(),
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
            self.result_table.setItem(
                i, 0, QTableWidgetItem(str(p.get("성명", p.get(name_col, ""))))
            )
            self.result_table.setItem(
                i, 1, QTableWidgetItem(str(p.get("학교명", p.get(dept_col, ""))))
            )
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
        self._lunch_person_loader.roster_headers = []
        self._lunch_person_loader.roster_data = []
        self._lunch_person_mapping = {}
        self._lunch_person_file_path = ""
        self._lunch_person_sheet_name = ""
        self._controller.set_lunch_people([], [], {}, "")
        self._populate_sheet_combo(self.lunch_person_sheet_combo, [])
        self.lunch_person_file_label.setText("불러오지 않음")
        self.lunch_person_file_label.setStyleSheet("color: #888;")
        self.lunch_person_mapping_btn.setEnabled(False)
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
        self._update_generate_enabled()

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

    def get_daily_delivery_data(
        self, year: int | None = None, month: int | None = None
    ) -> InvoiceData:
        """청구서를 만들지 않아도 현재 명단으로 일일배송 데이터를 만든다."""

        target_year = year if year is not None else self.get_year()
        target_month = month if month is not None else self.get_month()
        mapping = self._daily_delivery_mapping()
        prepared = self._controller.prepare_daily_delivery_people(
            self._loader.roster_data,
            target_year,
            target_month,
            col_mapping=mapping,
        )

        return InvoiceData(
            invoices=prepared.enriched if prepared.success else [],
            col_mapping=mapping,
            year=target_year,
            month=target_month,
            excluded_dates=self.get_excluded_dates(),
            roster_headers=self.get_roster_headers(),
            roster_path=self.get_roster_path(),
            preparation_error=prepared.error,
            preparation_warnings=[warning.message for warning in prepared.warnings],
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
    """예전 호출부용 일정 파일 읽기 헬퍼 (UI에서는 RosterLoader 사용)."""

    ext = os.path.splitext(file_path)[1].lower()
    if ext not in (".xlsx", ".xls", ".xlsb", ".ods"):
        return None
    try:
        rows = FileReader.read_excel(file_path, sheet_name=sheet_name)
    except Exception:  # noqa: BLE001 - 호환 헬퍼는 읽기 실패를 None으로 반환
        return None
    if not rows:
        return None

    first_cell = str(rows[0][0]).strip() if rows[0] else ""
    header_index = 1 if first_cell.startswith("■") or len(first_cell) > 30 else 0
    if header_index >= len(rows):
        return None

    raw_headers = [str(value).replace("\n", " ").strip() for value in rows[header_index]]
    headers: list[str] = []
    seen: dict[str, int] = {}
    for raw_header in raw_headers:
        header = raw_header or (
            chr(65 + len(headers)) if len(headers) < 26 else f"Col{len(headers)}"
        )
        seen[header] = seen.get(header, 0) + 1
        if seen[header] > 1:
            header = f"{header}_{seen[header]}"
        headers.append(header)

    data: list[dict] = []
    for row in rows[header_index + 1 :]:
        values = [str(value).strip() for value in row]
        if all(not value or value == " " for value in values):
            continue
        data.append(
            {
                header: values[index] if index < len(values) else ""
                for index, header in enumerate(headers)
            }
        )
    return headers, data


def _build_lunch_roster(
    lunch_headers: list[str],
    lunch_data: list[dict],
    lunch_mapping: dict,
    lunch_file_path: str,
    _ref_year: int,
) -> LunchRoster:
    """학교명·시작일·종료일 세 컬럼으로 학교별 방학 일정을 만든다.

    _open_lunch_mapping()과 _reload_lunch_roster()에서 중복 코드 제거.
    """
    lunch_school_col = lunch_mapping.get("school_name_col", "") or lunch_mapping.get(
        "lunch_school_name", ""
    )
    lunch_start_col = lunch_mapping.get("start_date_col", "") or lunch_mapping.get(
        "lunch_vacation_date", ""
    )
    lunch_end_col = lunch_mapping.get("end_date_col", "") or lunch_mapping.get(
        "lunch_opening_date", ""
    )

    roster = LunchRoster(headers=lunch_headers, file_path=lunch_file_path)
    for row in lunch_data:
        school_name = str(row.get(lunch_school_col, "")).strip()
        if not school_name or school_name in ("학교명", "구분"):
            continue

        start_date = _parse_lunch_date(row.get(lunch_start_col, ""))
        end_date = _parse_lunch_date(row.get(lunch_end_col, ""))
        if start_date is None or end_date is None or start_date > end_date:
            log.warning(
                "[Lunch] 잘못된 방학 일정 건너뜀: %s (%s~%s)",
                school_name,
                start_date,
                end_date,
            )
            continue

        entry = LunchSchoolEntry(
            school_name=school_name,
            start_date=start_date,
            end_date=end_date,
        )
        roster.entries.append(entry)

        log.debug(
            "[Lunch] %s: start=%s end=%s",
            school_name,
            start_date,
            end_date,
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
