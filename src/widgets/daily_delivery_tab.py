"""일일배송 탭 — 날짜 범위 선택과 날짜별 배송표 생성 UI."""

from __future__ import annotations

import calendar
import json
import os
from datetime import date, datetime

from PyQt6.QtCore import QDate, QSettings, Qt, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateEdit,
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
from ..generators.daily_delivery_generator import (
    DailyDeliverySheet,
    make_daily_delivery,
    prepare_daily_delivery_people,
    select_delivery_dates,
)
from ..services.delivery_calculator import to_bool


class DailyDeliveryTab(QWidget):
    """명단의 사용자 선택 컬럼을 날짜별 배송표로 변환한다."""

    def __init__(self):
        super().__init__()
        self._s = get_settings()
        self._settings = QSettings(
            self._s.get_app_organization(), self._s.get_app_title()
        )
        self._data = InvoiceData()
        self.deliveries: list[DailyDeliverySheet] = []
        self.output_dir: str = ""
        self._last_output_path: str | None = None
        self._has_saved_dates = bool(self._settings.value("daily/start_date", ""))
        self._setup_ui()
        self._restore_state()

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        data_group = QGroupBox("① 명단 데이터")
        data_layout = QVBoxLayout(data_group)
        self.data_label = QLabel(
            "명단 데이터가 없습니다. 청구서 탭에서 명단 파일을 불러와 주세요."
        )
        self.data_label.setStyleSheet("color: #888;")
        data_layout.addWidget(self.data_label)

        mapping_row_1 = QHBoxLayout()
        mapping_row_1.addWidget(QLabel("배송 담당 컬럼:"))
        self.driver_combo = QComboBox()
        self.driver_combo.setMinimumWidth(180)
        self.driver_combo.currentIndexChanged.connect(self._save_settings)
        mapping_row_1.addWidget(self.driver_combo)
        mapping_row_1.addSpacing(20)
        mapping_row_1.addWidget(QLabel("담당 구역 1순위 컬럼:"))
        self.region_primary_combo = QComboBox()
        self.region_primary_combo.setMinimumWidth(180)
        self.region_primary_combo.currentIndexChanged.connect(self._save_settings)
        mapping_row_1.addWidget(self.region_primary_combo)
        mapping_row_1.addStretch()
        data_layout.addLayout(mapping_row_1)

        mapping_row_2 = QHBoxLayout()
        mapping_row_2.addWidget(QLabel("담당 구역 2순위 컬럼(선택):"))
        self.region_secondary_combo = QComboBox()
        self.region_secondary_combo.setMinimumWidth(180)
        self.region_secondary_combo.currentIndexChanged.connect(self._save_settings)
        mapping_row_2.addWidget(self.region_secondary_combo)
        mapping_row_2.addSpacing(20)
        mapping_row_2.addWidget(QLabel("세대 구분(주소) 컬럼:"))
        self.address_combo = QComboBox()
        self.address_combo.setMinimumWidth(180)
        self.address_combo.currentIndexChanged.connect(self._save_settings)
        mapping_row_2.addWidget(self.address_combo)
        mapping_row_2.addStretch()
        data_layout.addLayout(mapping_row_2)

        meal_row = QHBoxLayout()
        meal_row.addWidget(QLabel("조식 여부 컬럼:"))
        self.breakfast_combo = QComboBox()
        self.breakfast_combo.setMinimumWidth(180)
        self.breakfast_combo.currentIndexChanged.connect(
            self._on_meal_column_changed
        )
        meal_row.addWidget(self.breakfast_combo)
        meal_row.addSpacing(20)
        meal_row.addWidget(QLabel("석식 여부 컬럼:"))
        self.dinner_combo = QComboBox()
        self.dinner_combo.setMinimumWidth(180)
        self.dinner_combo.currentIndexChanged.connect(self._on_meal_column_changed)
        meal_row.addWidget(self.dinner_combo)
        meal_row.addStretch()
        data_layout.addLayout(meal_row)

        note_row = QHBoxLayout()
        note_row.addWidget(QLabel("비고 컬럼(선택):"))
        self.note_combo = QComboBox()
        self.note_combo.setMinimumWidth(180)
        self.note_combo.currentIndexChanged.connect(self._save_settings)
        note_row.addWidget(self.note_combo)
        note_row.addSpacing(20)
        self.meal_summary_label = QLabel("조식·석식 컬럼을 선택하세요.")
        self.meal_summary_label.setStyleSheet("color: #555;")
        note_row.addWidget(self.meal_summary_label)
        note_row.addStretch()
        data_layout.addLayout(note_row)

        priority_guide = QLabel(
            "1순위만 선택해도 됩니다. 2순위를 선택하면 1순위가 공란인 행에만 사용합니다."
        )
        priority_guide.setStyleSheet("color: #555;")
        data_layout.addWidget(priority_guide)
        layout.addWidget(data_group)

        date_group = QGroupBox("② 생성할 날짜 범위")
        date_layout = QHBoxLayout(date_group)
        date_layout.addWidget(QLabel("시작일:"))
        self.start_date = QDateEdit()
        self.start_date.setCalendarPopup(True)
        self.start_date.setDisplayFormat("yyyy-MM-dd")
        self.start_date.dateChanged.connect(self._on_start_date_changed)
        date_layout.addWidget(self.start_date)
        date_layout.addWidget(QLabel("~"))
        date_layout.addWidget(QLabel("종료일:"))
        self.end_date = QDateEdit()
        self.end_date.setCalendarPopup(True)
        self.end_date.setDisplayFormat("yyyy-MM-dd")
        self.end_date.dateChanged.connect(self._save_settings)
        date_layout.addWidget(self.end_date)
        date_layout.addSpacing(24)
        self.business_days_only = QCheckBox(
            "토·일, 공휴일, 청구서 배송 제외일 건너뛰기"
        )
        self.business_days_only.setChecked(True)
        self.business_days_only.toggled.connect(self._save_settings)
        date_layout.addWidget(self.business_days_only)
        date_layout.addStretch()
        layout.addWidget(date_group)

        path_group = QGroupBox("③ 출력 경로")
        path_layout = QHBoxLayout(path_group)
        self.path_label = QLabel("선택된 경로 없음")
        self.path_label.setStyleSheet("color: #888;")
        self.path_btn = QPushButton("폴더 선택")
        self.path_btn.clicked.connect(self._select_output_dir)
        path_layout.addWidget(self.path_label, stretch=1)
        path_layout.addWidget(self.path_btn)
        layout.addWidget(path_group)

        button_layout = QHBoxLayout()
        self.generate_btn = QPushButton("일일배송 생성")
        self.generate_btn.setEnabled(False)
        self.generate_btn.clicked.connect(self._generate)
        self.open_file_btn = QPushButton("파일 열기")
        self.open_file_btn.setEnabled(False)
        self.open_file_btn.clicked.connect(self._open_output_file)
        self.clear_btn = QPushButton("결과 지우기")
        self.clear_btn.clicked.connect(self._clear)
        button_layout.addStretch()
        button_layout.addWidget(self.generate_btn)
        button_layout.addWidget(self.open_file_btn)
        button_layout.addWidget(self.clear_btn)
        layout.addLayout(button_layout)

        result_group = QGroupBox("생성 결과")
        result_layout = QVBoxLayout(result_group)
        self.result_table = QTableWidget()
        self.result_table.setColumnCount(5)
        self.result_table.setHorizontalHeaderLabels(
            ["날짜", "요일", "조식 합계", "석식 합계", "미분류 인원"]
        )
        self.result_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self.result_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.result_table.setAlternatingRowColors(True)
        result_layout.addWidget(self.result_table)
        self.info_label = QLabel("생성된 날짜 시트: 0개")
        self.info_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        result_layout.addWidget(self.info_label)
        layout.addWidget(result_group, stretch=1)

    @staticmethod
    def _to_qdate(value: date) -> QDate:
        return QDate(value.year, value.month, value.day)

    @staticmethod
    def _to_date(value: QDate) -> date:
        return date(value.year(), value.month(), value.day())

    def _restore_state(self):
        today = datetime.now().astimezone().date()
        start = date(today.year, today.month, 1)
        end = today
        saved_start = self._settings.value("daily/start_date", "")
        saved_end = self._settings.value("daily/end_date", "")
        try:
            if saved_start:
                start = date.fromisoformat(str(saved_start))
            if saved_end:
                end = date.fromisoformat(str(saved_end))
        except ValueError:
            pass
        end = max(end, start)

        self.start_date.blockSignals(True)
        self.end_date.blockSignals(True)
        self.start_date.setDate(self._to_qdate(start))
        self.end_date.setMinimumDate(self._to_qdate(start))
        self.end_date.setDate(self._to_qdate(end))
        self.start_date.blockSignals(False)
        self.end_date.blockSignals(False)

        checked = self._settings.value("daily/business_days_only", True, type=bool)
        self.business_days_only.setChecked(checked)

        out = self._settings.value("daily/output_dir", "") or self._settings.value(
            "invoice/output_dir", ""
        )
        if out:
            self.output_dir = str(out)
            self.path_label.setText(self.output_dir)
            self.path_label.setStyleSheet("color: #000;")

    def _save_settings(self, *_args):
        if not hasattr(self, "start_date"):
            return
        self._settings.setValue(
            "daily/start_date", self._to_date(self.start_date.date()).isoformat()
        )
        self._settings.setValue(
            "daily/end_date", self._to_date(self.end_date.date()).isoformat()
        )
        self._settings.setValue(
            "daily/business_days_only", self.business_days_only.isChecked()
        )
        self._settings.setValue(
            "daily/driver_col", self.driver_combo.currentData() or ""
        )
        self._settings.setValue(
            "daily/region_primary_col",
            self.region_primary_combo.currentData() or "",
        )
        self._settings.setValue(
            "daily/region_secondary_col",
            self.region_secondary_combo.currentData() or "",
        )
        self._settings.setValue(
            "daily/address_col", self.address_combo.currentData() or ""
        )
        self._settings.setValue(
            "daily/breakfast_col", self.breakfast_combo.currentData() or ""
        )
        self._settings.setValue(
            "daily/dinner_col", self.dinner_combo.currentData() or ""
        )
        self._settings.setValue(
            "daily/note_col", self.note_combo.currentData() or ""
        )
        if self.output_dir:
            self._settings.setValue("daily/output_dir", self.output_dir)
        self._settings.sync()

    def _on_meal_column_changed(self, *_args):
        self._save_settings()
        self._update_meal_summary()

    def _update_meal_summary(self):
        if not hasattr(self, "meal_summary_label"):
            return
        breakfast_col = self.breakfast_combo.currentData() or ""
        dinner_col = self.dinner_combo.currentData() or ""
        if not breakfast_col and not dinner_col:
            self.meal_summary_label.setText("조식·석식 중 하나 이상 선택하세요.")
            self.meal_summary_label.setStyleSheet("color: #b00020;")
            return

        breakfast_count = sum(
            to_bool(person.get(breakfast_col, 0))
            for person in self._data.invoices
        )
        dinner_count = sum(
            to_bool(person.get(dinner_col, 0)) for person in self._data.invoices
        )
        self.meal_summary_label.setText(
            f"대상: 조식 {breakfast_count}명 · 석식 {dinner_count}명"
        )
        self.meal_summary_label.setStyleSheet(
            "color: #b00020;" if breakfast_count + dinner_count == 0 else "color: #555;"
        )

    def _on_start_date_changed(self, value: QDate):
        self.end_date.setMinimumDate(value)
        if self.end_date.date() < value:
            self.end_date.setDate(value)
        self._save_settings()

    def _select_output_dir(self):
        selected = QFileDialog.getExistingDirectory(
            self, "출력 폴더 선택", self.output_dir
        )
        if selected:
            self.output_dir = selected
            self.path_label.setText(selected)
            self.path_label.setStyleSheet("color: #000;")
            self._save_settings()

    @staticmethod
    def _find_column(columns: list[str], preferred: list[str]) -> str:
        for name in preferred:
            if name in columns:
                return name
        for name in preferred:
            for column in columns:
                if name in column:
                    return column
        return ""

    def _populate_column_combos(self, data: InvoiceData):
        columns = list(data.roster_headers)
        if not columns and data.invoices:
            columns = [key for key in data.invoices[0] if not key.startswith("_")]

        saved_driver = str(self._settings.value("daily/driver_col", "") or "")
        saved_primary = str(
            self._settings.value("daily/region_primary_col", "") or ""
        )
        saved_secondary = str(
            self._settings.value("daily/region_secondary_col", "") or ""
        )
        saved_address = str(self._settings.value("daily/address_col", "") or "")
        saved_breakfast = str(
            self._settings.value("daily/breakfast_col", "") or ""
        )
        saved_dinner = str(self._settings.value("daily/dinner_col", "") or "")
        saved_note = str(self._settings.value("daily/note_col", "") or "")
        mapped_region = str(data.col_mapping.get("관할동", "") or "")
        mapped_address = str(data.col_mapping.get("주소", "") or "")
        mapped_breakfast = str(data.col_mapping.get("__breakfast", "") or "")
        mapped_dinner = str(data.col_mapping.get("__dinner", "") or "")
        mapped_note = str(data.col_mapping.get("__note", "") or "")

        combos = (
            self.driver_combo,
            self.region_primary_combo,
            self.region_secondary_combo,
            self.address_combo,
            self.breakfast_combo,
            self.dinner_combo,
            self.note_combo,
        )
        for combo in combos:
            combo.blockSignals(True)
            combo.clear()
            combo.addItem(
                "(선택사항)"
                if combo in (self.region_secondary_combo, self.note_combo)
                else "(선택)",
                "",
            )
            for column in columns:
                combo.addItem(column, column)

        driver = (
            saved_driver
            if saved_driver in columns
            else self._find_column(
                columns, ["배송담당", "배송 담당", "담당기사", "배송기사", "기사"]
            )
        )
        primary = (
            saved_primary
            if saved_primary in columns
            else self._find_column(
                columns,
                [
                    "담당 구역 1순위",
                    "담당구역 1순위",
                    "담당구역1순위",
                    "배송 구역 1순위",
                    "배송구역 1순위",
                    "배송구역1순위",
                    "담당구역",
                    "배송구역",
                ],
            )
        )
        secondary = (
            saved_secondary
            if saved_secondary in columns
            else mapped_region
            if mapped_region in columns
            else self._find_column(
                columns,
                [
                    "담당 구역 2순위",
                    "담당구역 2순위",
                    "담당구역2순위",
                    "배송 구역 2순위",
                    "배송구역 2순위",
                    "배송구역2순위",
                    "관할동",
                    "행정동",
                ],
            )
        )
        address = (
            saved_address
            if saved_address in columns
            else mapped_address
            if mapped_address in columns
            else self._find_column(columns, ["주소", "배송지"])
        )
        breakfast = (
            saved_breakfast
            if saved_breakfast in columns
            else mapped_breakfast
            if mapped_breakfast in columns
            else self._find_column(
                columns, ["조식 여부", "조식대상", "조식 대상", "조식"]
            )
        )
        dinner = (
            saved_dinner
            if saved_dinner in columns
            else mapped_dinner
            if mapped_dinner in columns
            else self._find_column(
                columns, ["석식 여부", "석식대상", "석식 대상", "석식"]
            )
        )
        note = (
            saved_note
            if saved_note in columns
            else mapped_note
            if mapped_note in columns
            else self._find_column(columns, ["학기중 비고", "비고", "특이사항"])
        )
        if driver:
            self.driver_combo.setCurrentIndex(self.driver_combo.findData(driver))
        if primary:
            self.region_primary_combo.setCurrentIndex(
                self.region_primary_combo.findData(primary)
            )
        if secondary:
            self.region_secondary_combo.setCurrentIndex(
                self.region_secondary_combo.findData(secondary)
            )
        if address:
            self.address_combo.setCurrentIndex(self.address_combo.findData(address))
        if breakfast:
            self.breakfast_combo.setCurrentIndex(
                self.breakfast_combo.findData(breakfast)
            )
        if dinner:
            self.dinner_combo.setCurrentIndex(self.dinner_combo.findData(dinner))
        if note:
            self.note_combo.setCurrentIndex(self.note_combo.findData(note))
        for combo in combos:
            combo.blockSignals(False)
        self._update_meal_summary()

    def _load_cached_invoice_data(self) -> InvoiceData | None:
        out_dir = self._settings.value("invoice/output_dir", "")
        if not out_dir:
            return None
        cache_path = os.path.join(str(out_dir), ".invoice_data_cache.json")
        if not os.path.exists(cache_path):
            return None
        try:
            with open(cache_path, "r", encoding="utf-8") as file:
                payload = json.load(file)
            return InvoiceData(
                invoices=payload.get("invoices", []),
                col_mapping=payload.get("col_mapping", {}),
                year=payload.get("year", 0),
                month=payload.get("month", 0),
                excluded_dates={
                    date.fromisoformat(value)
                    for value in payload.get("excluded_dates", [])
                },
                roster_headers=payload.get("roster_headers", []),
                roster_path=payload.get("roster_path"),
            )
        except (OSError, json.JSONDecodeError, ValueError):
            return None

    def set_invoice_data(self, data: InvoiceData):
        if not data.invoices:
            cached = self._load_cached_invoice_data()
            if cached and cached.invoices:
                data = cached
        self._data = data
        self._populate_column_combos(data)
        self.generate_btn.setEnabled(bool(data.invoices))

        if data.invoices:
            self.data_label.setText(
                f"명단 데이터 {len(data.invoices)}명 로드됨 — 청구서 생성 없이 일일배송을 만들 수 있습니다."
            )
            self.data_label.setStyleSheet("color: #000;")
        else:
            self.data_label.setText(
                "명단 데이터가 없습니다. 청구서 탭에서 명단 파일을 불러와 주세요."
            )
            self.data_label.setStyleSheet("color: #888;")

        if data.year and data.month and not self._has_saved_dates:
            month_start = date(data.year, data.month, 1)
            month_end = date(
                data.year, data.month, calendar.monthrange(data.year, data.month)[1]
            )
            self.start_date.setDate(self._to_qdate(month_start))
            self.end_date.setDate(self._to_qdate(month_end))
            self._has_saved_dates = True
        self._save_settings()

    def _output_filename(self, start: date, end: date) -> str:
        fmt = self._s.get_daily_delivery_output_filename_format()
        try:
            return fmt.format(start=start, end=end)
        except (KeyError, ValueError):
            return f"일일배송_{start:%Y%m%d}_{end:%Y%m%d}.xlsx"

    def _prepare_people(self) -> list[dict]:
        return prepare_daily_delivery_people(
            self._data.invoices,
            breakfast_col=self.breakfast_combo.currentData() or "",
            dinner_col=self.dinner_combo.currentData() or "",
            note_col=self.note_combo.currentData() or "",
        )

    def _generate(self):
        if not self.output_dir:
            QMessageBox.warning(self, "알림", "출력 경로를 먼저 선택하세요.")
            return
        if not self._data.invoices:
            QMessageBox.warning(
                self, "알림", "명단 데이터가 없습니다. 명단 파일을 먼저 불러오세요."
            )
            return

        driver_col = self.driver_combo.currentData() or ""
        region_primary_col = self.region_primary_combo.currentData() or ""
        region_secondary_col = self.region_secondary_combo.currentData() or ""
        address_col = self.address_combo.currentData() or ""
        breakfast_col = self.breakfast_combo.currentData() or ""
        dinner_col = self.dinner_combo.currentData() or ""
        if not driver_col:
            QMessageBox.warning(self, "알림", "배송 담당 컬럼을 선택하세요.")
            return
        if not region_primary_col:
            QMessageBox.warning(self, "알림", "담당 구역 1순위 컬럼을 선택하세요.")
            return
        if not address_col:
            QMessageBox.warning(self, "알림", "세대 구분(주소) 컬럼을 선택하세요.")
            return
        if not breakfast_col and not dinner_col:
            QMessageBox.warning(
                self, "알림", "조식 여부 또는 석식 여부 컬럼을 하나 이상 선택하세요."
            )
            return

        start = self._to_date(self.start_date.date())
        end = self._to_date(self.end_date.date())
        try:
            dates = select_delivery_dates(
                start,
                end,
                skip_non_business_days=self.business_days_only.isChecked(),
                excluded_dates=self._data.excluded_dates,
            )
            if not dates:
                QMessageBox.warning(
                    self,
                    "알림",
                    "선택 범위에 생성할 날짜가 없습니다. 주말·공휴일 제외 설정을 확인하세요.",
                )
                return

            self._save_settings()
            output_path = os.path.join(
                self.output_dir, self._output_filename(start, end)
            )
            self.deliveries = make_daily_delivery(
                self._prepare_people(),
                dates,
                output_path,
                template_path=self._s.get_daily_delivery_template_path(),
                driver_col=driver_col,
                region_primary_col=region_primary_col,
                region_secondary_col=region_secondary_col,
                address_col=address_col,
            )
        except ValueError as error:
            QMessageBox.warning(self, "생성 오류", str(error))
            return
        except Exception as error:  # noqa: BLE001 - UI 경계에서 오류를 대화상자로 변환
            QMessageBox.critical(self, "생성 오류", f"일일배송 생성 실패:\n{error}")
            return

        self._last_output_path = output_path
        self.open_file_btn.setEnabled(True)
        self._update_table()
        unmatched = max((sheet.unmatched_count for sheet in self.deliveries), default=0)
        message = (
            f"일일배송 날짜 시트 {len(self.deliveries)}개 생성 완료!\n{output_path}"
        )
        if unmatched:
            message += (
                f"\n\n최대 {unmatched}명이 '미분류' 행에 포함되었습니다. "
                "선택한 배송 담당·담당 구역 1순위·2순위 컬럼의 값을 확인해 주세요."
            )
        QMessageBox.information(self, "완료", message)

    def _update_table(self):
        weekdays = ["월", "화", "수", "목", "금", "토", "일"]
        self.result_table.setRowCount(len(self.deliveries))
        for row, delivery in enumerate(self.deliveries):
            values = (
                delivery.delivery_date.isoformat(),
                weekdays[delivery.delivery_date.weekday()],
                str(delivery.breakfast_total),
                str(delivery.dinner_total),
                str(delivery.unmatched_count),
            )
            for column, value in enumerate(values):
                self.result_table.setItem(row, column, QTableWidgetItem(value))
        self.info_label.setText(f"생성된 날짜 시트: {len(self.deliveries)}개")

    def _open_output_file(self):
        if self._last_output_path and os.path.exists(self._last_output_path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(self._last_output_path))
        else:
            QMessageBox.warning(
                self, "알림", "열 파일이 없습니다. 먼저 일일배송을 생성하세요."
            )

    def _clear(self):
        self.deliveries.clear()
        self.result_table.setRowCount(0)
        self.info_label.setText("생성된 날짜 시트: 0개")
        self._last_output_path = None
        self.open_file_btn.setEnabled(False)

    def get_daily_deliveries(self) -> list[DailyDeliverySheet]:
        return list(self.deliveries)
