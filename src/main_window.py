from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import QMainWindow, QStatusBar, QTabWidget

from src.core.settings import get_settings
from src.widgets.confirm_tab import ConfirmTab
from src.widgets.daily_delivery_tab import DailyDeliveryTab
from src.widgets.invoice_tab import InvoiceTab
from src.widgets.meal_summary_tab import MealSummaryTab
from src.widgets.settings_dialog import SettingsDialog


class MainWindow(QMainWindow):
    """급식비 정산 프로그램 메인 윈도우"""

    def __init__(self):
        super().__init__()
        self._s = get_settings()
        self._setup_window()
        self._setup_menu()
        self._setup_tabs()
        self._connect_signals()
        self._setup_statusbar()

    def _setup_window(self):
        self.setWindowTitle(self._s.get_app_title())
        w, h = self._s.get_app_window_size()
        self.setMinimumSize(w, h)
        self.resize(w, h)

    def _setup_menu(self):
        menubar = self.menuBar()
        file_menu = menubar.addMenu("파일")
        settings_action = QAction("설정", self)
        settings_action.triggered.connect(self._open_settings)
        file_menu.addAction(settings_action)

    def _setup_tabs(self):
        self.tab_widget = QTabWidget()
        self.invoice_tab = InvoiceTab()
        self.confirm_tab = ConfirmTab()
        self.daily_delivery_tab = DailyDeliveryTab()
        self.meal_summary_tab = MealSummaryTab()
        self.meal_summary_tab.set_month(
            self.invoice_tab.get_year(), self.invoice_tab.get_month()
        )
        self.tab_widget.addTab(self.invoice_tab, "① 청구서")
        self.tab_widget.addTab(self.confirm_tab, "② 확인서")
        self.tab_widget.addTab(self.daily_delivery_tab, "③ 일일배송")
        self.tab_widget.addTab(self.meal_summary_tab, "④ 식수현황")
        self.setCentralWidget(self.tab_widget)

    def _connect_signals(self):
        self.tab_widget.currentChanged.connect(self._on_tab_changed)
        self.meal_summary_tab.month_changed.connect(self._refresh_meal_summary)

    def _setup_statusbar(self):
        self.statusbar = QStatusBar()
        self.setStatusBar(self.statusbar)
        self.statusbar.showMessage("명단 파일을 선택해 주세요.")

    def _on_tab_changed(self, index: int):
        if index == 1:
            self.confirm_tab.set_invoice_data(self.invoice_tab.get_invoice_data())
        elif index == 2:
            self.daily_delivery_tab.set_invoice_data(
                self.invoice_tab.get_daily_delivery_data()
            )
        elif index == 3:
            year, month = self.meal_summary_tab.selected_month()
            self._refresh_meal_summary(year, month)

        self.statusbar.showMessage(
            {
                0: "명단 로드 완료. 컬럼 매핑 후 청구서 생성",
                1: "청구서 기반 확인서 생성",
                2: "날짜 범위를 선택해 일일배송 생성",
                3: "월을 선택해 일자별·식종별 식수 확인",
            }.get(index, "")
        )

    def _refresh_meal_summary(self, year: int, month: int):
        data = self.invoice_tab.get_daily_delivery_data(year, month)
        self.meal_summary_tab.set_invoice_data(data)

    def _open_settings(self):
        dlg = SettingsDialog(self)
        dlg.exec()
