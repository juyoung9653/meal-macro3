import os

from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QStatusBar,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from src.core.settings import get_settings
from src.widgets.confirm_tab import ConfirmTab
from src.widgets.delivery_substitute_tab import DeliverySubstituteTab
from src.widgets.invoice_tab import InvoiceTab
from src.widgets.report_tab import ReportTab


class MainWindow(QMainWindow):
    """급식비 정산 프로그램 메인 윈도우"""

    def __init__(self):
        super().__init__()
        self._s = get_settings()
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

        # ── 업체명 선택 + 명단 불러오기 ──
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
        meal_layout.addWidget(QLabel("명단:"))
        self.roster_label = QLabel("db.xlsx 로드 대기 중")
        self.roster_label.setStyleSheet("color: #888;")
        meal_layout.addWidget(self.roster_label)
        self.roster_btn = QPushButton("파일 선택")
        self.roster_btn.clicked.connect(self._select_roster_file)
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
        self.statusbar.showMessage("명단 파일을 선택해 주세요.")

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

    # ── 명단 파일 선택 ──

    def _select_roster_file(self):
        from src.excel.db_reader import get_db_reader

        reader = get_db_reader()
        path, _ = QFileDialog.getOpenFileName(
            self,
            "명단 파일 선택",
            reader.db_path,
            "Excel (*.xlsx *.xls);;모든 파일 (*.*)",
        )
        if path:
            self._load_roster(path)

    def _load_roster(self, path: str):
        from src.excel.db_reader import get_db_reader

        reader = get_db_reader()
        reader.db_path = path
        self._s.set("db", "path", value=path)

        # db/last_path 저장 (재실행 시 자동 복원)
        settings = QSettings(
            self._s.get("app", "organization"),
            self._s.get("app", "title"),
        )
        settings.setValue("db/last_path", path)
        settings.sync()

        # 모든 탭에 변경 알림
        self.invoice_tab.on_roster_changed()
        self.report_tab.on_roster_changed()
        self.delivery_tab.on_roster_changed()

        roster = reader.read_roster()
        self.roster_label.setText(f"✅ {os.path.basename(path)} ({len(roster)}명)")
        self.roster_label.setStyleSheet("color: #0a0; font-weight: bold;")
        self.statusbar.showMessage(f"명단 로드 완료: {len(roster)}명")

    def post_init_load(self):
        """show() 후 자동 로드 (saved state)."""
        from src.excel.db_reader import get_db_reader

        settings = QSettings(
            self._s.get("app", "organization"),
            self._s.get("app", "title"),
        )
        db_path = settings.value("db/last_path", "")
        if not db_path:
            db_path = self._s.get("db", "path")

        if db_path and os.path.exists(db_path):
            reader = get_db_reader()
            reader.db_path = db_path
            self._load_roster(db_path)

    def _on_tab_changed(self, index: int):
        # 출력 경로 양방향 동기화
        inv_dir = self.invoice_tab.output_dir
        cnf_dir = self.confirm_tab.output_dir
        if inv_dir and inv_dir != cnf_dir:
            self.confirm_tab.set_output_dir(inv_dir)
        elif cnf_dir and cnf_dir != inv_dir:
            self.invoice_tab.set_output_dir(cnf_dir)

        if index >= 1:
            self.confirm_tab.set_invoice_data(self.invoice_tab.get_invoice_data())

        self.statusbar.showMessage(
            {
                0: "① 청구서 탭",
                1: "② 확인서 탭",
                2: "③ 배송리포트 탭",
            }.get(index, "")
        )
