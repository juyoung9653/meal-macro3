from PyQt6.QtCore import Qt
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


class ConfirmTab(QWidget):
    """확인서 생성 탭"""

    def __init__(self):
        super().__init__()
        self.confirms = []
        self.output_dir = None
        self._setup_ui()

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

        self.clear_btn = QPushButton("초기화")
        self.clear_btn.clicked.connect(self._clear)

        btn_layout.addStretch()
        btn_layout.addWidget(self.generate_btn)
        btn_layout.addWidget(self.clear_btn)

        # ── 확인서 목록 ──
        table_group = QGroupBox("생성된 확인서 목록")
        table_layout = QVBoxLayout(table_group)

        self.confirm_table = QTableWidget()
        self.confirm_table.setColumnCount(5)
        self.confirm_table.setHorizontalHeaderLabels(
            ["번호", "이름", "소속", "청구금액", "파일명"]
        )
        self.confirm_table.horizontalHeader().setStretchLastSection(True)
        self.confirm_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
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

    def _select_output_dir(self):
        dir_path = QFileDialog.getExistingDirectory(self, "출력 폴더 선택")
        if dir_path:
            self.output_dir = dir_path
            self.path_label.setText(dir_path)
            self.path_label.setStyleSheet("color: #000;")

    def _generate_confirms(self):
        """확인서 생성 (추후 실제 로직 구현)"""
        if not self.output_dir:
            QMessageBox.warning(self, "알림", "출력 경로를 먼저 선택하세요.")
            return
        if not hasattr(self, "_invoices") or not self._invoices:
            QMessageBox.warning(
                self, "알림", "청구서 데이터가 없습니다. 청구서 탭에서 먼저 생성하세요."
            )
            return

        # TODO: 청구서 기반 확인서 생성 로직 구현
        QMessageBox.information(
            self, "알림", "확인서 생성 로직이 아직 구현되지 않았습니다."
        )

    def _update_table(self):
        self.confirm_table.setRowCount(len(self.confirms))
        for i, cf in enumerate(self.confirms):
            self.confirm_table.setItem(i, 0, QTableWidgetItem(str(i + 1)))
            self.confirm_table.setItem(i, 1, QTableWidgetItem(cf["name"]))
            self.confirm_table.setItem(i, 2, QTableWidgetItem(cf["dept"]))
            self.confirm_table.setItem(i, 3, QTableWidgetItem(f"{cf['amount']:,}원"))
            self.confirm_table.setItem(i, 4, QTableWidgetItem(cf["file"]))

        self.info_label.setText(f"생성된 확인서: {len(self.confirms)}건")

    def _clear(self):
        self.confirms.clear()
        self.confirm_table.setRowCount(0)
        self.info_label.setText("생성된 확인서: 0건")

    def set_invoices(self, invoices: list[dict]):
        """청구서 데이터 전달받기"""
        self._invoices = invoices
        self.generate_btn.setEnabled(bool(invoices))
        if invoices:
            self.info_label.setText(
                f"청구서 {len(invoices)}건 로드됨. 생성 버튼을 누르면 확인서를 만듭니다."
            )

    def get_confirms(self):
        return self.confirms
