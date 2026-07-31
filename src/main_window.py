import os

from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStatusBar,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from src.core.delivery_matrix import AnnualDeliveryBook
from src.core.settings import get_settings
from src.excel.annual_workbook import AnnualWorkbook
from src.widgets.confirm_tab import ConfirmTab
from src.widgets.delivery_substitute_tab import DeliverySubstituteTab
from src.widgets.invoice_tab import InvoiceTab
from src.widgets.report_tab import ReportTab


class MainWindow(QMainWindow):
    """급식비 정산 프로그램 메인 윈도우"""

    def __init__(self):
        super().__init__()
        self._s = get_settings()
        self._annual_workbook: AnnualWorkbook | None = None
        self._annual_book: AnnualDeliveryBook | None = None
        self._restore_meal_type()
        self._setup_window()
        self._setup_central()
        self._setup_tabs()
        self._connect_signals()
        self._setup_statusbar()

    def _setup_window(self):
        self.setWindowTitle(self._s.get("app", "title"))
        w, h = self._s.get_app_window_size()
        self.setMinimumSize(w, h)
        self.resize(w, h)

    def _setup_central(self):
        central = QWidget()
        layout = QVBoxLayout(central)

        # ── 업체명 선택 + 연간 배송 파일 불러오기 ──
        meal_layout = QHBoxLayout()
        meal_layout.addWidget(QLabel("업체명:"))
        self.meal_combo = QComboBox()
        self.meal_combo.addItems(["급식", "행복도시락"])
        current_meal = self._s.get_meal_type()
        if current_meal in ("급식", "행복도시락"):
            self.meal_combo.setCurrentText(current_meal)
        self.meal_combo.currentTextChanged.connect(self._on_meal_changed)
        meal_layout.addWidget(self.meal_combo)
        meal_layout.addSpacing(30)
        meal_layout.addWidget(QLabel("연간 파일:"))
        self.roster_label = QLabel("연간 배송 Excel 로드 대기 중")
        self.roster_label.setStyleSheet("color: #888;")
        meal_layout.addWidget(self.roster_label)
        self.roster_btn = QPushButton("파일 선택")
        self.roster_btn.clicked.connect(self._select_annual_file)
        meal_layout.addWidget(self.roster_btn)
        meal_layout.addStretch()
        layout.addLayout(meal_layout)

        self.tab_widget = QTabWidget()
        layout.addWidget(self.tab_widget)

        self.setCentralWidget(central)

    def _setup_tabs(self):
        meal_type = self._s.get_meal_type()
        self.invoice_tab = InvoiceTab(meal_type=meal_type)
        self.confirm_tab = ConfirmTab(meal_type=meal_type)
        self.report_tab = ReportTab(meal_type=meal_type)
        self.delivery_tab = DeliverySubstituteTab(meal_type=meal_type)
        self.tab_widget.addTab(self.invoice_tab, "① 청구서")
        self.tab_widget.addTab(self.confirm_tab, "② 확인서")
        self.tab_widget.addTab(self.report_tab, "③ 배송리포트")
        self.tab_widget.addTab(self.delivery_tab, "④ 배송대타")

    def _connect_signals(self):
        self.tab_widget.currentChanged.connect(self._on_tab_changed)

    def _setup_statusbar(self):
        self.statusbar = QStatusBar()
        self.setStatusBar(self.statusbar)
        self.statusbar.showMessage("연간 배송 Excel 파일을 선택해 주세요.")

    def _restore_meal_type(self):
        """QSettings에서 이전에 선택한 업체명을 복원."""
        settings = QSettings(
            self._s.get("app", "organization"),
            self._s.get("app", "title"),
        )
        saved = settings.value("meal_type", "")
        if saved in ("급식", "행복도시락"):
            self._s.set_meal_type(saved)

    def _on_meal_changed(self, meal_type: str):
        """업체명 변경 → 설정 저장(QSettings + in-memory) + 양 탭에 전파."""
        self._s.set_meal_type(meal_type)
        settings = QSettings(
            self._s.get("app", "organization"),
            self._s.get("app", "title"),
        )
        settings.setValue("meal_type", meal_type)
        settings.sync()
        self.invoice_tab.set_meal_type(meal_type)
        self.confirm_tab.set_meal_type(meal_type)
        self.report_tab.set_meal_type(meal_type)
        self.delivery_tab.set_meal_type(meal_type)
        self.confirm_tab.set_invoice_data(self.invoice_tab.get_invoice_data())

    # ── 연간 배송 파일 선택 ──

    def _select_annual_file(self):
        configured_path = self._s.get("annual_workbook", "path", default="")
        initial_path = (
            str(self._annual_workbook.path)
            if self._annual_workbook is not None
            else str(configured_path or "")
        )
        path, _ = QFileDialog.getOpenFileName(
            self,
            "연간 배송 Excel 선택",
            initial_path,
            "Excel 통합 문서 (*.xlsx *.xlsm);;모든 파일 (*.*)",
        )
        if path:
            self._load_annual_file(path)

    def _load_annual_file(self, path: str) -> bool:
        """파일을 먼저 검증한 뒤 성공한 장부만 모든 탭에 배포한다."""
        password = self._s.get("annual_workbook", "password", default="")
        workbook = AnnualWorkbook(path, password=password)
        try:
            book = workbook.load()
        except Exception as error:  # noqa: BLE001 - 사용자용 오류로 표시
            if self._annual_workbook is not None:
                previous_name = os.path.basename(str(self._annual_workbook.path))
                retained_message = f"기존 파일 '{previous_name}'을 계속 사용합니다."
                self.roster_label.setText(f"⚠️ 로드 실패 · {retained_message}")
                self.roster_label.setStyleSheet(
                    "color: #b36b00; font-weight: bold;"
                )
            else:
                retained_message = "현재 활성화된 연간 파일이 없습니다."
                self.roster_label.setStyleSheet("color: #c00;")
            self.statusbar.showMessage(
                f"연간 배송 Excel 로드 실패 · {retained_message}"
            )
            QMessageBox.critical(
                self,
                "연간 파일 오류",
                "연간 배송 Excel을 불러오지 못했습니다:\n"
                f"{error}\n\n{retained_message}",
            )
            return False

        self._annual_workbook = workbook
        self._annual_book = book
        self._s.set("annual_workbook", "path", value=path)

        settings = QSettings(
            self._s.get("app", "organization"),
            self._s.get("app", "title"),
        )
        settings.setValue("annual_workbook/last_path", path)
        settings.sync()

        self.invoice_tab.set_annual_book(book)
        self.report_tab.set_annual_book(book)
        self.delivery_tab.set_annual_book(book)
        self.confirm_tab.set_invoice_data(self.invoice_tab.get_invoice_data())

        filename = os.path.basename(path)
        self.roster_label.setText(
            f"✅ {filename} ({book.year}년 · {len(book.roster)}명)"
        )
        self.roster_label.setStyleSheet("color: #0a0; font-weight: bold;")
        self.statusbar.showMessage(
            f"연간 배송 파일 로드 완료: {book.year}년, 명단 {len(book.roster)}명"
        )
        return True

    def post_init_load(self):
        """show() 후 마지막으로 사용한 연간 배송 파일을 자동 로드한다."""
        settings = QSettings(
            self._s.get("app", "organization"),
            self._s.get("app", "title"),
        )
        saved_path = settings.value("annual_workbook/last_path", "")
        if not saved_path:
            saved_path = self._s.get("annual_workbook", "path", default="")

        path = str(saved_path) if saved_path else ""
        if path and os.path.isfile(path):
            self._load_annual_file(path)

    def _on_tab_changed(self, index: int):
        # 출력 경로 양방향 동기화
        inv_dir = self.invoice_tab.output_dir
        cnf_dir = self.confirm_tab.output_dir
        if inv_dir and inv_dir != cnf_dir:
            self.confirm_tab.set_output_dir(inv_dir)
        elif cnf_dir and cnf_dir != inv_dir:
            self.invoice_tab.set_output_dir(cnf_dir)

        if index == 1:
            self.confirm_tab.set_invoice_data(self.invoice_tab.get_invoice_data())

        self.statusbar.showMessage(
            {
                0: "① 청구서 탭",
                1: "② 확인서 탭",
                2: "③ 배송리포트 탭",
                3: "④ 배송대타 탭",
            }.get(index, "")
        )
