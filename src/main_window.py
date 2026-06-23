from PyQt6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QStatusBar,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from src.core.settings import get_settings
from src.widgets.confirm_tab import ConfirmTab
from src.widgets.invoice_tab import InvoiceTab


class MainWindow(QMainWindow):
    """급식비 정산 프로그램 메인 윈도우"""

    def __init__(self):
        super().__init__()
        self._s = get_settings()
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

        # ── 식사 유형 선택 ──
        meal_layout = QHBoxLayout()
        meal_layout.addWidget(QLabel("식사 유형:"))
        self.meal_combo = QComboBox()
        self.meal_combo.addItems(["급식", "행복도시락"])
        current_meal = self._s.get_meal_type()
        if current_meal in ("급식", "행복도시락"):
            self.meal_combo.setCurrentText(current_meal)
        self.meal_combo.currentTextChanged.connect(self._on_meal_changed)
        meal_layout.addWidget(self.meal_combo)
        meal_layout.addStretch()
        layout.addLayout(meal_layout)

        self.tab_widget = QTabWidget()
        layout.addWidget(self.tab_widget)

        self.setCentralWidget(central)

    def _setup_tabs(self):
        meal_type = self._s.get_meal_type()
        self.invoice_tab = InvoiceTab(meal_type=meal_type)
        self.confirm_tab = ConfirmTab(meal_type=meal_type)
        self.tab_widget.addTab(self.invoice_tab, "① 청구서")
        self.tab_widget.addTab(self.confirm_tab, "② 확인서")

    def _connect_signals(self):
        self.tab_widget.currentChanged.connect(self._on_tab_changed)

    def _setup_statusbar(self):
        self.statusbar = QStatusBar()
        self.setStatusBar(self.statusbar)
        self.statusbar.showMessage("명단 파일을 선택해 주세요.")

    def _on_meal_changed(self, meal_type: str):
        """식사 유형 변경 → 설정 저장 + 양 탭에 전파."""
        self._s.set_meal_type(meal_type)
        self.invoice_tab.set_meal_type(meal_type)
        self.confirm_tab.set_meal_type(meal_type)

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
                0: "명단 로드 완료. 컬럼 매핑 후 청구서 생성",
                1: "청구서 기반 확인서 생성",
            }.get(index, "")
        )
