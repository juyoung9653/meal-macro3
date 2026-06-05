from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QMainWindow, QMessageBox, QStatusBar, QTabWidget

from src.widgets.confirm_tab import ConfirmTab
from src.widgets.invoice_tab import InvoiceTab
from src.widgets.roster_tab import RosterTab


class MainWindow(QMainWindow):
    """급식비 정산 프로그램 메인 윈도우"""

    APP_TITLE = "급식비 정산 프로그램"
    WINDOW_WIDTH = 1000
    WINDOW_HEIGHT = 700

    def __init__(self):
        super().__init__()
        self._setup_window()
        self._setup_tabs()
        self._connect_signals()
        self._setup_statusbar()

    def _setup_window(self):
        self.setWindowTitle(self.APP_TITLE)
        self.setMinimumSize(self.WINDOW_WIDTH, self.WINDOW_HEIGHT)
        self.resize(self.WINDOW_WIDTH, self.WINDOW_HEIGHT)

    def _setup_tabs(self):
        self.tab_widget = QTabWidget()
        self.roster_tab = RosterTab()
        self.invoice_tab = InvoiceTab()
        self.confirm_tab = ConfirmTab()
        self.tab_widget.addTab(self.roster_tab, "① 명단")
        self.tab_widget.addTab(self.invoice_tab, "② 청구서")
        self.tab_widget.addTab(self.confirm_tab, "③ 확인서")
        self.setCentralWidget(self.tab_widget)

    def _connect_signals(self):
        self.tab_widget.currentChanged.connect(self._on_tab_changed)

    def _setup_statusbar(self):
        self.statusbar = QStatusBar()
        self.setStatusBar(self.statusbar)
        self.statusbar.showMessage("명단 파일을 선택해 주세요.")

    def _on_tab_changed(self, index: int):
        if index >= 1:
            data = self.roster_tab.get_roster_data()
            headers = self.roster_tab.get_roster_headers()
            self.invoice_tab.set_roster_data(data, headers)

        if index >= 2:
            invoices = self.invoice_tab.get_invoices()
            col_mapping = self.invoice_tab.get_col_mapping()
            year = self.invoice_tab.get_year()
            month = self.invoice_tab.get_month()
            excluded_dates = self.invoice_tab.get_excluded_dates()
            roster_headers = self.roster_tab.get_roster_headers()
            self.confirm_tab.set_invoices(
                invoices, col_mapping, year, month, excluded_dates, roster_headers
            )

        messages = {
            0: "명단 파일을 선택해 주세요.",
            1: "명단 로드 완료. 컬럼 매핑 후 청구서 생성",
            2: "청구서 기반 확인서 생성",
        }
        self.statusbar.showMessage(messages.get(index, ""))
