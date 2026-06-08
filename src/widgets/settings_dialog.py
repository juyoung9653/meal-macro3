"""설정 편집 다이얼로그 — 모든 하드코딩 상수를 GUI로 편집."""

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..settings import get_settings, save_settings

WEEKDAY_NAMES = ["월", "화", "수", "목", "금", "토", "일"]


class _NoWheelSpinBox(QSpinBox):
    def wheelEvent(self, event):
        event.ignore()


class _NoWheelDoubleSpinBox(QDoubleSpinBox):
    def wheelEvent(self, event):
        event.ignore()


class SettingsDialog(QDialog):
    """전체 설정 편집 다이얼로그 (탭 분할)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("프로그램 설정")
        self.setMinimumSize(650, 550)
        self.resize(700, 600)
        self._s = get_settings()
        self._setup_ui()
        self._load_settings()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        tabs = QTabWidget()
        tabs.addTab(self._make_general_tab(), "일반")
        tabs.addTab(self._make_template_tab(), "템플릿")
        tabs.addTab(self._make_pricing_tab(), "단가")
        tabs.addTab(self._make_schedule_tab(), "배송 요일")
        tabs.addTab(self._make_display_tab(), "표시")
        tabs.addTab(self._make_confirm_tab(), "확인서")
        layout.addWidget(tabs)

        btn_box = QDialogButtonBox()
        reset_btn = QPushButton("기본값 초기화")
        reset_btn.clicked.connect(self._reset_to_defaults)
        btn_box.addButton(reset_btn, QDialogButtonBox.ButtonRole.ResetRole)
        btn_box.addButton(QDialogButtonBox.StandardButton.Ok)
        btn_box.addButton(QDialogButtonBox.StandardButton.Cancel)
        btn_box.accepted.connect(self._accept)
        btn_box.rejected.connect(self.reject)
        layout.addWidget(btn_box)

    # ── 각 탭 구성 ──

    def _make_general_tab(self):
        w = QWidget()
        form = QFormLayout(w)
        self._win_w_spin = _NoWheelSpinBox()
        self._win_w_spin.setRange(600, 3000)
        form.addRow("기본 창 너비:", self._win_w_spin)
        self._win_h_spin = _NoWheelSpinBox()
        self._win_h_spin.setRange(400, 2000)
        form.addRow("기본 창 높이:", self._win_h_spin)
        return self._wrap_scroll(w)

    def _make_template_tab(self):
        w = QWidget()
        layout = QVBoxLayout(w)

        path_group = QGroupBox("템플릿 파일 경로")
        path_form = QFormLayout(path_group)
        self._invoice_tpl_edit = QLineEdit()
        inv_btn = QPushButton("찾기...")
        inv_btn.clicked.connect(
            lambda: self._browse_file(self._invoice_tpl_edit, "청구서 템플릿 선택")
        )
        inv_row = QHBoxLayout()
        inv_row.addWidget(self._invoice_tpl_edit)
        inv_row.addWidget(inv_btn)
        path_form.addRow("청구서 템플릿:", inv_row)

        self._confirm_tpl_edit = QLineEdit()
        cnf_btn = QPushButton("찾기...")
        cnf_btn.clicked.connect(
            lambda: self._browse_file(self._confirm_tpl_edit, "확인서 템플릿 선택")
        )
        cnf_row = QHBoxLayout()
        cnf_row.addWidget(self._confirm_tpl_edit)
        cnf_row.addWidget(cnf_btn)
        path_form.addRow("확인서 템플릿:", cnf_row)
        layout.addWidget(path_group)

        sheet_group = QGroupBox("시트 정보")
        sheet_form = QFormLayout(sheet_group)
        self._sheet_name_edit = QLineEdit()
        sheet_form.addRow("청구서 시트명:", self._sheet_name_edit)
        self._data_start_row_spin = _NoWheelSpinBox()
        self._data_start_row_spin.setRange(1, 50)
        sheet_form.addRow("데이터 시작 행:", self._data_start_row_spin)
        self._data_cols_spin = _NoWheelSpinBox()
        self._data_cols_spin.setRange(5, 30)
        sheet_form.addRow("데이터 열 수:", self._data_cols_spin)
        self._confirm_sheets_list = QListWidget()
        self._confirm_sheets_list.setMaximumHeight(100)
        add_btn = QPushButton("시트명 추가")
        add_btn.clicked.connect(self._add_confirm_sheet)
        del_btn = QPushButton("삭제")
        del_btn.clicked.connect(self._del_confirm_sheet)
        btn_row = QHBoxLayout()
        btn_row.addWidget(add_btn)
        btn_row.addWidget(del_btn)
        btn_row.addStretch()
        sheet_form.addRow("확인서 템플릿 시트명:", self._confirm_sheets_list)
        sheet_form.addRow("", btn_row)
        layout.addWidget(sheet_group)
        return self._wrap_scroll(w)

    def _make_pricing_tab(self):
        w = QWidget()
        form = QFormLayout(w)
        self._bf_price_spin = _NoWheelSpinBox()
        self._bf_price_spin.setRange(0, 999999)
        self._bf_price_spin.setSuffix(" 원")
        form.addRow("조식 단가:", self._bf_price_spin)
        self._dn_price_spin = _NoWheelSpinBox()
        self._dn_price_spin.setRange(0, 999999)
        self._dn_price_spin.setSuffix(" 원")
        form.addRow("석식 단가:", self._dn_price_spin)
        self._lh_price_spin = _NoWheelSpinBox()
        self._lh_price_spin.setRange(0, 999999)
        self._lh_price_spin.setSuffix(" 원")
        form.addRow("중식 단가:", self._lh_price_spin)
        return self._wrap_scroll(w)

    def _make_schedule_tab(self):
        w = QWidget()
        layout = QVBoxLayout(w)
        info = QLabel(
            "각 급식 유형별 배송 요일을 선택하세요.\n선택된 요일만 배송일로 계산됩니다."
        )
        info.setStyleSheet("color: #555;")
        layout.addWidget(info)
        for meal, key in [
            ("조식", "breakfast_days"),
            ("석식", "dinner_days"),
            ("중식", "lunch_days"),
        ]:
            group = QGroupBox(f"{meal} 배송 요일")
            glayout = QHBoxLayout(group)
            checks = []
            for i, wname in enumerate(WEEKDAY_NAMES):
                cb = QCheckBox(wname)
                cb.setProperty("day_idx", i)
                checks.append(cb)
                glayout.addWidget(cb)
            glayout.addStretch()
            setattr(self, f"_{key}_checks", checks)
            layout.addWidget(group)
        return self._wrap_scroll(w)

    def _make_display_tab(self):
        w = QWidget()
        form = QFormLayout(w)
        self._font_name_edit = QLineEdit()
        form.addRow("폰트명:", self._font_name_edit)
        self._inv_font_size_spin = _NoWheelSpinBox()
        self._inv_font_size_spin.setRange(6, 20)
        form.addRow("청구서 폰트 크기:", self._inv_font_size_spin)
        self._cnf_font_size_spin = _NoWheelSpinBox()
        self._cnf_font_size_spin.setRange(6, 20)
        form.addRow("확인서 폰트 크기:", self._cnf_font_size_spin)
        self._cnf_font_small_spin = _NoWheelSpinBox()
        self._cnf_font_small_spin.setRange(6, 20)
        form.addRow("확인서 작은 폰트 크기:", self._cnf_font_small_spin)
        return self._wrap_scroll(w)

    def _make_confirm_tab(self):
        w = QWidget()
        form = QFormLayout(w)
        self._confirm_title_suffix_edit = QLineEdit()
        form.addRow("확인서 제목 접미사:", self._confirm_title_suffix_edit)
        self._confirm_filename_edit = QLineEdit()
        form.addRow("출력 파일명 형식:", self._confirm_filename_edit)
        self._invoice_filename_edit = QLineEdit()
        form.addRow("청구서 파일명 형식:", self._invoice_filename_edit)
        self._col_w_g_spin = _NoWheelDoubleSpinBox()
        self._col_w_g_spin.setRange(5, 30)
        self._col_w_g_spin.setDecimals(1)
        form.addRow("G열 너비:", self._col_w_g_spin)
        self._col_w_h_spin = _NoWheelDoubleSpinBox()
        self._col_w_h_spin.setRange(5, 30)
        self._col_w_h_spin.setDecimals(1)
        form.addRow("H열 너비:", self._col_w_h_spin)
        self._col_w_i_spin = _NoWheelDoubleSpinBox()
        self._col_w_i_spin.setRange(5, 30)
        self._col_w_i_spin.setDecimals(1)
        form.addRow("I열 너비:", self._col_w_i_spin)
        self._col_w_j_spin = _NoWheelDoubleSpinBox()
        self._col_w_j_spin.setRange(5, 30)
        self._col_w_j_spin.setDecimals(1)
        form.addRow("J열 너비:", self._col_w_j_spin)
        return self._wrap_scroll(w)

    # ── 헬퍼 ──

    def _wrap_scroll(self, w: QWidget) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(w)
        return scroll

    def _browse_file(self, line_edit: QLineEdit, title: str):
        path, _ = QFileDialog.getOpenFileName(
            self, title, "", "Excel (*.xlsx);;모든 파일 (*.*)"
        )
        if path:
            line_edit.setText(path)

    def _add_confirm_sheet(self):
        name, ok = QInputDialog.getText(self, "시트명 추가", "템플릿 시트명:")
        if ok and name.strip():
            self._confirm_sheets_list.addItem(name.strip())

    def _del_confirm_sheet(self):
        for item in self._confirm_sheets_list.selectedItems():
            self._confirm_sheets_list.takeItem(self._confirm_sheets_list.row(item))

    # ── 로드/저장 ──

    def _load_settings(self):
        s = self._s
        self._win_w_spin.setValue(s.get("app", "window_width", default=1000))
        self._win_h_spin.setValue(s.get("app", "window_height", default=700))

        self._invoice_tpl_edit.setText(s.get("templates", "invoice_path", default=""))
        self._confirm_tpl_edit.setText(s.get("templates", "confirm_path", default=""))
        self._sheet_name_edit.setText(s.get("templates", "sheet_name", default=""))
        self._data_start_row_spin.setValue(
            s.get("templates", "data_start_row", default=4)
        )
        self._data_cols_spin.setValue(s.get("templates", "data_columns", default=11))
        for name in s.get("templates", "confirm_sheet_names", default=[]):
            self._confirm_sheets_list.addItem(name)

        self._bf_price_spin.setValue(s.get("pricing", "breakfast", default=9500))
        self._dn_price_spin.setValue(s.get("pricing", "dinner", default=9500))
        self._lh_price_spin.setValue(s.get("pricing", "lunch", default=9500))

        for meal_key in ["breakfast_days", "dinner_days", "lunch_days"]:
            days = set(s.get("schedule", meal_key, default=[]))
            checks = getattr(self, f"_{meal_key}_checks", [])
            for cb in checks:
                cb.setChecked(cb.property("day_idx") in days)

        self._font_name_edit.setText(s.get("display", "font_name", default=""))
        self._inv_font_size_spin.setValue(
            s.get("display", "invoice_font_size", default=9)
        )
        self._cnf_font_size_spin.setValue(
            s.get("display", "confirm_font_size", default=11)
        )
        self._cnf_font_small_spin.setValue(
            s.get("display", "confirm_font_size_small", default=10)
        )

        self._confirm_title_suffix_edit.setText(
            s.get("confirm", "title_suffix", default="")
        )
        self._confirm_filename_edit.setText(
            s.get("confirm", "output_filename_format", default="")
        )
        self._invoice_filename_edit.setText(
            s.get("invoice", "output_filename_format", default="")
        )
        self._col_w_g_spin.setValue(s.get("confirm", "col_width_g", default=12.5))
        self._col_w_h_spin.setValue(s.get("confirm", "col_width_h", default=12.5))
        self._col_w_i_spin.setValue(s.get("confirm", "col_width_i", default=12.5))
        self._col_w_j_spin.setValue(s.get("confirm", "col_width_j", default=12.5))

    def _collect_settings(self):
        s = self._s
        s.set("app", "window_width", value=self._win_w_spin.value())
        s.set("app", "window_height", value=self._win_h_spin.value())

        s.set("templates", "invoice_path", value=self._invoice_tpl_edit.text())
        s.set("templates", "confirm_path", value=self._confirm_tpl_edit.text())
        s.set("templates", "sheet_name", value=self._sheet_name_edit.text())
        s.set("templates", "data_start_row", value=self._data_start_row_spin.value())
        s.set("templates", "data_columns", value=self._data_cols_spin.value())
        sheets = [
            self._confirm_sheets_list.item(i).text()
            for i in range(self._confirm_sheets_list.count())
        ]
        s.set("templates", "confirm_sheet_names", value=sheets)

        s.set("pricing", "breakfast", value=self._bf_price_spin.value())
        s.set("pricing", "dinner", value=self._dn_price_spin.value())
        s.set("pricing", "lunch", value=self._lh_price_spin.value())

        for meal_key in ["breakfast_days", "dinner_days", "lunch_days"]:
            checks = getattr(self, f"_{meal_key}_checks", [])
            days = [cb.property("day_idx") for cb in checks if cb.isChecked()]
            s.set("schedule", meal_key, value=days)

        s.set("display", "font_name", value=self._font_name_edit.text())
        s.set("display", "invoice_font_size", value=self._inv_font_size_spin.value())
        s.set("display", "confirm_font_size", value=self._cnf_font_size_spin.value())
        s.set(
            "display",
            "confirm_font_size_small",
            value=self._cnf_font_small_spin.value(),
        )

        s.set("confirm", "title_suffix", value=self._confirm_title_suffix_edit.text())
        s.set(
            "confirm",
            "output_filename_format",
            value=self._confirm_filename_edit.text(),
        )
        s.set(
            "invoice",
            "output_filename_format",
            value=self._invoice_filename_edit.text(),
        )
        s.set("confirm", "col_width_g", value=self._col_w_g_spin.value())
        s.set("confirm", "col_width_h", value=self._col_w_h_spin.value())
        s.set("confirm", "col_width_i", value=self._col_w_i_spin.value())
        s.set("confirm", "col_width_j", value=self._col_w_j_spin.value())

    def _accept(self):
        self._collect_settings()
        save_settings()
        QMessageBox.information(
            self,
            "알림",
            "설정이 저장되었습니다.\n일부 변경사항은 프로그램 재시작 후 적용됩니다.",
        )
        self.accept()

    def _reset_to_defaults(self):
        reply = QMessageBox.question(
            self,
            "초기화 확인",
            "모든 설정을 기본값으로 초기화하시겠습니까?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._s.reset_to_defaults()
            self._confirm_sheets_list.clear()
            self._load_settings()
