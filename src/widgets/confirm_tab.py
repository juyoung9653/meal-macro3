import os

from PyQt6.QtCore import QSettings, Qt, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
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
from ..generators.confirm_generator import make_confirm


class ConfirmTab(QWidget):
    """확인서 생성 탭"""

    def __init__(self, meal_type: str = ""):
        super().__init__()
        self.confirms = []
        self.output_dir = None
        self._last_output_path: str | None = None
        self._data = InvoiceData()
        self._per_file_mode = True
        self._s = get_settings()
        self._meal_type = meal_type or self._s.get_meal_type()
        org = self._s.get("app", "organization")
        app_title = self._s.get("app", "title")
        self._settings = QSettings(org, app_title)
        self._setup_ui()
        self._restore_state()

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        # ── 안내 ──
        info_label = QLabel("② 청구서 기반으로 확인서를 생성하세요.")
        info_label.setStyleSheet("color: #666; font-style: italic;")

        # ── 출력 경로 ──
        path_group = QGroupBox("출력 경로")
        path_layout = QHBoxLayout(path_group)

        self.path_label = QLabel("선택된 경로 없음")
        self.path_label.setStyleSheet("color: #888;")
        self.path_btn = QPushButton("폴더 선택")
        self.path_btn.clicked.connect(self._select_output_dir)

        path_layout.addWidget(self.path_label, stretch=1)
        path_layout.addWidget(self.path_btn)

        # ── 버튼 ──
        btn_layout = QHBoxLayout()

        self.generate_btn = QPushButton("확인서 생성")
        self.generate_btn.setEnabled(False)
        self.generate_btn.clicked.connect(self._generate_confirms)

        self.open_file_btn = QPushButton("파일 열기")
        self.open_file_btn.setEnabled(False)
        self.open_file_btn.clicked.connect(self._open_output_file)

        self.clear_btn = QPushButton("초기화")
        self.clear_btn.clicked.connect(self._clear)

        btn_layout.addStretch()
        btn_layout.addWidget(self.generate_btn)
        btn_layout.addWidget(self.open_file_btn)
        btn_layout.addWidget(self.clear_btn)

        # ── 확인서 목록 ──
        table_group = QGroupBox("생성된 확인서 목록")
        table_layout = QVBoxLayout(table_group)

        self.confirm_table = QTableWidget()
        self.confirm_table.setColumnCount(7)
        self.confirm_table.setHorizontalHeaderLabels(
            ["시트명", "총 가구 수", "1명", "2명", "3명", "4명", "5명"]
        )
        hdr2 = self.confirm_table.horizontalHeader()
        hdr2.setStretchLastSection(True)
        hdr2.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.confirm_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.confirm_table.setAlternatingRowColors(True)

        table_layout.addWidget(self.confirm_table)

        # ── 하단 정보 ──
        self.info_label = QLabel("생성된 확인서: 0건")
        self.info_label.setAlignment(Qt.AlignmentFlag.AlignRight)

        # ── 조립 ──
        layout.addWidget(info_label)
        layout.addWidget(path_group)
        layout.addLayout(btn_layout)
        layout.addWidget(table_group, stretch=1)
        layout.addWidget(self.info_label)

    def _save_settings(self):
        self._settings.setValue("output_dir", self.output_dir or "")
        self._settings.sync()

    def _restore_state(self):
        raw = self._settings.value("output_dir", "")
        if raw:
            self.output_dir = raw
            self.path_label.setText(raw)
            self.path_label.setStyleSheet("color: #000;")

    def _select_output_dir(self):
        dir_path = QFileDialog.getExistingDirectory(self, "출력 폴더 선택")
        if dir_path:
            self.output_dir = dir_path
            self.path_label.setText(dir_path)
            self.path_label.setStyleSheet("color: #000;")
            self._save_settings()

    def _generate_confirms(self):
        """확인서 생성"""
        if not self.output_dir:
            QMessageBox.warning(self, "알림", "출력 경로를 먼저 선택하세요.")
            return
        if not self._data.invoices:
            QMessageBox.warning(
                self, "알림", "청구서 데이터가 없습니다. 청구서 탭에서 먼저 생성하세요."
            )
            return

        try:
            self._save_settings()

            # 청구서에 실제 포함된 인원만 확인서 대상 (건수 0인 인원 제외)
            active_invoices = [
                p
                for p in self._data.invoices
                if p.get("_bf_cnt", 0) > 0
                or p.get("_dn_cnt", 0) > 0
                or p.get("_lh_cnt", 0) > 0
            ]

            template_path = self._s.get("templates", "confirm_path")
            self.confirms = make_confirm(
                roster_data=active_invoices,
                col_mapping=self._data.col_mapping,
                output_dir=self.output_dir,
                template_path=template_path,
                year=self._data.year,
                month=self._data.month,
                excluded_dates=self._data.excluded_dates.copy()
                if self._data.excluded_dates
                else None,
                meal_type=self._meal_type,
            )
            if self.confirms:
                fname = self.confirms[0].get("file", "")
                self._last_output_path = os.path.join(self.output_dir, fname)
                self.open_file_btn.setEnabled(True)
            self._update_table()
            QMessageBox.information(
                self, "완료", f"확인서 {len(self.confirms)}건 생성 완료!"
            )
        except ValueError as e:
            QMessageBox.warning(self, "템플릿 오류", str(e))
        except Exception as e:
            QMessageBox.critical(self, "생성 오류", f"확인서 생성 실패:\n{e}")

    def _update_table(self):
        show = self.confirms
        self.confirm_table.setRowCount(len(show))
        for i, cf in enumerate(show):
            self.confirm_table.setItem(i, 0, QTableWidgetItem(cf["sheet_name"]))
            self.confirm_table.setItem(i, 1, QTableWidgetItem(str(cf["total_hh"])))
            self.confirm_table.setItem(i, 2, QTableWidgetItem(str(cf["count_1"])))
            self.confirm_table.setItem(i, 3, QTableWidgetItem(str(cf["count_2"])))
            self.confirm_table.setItem(i, 4, QTableWidgetItem(str(cf["count_3"])))
            self.confirm_table.setItem(i, 5, QTableWidgetItem(str(cf["count_4"])))
            self.confirm_table.setItem(i, 6, QTableWidgetItem(str(cf["count_5"])))

        self.info_label.setText(f"생성된 확인서: {len(self.confirms)}건")

    def _open_output_file(self):
        if self._last_output_path and os.path.exists(self._last_output_path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(self._last_output_path))
        else:
            QMessageBox.warning(
                self, "알림", "열 파일이 없습니다. 먼저 확인서를 생성하세요."
            )

    def set_output_dir(self, path: str):
        """외부에서 출력 경로 설정 (탭 간 공유용)."""
        if path and path != self.output_dir:
            self.output_dir = path
            self.path_label.setText(path)
            self.path_label.setStyleSheet("color: #000;")
            self._save_settings()

    def set_meal_type(self, meal_type: str):
        """식사 유형 변경."""
        if meal_type == self._meal_type:
            return
        self._meal_type = meal_type

    def _clear(self):
        self.confirms.clear()
        self.confirm_table.setRowCount(0)
        self.info_label.setText("생성된 확인서: 0건")
        self._last_output_path = None
        self.open_file_btn.setEnabled(False)

    def set_invoice_data(self, data: InvoiceData):
        """InvoiceData 객체로 모든 상태 한 번에 설정"""
        self._data = data
        self.generate_btn.setEnabled(bool(data.invoices))

        if data.invoices:
            self.info_label.setText(
                f"청구서 {len(data.invoices)}건 로드됨. 생성 버튼을 누르면 확인서를 만듭니다."
            )

    def get_confirms(self):
        return self.confirms
