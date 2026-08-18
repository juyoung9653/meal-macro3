"""연간 배송 데이터 기반 청구서 생성 탭."""

import os
from datetime import datetime, timezone
from typing import ClassVar

from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QComboBox,
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

from ..core.delivery_matrix import AnnualDeliveryBook, MonthState
from ..core.invoice_data import InvoiceData, MonthlyDeliveryBatch
from ..core.models import FULL_ADDRESS_FIELD
from ..core.settings import get_settings
from ..core.user_preferences import UserPreferences
from ..services.invoice_controller import InvoiceGenerationConfig, InvoiceService
from ..templates.config import auto_detect_semantic_columns, read_template_columns


class InvoiceTab(QWidget):
    """연간 배송 파일에서 대상 월을 선택해 청구서를 생성한다."""

    MAX_RESULT_ROWS = 10
    _MONTH_STATE_TEXT: ClassVar[dict[MonthState, str]] = {
        MonthState.ENTERED: "ENTERED (입력됨)",
        MonthState.UNENTERED: "UNENTERED (미입력)",
        MonthState.MISSING: "MISSING (없음)",
    }

    def __init__(self, meal_type: str = ""):
        super().__init__()
        self._s = get_settings()
        self._meal_type = meal_type or self._s.get_meal_type()
        self._template_path = self._s.get_invoice_template_path(self._meal_type)
        self._template_columns = read_template_columns(
            self._template_path, meal_type=self._meal_type
        )
        self._service = InvoiceService()
        self._book: AnnualDeliveryBook | None = None
        self._batch: MonthlyDeliveryBatch | None = None
        self.output_dir: str | None = None
        self.roster_headers: list[str] = []
        self._col_mapping = self._new_col_mapping()
        self._year = 0
        self._month = 0
        self._result_year = 0
        self._result_month = 0
        self._preferences = UserPreferences(self._s)
        self._last_output_path: str | None = None
        self._setup_ui()
        self._restore_ui_state()

    # ── UI ──

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        config_group = QGroupBox("① 청구 기준 (db 파일 / 선택 월)")
        config_row = QHBoxLayout(config_group)

        config_row.addWidget(QLabel("귀속 연월:"))
        self.year_label = QLabel("----")
        self.year_label.setStyleSheet("font-weight: bold; font-size: 14px;")
        config_row.addWidget(self.year_label)
        config_row.addWidget(QLabel("년"))

        self.month_combo = QComboBox()
        self.month_combo.setEnabled(False)
        self.month_combo.setMinimumWidth(80)
        self.month_combo.currentIndexChanged.connect(self._on_month_changed)
        config_row.addWidget(self.month_combo)
        config_row.addSpacing(20)

        self.month_state_label = QLabel()
        config_row.addWidget(self.month_state_label)
        config_row.addStretch()
        layout.addWidget(config_group)
        self._update_month_state_label(MonthState.MISSING)

        path_group = QGroupBox("② 출력 경로")
        path_layout = QHBoxLayout(path_group)
        self.path_label = QLabel("선택된 경로 없음")
        self.path_label.setStyleSheet("color: #888;")
        self.path_btn = QPushButton("폴더 선택")
        self.path_btn.clicked.connect(self._select_output_dir)
        path_layout.addWidget(self.path_label, stretch=1)
        path_layout.addWidget(self.path_btn)
        layout.addWidget(path_group)

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

        table_group = QGroupBox("③ 생성 결과")
        table_layout = QVBoxLayout(table_group)
        self.result_table = QTableWidget()
        self.result_table.setColumnCount(4)
        self.result_table.setHorizontalHeaderLabels(
            ["이름", "소속", "총급식수", "청구금액"]
        )
        result_header = self.result_table.horizontalHeader()
        if result_header is not None:
            result_header.setStretchLastSection(True)
            result_header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.result_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.result_table.setAlternatingRowColors(True)
        table_layout.addWidget(self.result_table)
        self.info_label = QLabel("db 파일을 불러오세요.")
        self.info_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        table_layout.addWidget(self.info_label)
        layout.addWidget(table_group, stretch=1)

    def _on_month_changed(self, index: int):
        raw_month = self.month_combo.itemData(index) if index >= 0 else None
        self._month = int(raw_month) if raw_month is not None else 0
        self._year = self._book.year if self._book is not None else 0
        self._update_month_state_label(self._selected_month_state())
        self._invalidate_result(self._selection_info_text())
        self._update_generate_enabled()

    def _selected_month_state(self) -> MonthState:
        if self._book is None or not self._month:
            return MonthState.MISSING
        return self._book.month_state(self._book.year, self._month)

    def _update_month_state_label(self, state: MonthState):
        self.month_state_label.setText(
            f"월 상태: {self._MONTH_STATE_TEXT[state]}"
        )
        colors = {
            MonthState.ENTERED: "#080",
            MonthState.UNENTERED: "#b36b00",
            MonthState.MISSING: "#888",
        }
        self.month_state_label.setStyleSheet(
            f"font-weight: bold; color: {colors[state]};"
        )

    def _update_generate_enabled(self):
        self.generate_btn.setEnabled(self._book is not None and self._month > 0)

    def _selection_info_text(self) -> str:
        if self._book is None:
            return "db 파일을 불러오세요."
        if not self._month:
            return f"{self._book.year}년 db 파일에 선택 가능한 월이 없습니다."
        return (
            f"db 파일 명단 {len(self._book.roster)}명 · "
            f"{self._book.year}년 {self._month}월 선택"
        )

    # ── 설정 저장/복원 ──

    def _new_col_mapping(self) -> dict[str, str]:
        mapping = {column: "" for column in self._template_columns}
        mapping.update(auto_detect_semantic_columns(self._template_columns))
        return mapping

    def _restore_ui_state(self):
        output_dir = self._preferences.get_output_dir()
        if output_dir:
            self.output_dir = str(output_dir)
            self.path_label.setText(self.output_dir)
            self.path_label.setStyleSheet("color: #000;")

        mapping = self._preferences.get_invoice_column_mapping()
        if mapping:
            self._col_mapping = {
                key: mapping.get(key, value)
                for key, value in self._col_mapping.items()
            }

    def _save_settings(self):
        self._preferences.set_invoice_column_mapping(self._col_mapping)
        self._preferences.set_output_dir(self.output_dir or "")

    def _select_output_dir(self):
        selected = QFileDialog.getExistingDirectory(self, "출력 폴더 선택")
        if selected:
            self.output_dir = selected
            self.path_label.setText(selected)
            self.path_label.setStyleSheet("color: #000;")
            self._save_settings()

    # ── 청구서 생성 ──

    def _generate_invoices(self):
        if self._book is None or not self._year or not self._month:
            QMessageBox.warning(
                self, "알림", "db 파일을 불러오고 청구 대상 월을 선택하세요."
            )
            return

        if not self.output_dir:
            QMessageBox.warning(self, "알림", "출력 경로를 먼저 선택하세요.")
            return

        self._save_settings()
        filename = self._s.get("invoice", "output_filename_format").format(
            year=self._year, month=self._month
        )
        output_path = os.path.join(self.output_dir, filename)

        config = InvoiceGenerationConfig(
            template_path=self._template_path,
            output_path=output_path,
            year=self._year,
            month=self._month,
            annual_book=self._book,
            col_mapping=self._col_mapping,
            meal_type=self._meal_type,
        )
        result = self._service.generate(config)

        for warning in result.warnings:
            QMessageBox.warning(self, warning.code, warning.message)

        if not result.success:
            if result.error:
                QMessageBox.critical(self, "오류", result.error)
            return

        batch = result.batch
        if batch is None:
            QMessageBox.critical(self, "오류", "청구서 생성 결과 배치가 없습니다.")
            return
        if result.count != len(batch.deliveries):
            QMessageBox.critical(
                self,
                "오류",
                "청구서 생성 결과 건수가 일치하지 않습니다: "
                f"{result.count}건 / {len(batch.deliveries)}명",
            )
            return

        self._batch = batch
        self._result_year = batch.year
        self._result_month = batch.month
        self._last_output_path = output_path
        self.open_file_btn.setEnabled(True)
        self._update_result(batch)

        QMessageBox.information(
            self,
            "완료",
            f"청구서 {len(batch.deliveries)}건 생성 완료!\n{output_path}",
        )

    @staticmethod
    def _display_text(value: object) -> str:
        return "" if value is None else str(value)

    def _update_result(self, batch: MonthlyDeliveryBatch):
        show = batch.deliveries[: self.MAX_RESULT_ROWS]
        self.result_table.setRowCount(len(show))
        name_col = self._col_mapping.get("성명") or "성명"
        department_col = self._col_mapping.get("학교명") or "학교명"
        for row, person in enumerate(show):
            values = (
                person.roster.extra.get(name_col, ""),
                person.roster.extra.get(department_col, ""),
                person.total_count,
                f"{person.amount(batch.prices):,}원",
            )
            for column, value in enumerate(values):
                self.result_table.setItem(
                    row, column, QTableWidgetItem(self._display_text(value))
                )
        self.info_label.setText(
            f"생성된 청구서: {len(batch.deliveries)}건 (처음 {len(show)}건 표시)"
        )

    def _open_output_file(self):
        if self._last_output_path and os.path.exists(self._last_output_path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(self._last_output_path))
        else:
            QMessageBox.warning(
                self, "알림", "열 파일이 없습니다. 먼저 청구서를 생성하세요."
            )

    def _invalidate_result(self, info_text: str | None = None):
        self._batch = None
        self._result_year = 0
        self._result_month = 0
        self.result_table.setRowCount(0)
        self._last_output_path = None
        self.open_file_btn.setEnabled(False)
        if info_text is not None:
            self.info_label.setText(info_text)

    def _clear(self):
        self._invalidate_result(self._selection_info_text())
        self._update_generate_enabled()

    def _auto_map_columns(self):
        """템플릿 컬럼을 현재 명단 컬럼에 맞추고 오래된 매핑은 버린다."""
        roster_normalized = {
            self._normalize_header(header): header for header in self.roster_headers
        }
        valid_headers = set(self.roster_headers)
        for template_column in self._col_mapping:
            if template_column.startswith("__"):
                continue
            source_column = self._col_mapping[template_column]
            if source_column and source_column not in valid_headers:
                self._col_mapping[template_column] = ""
            normalized = self._normalize_header(template_column)
            if normalized in roster_normalized:
                self._col_mapping[template_column] = roster_normalized[normalized]

        fallback_map = {
            "취학유형(미취학/초/중/고/기타)": "취학유형",
            "주민등록번호": "생년월일",
            "구분": "취학유형",
            "주소": FULL_ADDRESS_FIELD,
        }
        for template_column, roster_column in fallback_map.items():
            if template_column not in self._col_mapping:
                continue
            if self._col_mapping[template_column]:
                continue
            normalized = self._normalize_header(roster_column)
            if normalized in roster_normalized:
                self._col_mapping[template_column] = roster_normalized[normalized]

    @staticmethod
    def _normalize_header(header: str) -> str:
        return header.replace(" ", "").replace("\n", "")

    # ── 외부 접근 ──

    def set_annual_book(self, book: AnnualDeliveryBook | None):
        """연간 배송 책을 설정하고 기본 청구 월을 선택한다."""
        self._book = book
        self.roster_headers = (
            list(book.roster[0].as_dict().keys())
            if book is not None and book.roster
            else []
        )
        if (
            {"도로명주소", "상세주소"} & set(self.roster_headers)
            and FULL_ADDRESS_FIELD not in self.roster_headers
        ):
            self.roster_headers.append(FULL_ADDRESS_FIELD)
        self._auto_map_columns()

        self.month_combo.blockSignals(True)
        self.month_combo.clear()
        self._year = book.year if book is not None else 0
        self._month = 0

        months = sorted(book.available_months) if book is not None else []
        for month in months:
            self.month_combo.addItem(f"{month}월", month)

        selected_month = self._default_month(book, months)
        if selected_month is not None:
            self.month_combo.setCurrentIndex(self.month_combo.findData(selected_month))
        self.month_combo.blockSignals(False)
        self.month_combo.setEnabled(bool(months))

        self.year_label.setText(str(book.year) if book is not None else "----")
        self.year_label.setStyleSheet(
            "font-weight: bold; font-size: 14px;"
            + (" color: #080;" if book is not None else "")
        )
        self._on_month_changed(self.month_combo.currentIndex())
        self._save_settings()

    @staticmethod
    def _default_month(
        book: AnnualDeliveryBook | None, months: list[int]
    ) -> int | None:
        if book is None or not months:
            return None

        today = datetime.now(timezone.utc).astimezone().date()
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

    def set_output_dir(self, path: str):
        """외부에서 출력 경로 설정 (탭 간 공유용)."""
        if path and path != self.output_dir:
            self.output_dir = path
            self.path_label.setText(path)
            self.path_label.setStyleSheet("color: #000;")
            self._save_settings()

    def set_meal_type(self, meal_type: str):
        """식사 유형 변경 시 템플릿과 매핑을 갱신한다."""
        if meal_type == self._meal_type:
            return
        self._meal_type = meal_type
        self._template_path = self._s.get_invoice_template_path(self._meal_type)
        self._template_columns = read_template_columns(
            self._template_path, meal_type=self._meal_type
        )
        self._col_mapping = self._new_col_mapping()
        self._auto_map_columns()
        self._invalidate_result(self._selection_info_text())
        self._update_generate_enabled()
        self._save_settings()

    def get_invoice_data(self):
        return InvoiceData(batch=self._batch, col_mapping=dict(self._col_mapping))
