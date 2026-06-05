from datetime import date

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..invoice_generator import make_confirm


class ConfirmTab(QWidget):
    """확인서 생성 탭"""

    def __init__(self):
        super().__init__()
        self.confirms = []
        self.output_dir = None
        self._invoices = []
        self._col_mapping = {}
        self._year = 2026
        self._month = 6
        self._excluded_dates: set[date] = set()
        self._setup_ui()
        self._update_delivery_from_roster()

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

        # ── 배송담당 정보 ──
        info_group = QGroupBox("배송담당 정보")
        info_layout = QHBoxLayout(info_group)
        info_layout.addWidget(QLabel("담당자명:"))
        self.delivery_edit = QLineEdit()
        self.delivery_edit.setPlaceholderText("배송담당자 성명")
        info_layout.addWidget(self.delivery_edit)
        info_layout.addSpacing(10)
        info_layout.addWidget(QLabel("연락처:"))
        self.phone_edit = QLineEdit()
        self.phone_edit.setPlaceholderText("010-xxxx-xxxx")
        info_layout.addWidget(self.phone_edit)
        info_layout.addStretch()

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
        layout.addWidget(info_group)
        layout.addLayout(btn_layout)
        layout.addWidget(table_group, stretch=1)
        layout.addWidget(self.info_label)

    def _update_delivery_from_roster(self):
        """매핑 정보로 명단에서 배송담당자명/연락처 읽어 QLineEdit 자동 채움"""
        if not self._invoices or not self._col_mapping:
            return
        person_key = self._col_mapping.get("roster_delivery_person_col", "")
        phone_key = self._col_mapping.get("roster_delivery_phone_col", "")
        if person_key and self._invoices:
            val = str(self._invoices[0].get(person_key, ""))
            if val:
                self.delivery_edit.setText(val)
        if phone_key and self._invoices:
            val = str(self._invoices[0].get(phone_key, ""))
            if val:
                self.phone_edit.setText(val)

    def _select_output_dir(self):
        dir_path = QFileDialog.getExistingDirectory(self, "출력 폴더 선택")
        if dir_path:
            self.output_dir = dir_path
            self.path_label.setText(dir_path)
            self.path_label.setStyleSheet("color: #000;")

    def _generate_confirms(self):
        """확인서 생성"""
        if not self.output_dir:
            QMessageBox.warning(self, "알림", "출력 경로를 먼저 선택하세요.")
            return
        if not self._invoices:
            QMessageBox.warning(
                self, "알림", "청구서 데이터가 없습니다. 청구서 탭에서 먼저 생성하세요."
            )
            return
        if not self._col_mapping:
            QMessageBox.warning(self, "알림", "컬럼 매핑 정보가 없습니다.")
            return

        # QLineEdit 값 우선, 공란이면 명단 매핑에서 읽기
        delivery_person = self.delivery_edit.text().strip()
        delivery_phone = self.phone_edit.text().strip()
        if not delivery_person or not delivery_phone:
            person_key = self._col_mapping.get("roster_delivery_person_col", "")
            phone_key = self._col_mapping.get("roster_delivery_phone_col", "")
            if self._invoices:
                if not delivery_person and person_key:
                    delivery_person = str(self._invoices[0].get(person_key, ""))
                if not delivery_phone and phone_key:
                    delivery_phone = str(self._invoices[0].get(phone_key, ""))

        try:
            self.confirms = make_confirm(
                roster_data=self._invoices,
                col_mapping=self._col_mapping,
                output_dir=self.output_dir,
                template_path="확인서템플릿.xlsx",
                year=self._year,
                month=self._month,
                delivery_person=delivery_person,
                delivery_phone=delivery_phone,
                excluded_dates=self._excluded_dates.copy()
                if self._excluded_dates
                else None,
            )
            self._update_table()
            QMessageBox.information(
                self, "완료", f"확인서 {len(self.confirms)}건 생성 완료!"
            )
        except Exception as e:
            QMessageBox.critical(self, "생성 오류", f"확인서 생성 실패:\n{e}")

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

    def set_invoices(
        self,
        invoices: list[dict],
        col_mapping: dict | None = None,
        year: int = 2026,
        month: int = 6,
        excluded_dates: set[date] | None = None,
    ):
        """청구서 데이터 + 매핑 + 제외일 전달받기"""
        self._invoices = invoices
        if col_mapping:
            self._col_mapping = col_mapping
        self._year = year
        self._month = month
        self._excluded_dates = excluded_dates or set()
        self.generate_btn.setEnabled(bool(invoices))
        self._update_delivery_from_roster()
        if invoices:
            self.info_label.setText(
                f"청구서 {len(invoices)}건 로드됨. 생성 버튼을 누르면 확인서를 만듭니다."
            )

    def get_confirms(self):
        return self.confirms
