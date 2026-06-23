"""설정 편집 다이얼로그 — 모든 하드코딩 상수를 GUI로 편집."""

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
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

from ..core.settings import get_settings, save_settings


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
        layout = QVBoxLayout(w)

        app_group = QGroupBox("애플리케이션")
        form = QFormLayout(app_group)
        self._win_w_spin = _NoWheelSpinBox()
        self._win_w_spin.setRange(600, 3000)
        form.addRow("기본 창 너비:", self._win_w_spin)
        self._win_h_spin = _NoWheelSpinBox()
        self._win_h_spin.setRange(400, 2000)
        form.addRow("기본 창 높이:", self._win_h_spin)
        layout.addWidget(app_group)

        db_group = QGroupBox("데이터베이스 (db.xlsx)")
        db_form = QFormLayout(db_group)
        self._db_path_edit = QLineEdit()
        db_btn = QPushButton("찾기...")
        db_btn.clicked.connect(
            lambda: self._browse_file(self._db_path_edit, "db.xlsx 파일 선택")
        )
        db_row = QHBoxLayout()
        db_row.addWidget(self._db_path_edit)
        db_row.addWidget(db_btn)
        db_form.addRow("db.xlsx 경로:", db_row)
        layout.addWidget(db_group)
        layout.addStretch()
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
        w, h = s.get_app_window_size()
        self._win_w_spin.setValue(w)
        self._win_h_spin.setValue(h)
        self._db_path_edit.setText(s.get("db", "path"))

        self._invoice_tpl_edit.setText(s.get("templates", "invoice_path"))
        self._confirm_tpl_edit.setText(s.get("templates", "confirm_path"))
        self._sheet_name_edit.setText(s.get("templates", "sheet_name"))
        self._data_start_row_spin.setValue(s.get("templates", "data_start_row"))
        self._data_cols_spin.setValue(s.get("templates", "data_columns"))
        for name in s.get("templates", "confirm_sheet_names"):
            self._confirm_sheets_list.addItem(name)

        self._confirm_title_suffix_edit.setText(s.get("confirm", "title_suffix"))
        self._confirm_filename_edit.setText(s.get("confirm", "output_filename_format"))
        self._invoice_filename_edit.setText(s.get("invoice", "output_filename_format"))
        col_widths = s.get_confirm_col_widths()
        self._col_w_g_spin.setValue(col_widths["G"])
        self._col_w_h_spin.setValue(col_widths["H"])
        self._col_w_i_spin.setValue(col_widths["I"])
        self._col_w_j_spin.setValue(col_widths["J"])

    def _collect_settings(self):
        s = self._s
        s.set("app", "window_width", value=self._win_w_spin.value())
        s.set("app", "window_height", value=self._win_h_spin.value())
        s.set("db", "path", value=self._db_path_edit.text())

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
