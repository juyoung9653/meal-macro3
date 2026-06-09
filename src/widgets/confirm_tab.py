from datetime import date

from PyQt6.QtCore import QSettings, Qt
from PyQt6.QtWidgets import (
    QComboBox,
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

from ..confirm_generator import make_confirm
from ..settings import get_settings


class ConfirmTab(QWidget):
    """확인서 생성 탭"""

    MAX_RESULT_ROWS = 10

    def __init__(self):
        super().__init__()
        self.confirms = []
        self.output_dir = None
        self._invoices = []
        self._col_mapping = {}
        self._year = 2026
        self._month = 6
        self._excluded_dates: set[date] = set()
        self._roster_headers: list[str] = []
        self._roster_path = None
        self._per_file_mode = True
        self._s = get_settings()
        org = self._s.get("app", "organization", default="분당구")
        app_title = self._s.get("app", "title", default="급식비정산")
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

        # ── 배송담당 매핑 ──
        delivery_group = QGroupBox("배송담당 정보")
        delivery_layout = QVBoxLayout(delivery_group)

        # 컬럼 선택
        col_row = QHBoxLayout()
        col_row.addWidget(QLabel("배송담당자 컬럼:"))
        self.delivery_col_combo = QComboBox()
        self.delivery_col_combo.setMinimumWidth(180)
        self.delivery_col_combo.addItem("(선택 안 함)", "")
        self.delivery_col_combo.currentIndexChanged.connect(
            self._on_delivery_col_changed
        )
        col_row.addWidget(self.delivery_col_combo)
        col_row.addStretch()

        # 테이블: 고유 배송담당자명 → 연락처
        map_label = QLabel("※ 고유 배송담당자명 목록 — 연락처를 직접 입력하세요")
        map_label.setStyleSheet("color: #555; font-size: 11px;")

        self.delivery_table = QTableWidget()
        self.delivery_table.setColumnCount(2)
        self.delivery_table.setHorizontalHeaderLabels(["배송담당자명", "연락처"])
        self.delivery_table.horizontalHeader().setStretchLastSection(True)
        self.delivery_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self.delivery_table.setMinimumHeight(100)
        self.delivery_table.setMaximumHeight(200)

        delivery_layout.addLayout(col_row)
        delivery_layout.addWidget(map_label)
        delivery_layout.addWidget(self.delivery_table)
        self.delivery_table.cellChanged.connect(self._save_settings)

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
        layout.addWidget(delivery_group)
        layout.addLayout(btn_layout)
        layout.addWidget(table_group, stretch=1)
        layout.addWidget(self.info_label)

    def _save_settings(self):
        self._settings.setValue("confirm/output_dir", self.output_dir or "")
        self._settings.setValue(
            "confirm/delivery_col", self.delivery_col_combo.currentData() or ""
        )
        self._settings.setValue(
            "confirm/delivery_phone_map", self._get_delivery_phone_map()
        )
        self._settings.sync()

    def _restore_state(self):
        raw = self._settings.value("confirm/output_dir", "")
        if raw:
            self.output_dir = raw
            self.path_label.setText(raw)
            self.path_label.setStyleSheet("color: #000;")

        # 배송담당자 컬럼/전화번호는 set_invoices()에서 복원 (콤보박스 채워진 후)

    def _on_delivery_col_changed(self):
        """배송담당자 컬럼 변경 → 고유 이름 목록 테이블 갱신"""
        self._save_settings()
        col = self.delivery_col_combo.currentData()
        if not col or not self._invoices:
            self.delivery_table.setRowCount(0)
            return

        # 고유 이름 추출 (비어있지 않은 값, 순서 유지)
        seen = set()
        unique_names = []
        for inv in self._invoices:
            val = str(inv.get(col, "")).strip()
            if val and val not in seen:
                seen.add(val)
                unique_names.append(val)

        if not unique_names:
            self.delivery_table.setRowCount(0)
            return

        self.delivery_table.setRowCount(len(unique_names))
        for i, name in enumerate(unique_names):
            name_item = QTableWidgetItem(name)
            name_item.setFlags(name_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.delivery_table.setItem(i, 0, name_item)
            self.delivery_table.setItem(i, 1, QTableWidgetItem(""))

    def _get_delivery_phone_map(self) -> dict[str, str]:
        """테이블에서 {이름: 연락처} 매핑 반환"""
        mapping = {}
        for row in range(self.delivery_table.rowCount()):
            name_item = self.delivery_table.item(row, 0)
            phone_item = self.delivery_table.item(row, 1)
            if name_item:
                name = name_item.text().strip()
                phone = phone_item.text().strip() if phone_item else ""
                if name:
                    mapping[name] = phone
        return mapping

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
        if not self._invoices:
            QMessageBox.warning(
                self, "알림", "청구서 데이터가 없습니다. 청구서 탭에서 먼저 생성하세요."
            )
            return

        # 배송담당자 컬럼 + 연락처 매핑
        delivery_col = self.delivery_col_combo.currentData()
        phone_map = self._get_delivery_phone_map()

        # 첫 번째 고유명의 연락처를 대표값으로 사용
        delivery_person = ""
        delivery_phone = ""
        if delivery_col and self._invoices:
            # 첫 번째 인보이스에서 배송담당자명 읽기
            delivery_person = str(self._invoices[0].get(delivery_col, "")).strip()
            if delivery_person in phone_map:
                delivery_phone = phone_map[delivery_person]
            elif phone_map:
                # 대표값: 맵핑의 첫 번째 전화번호
                delivery_phone = next(iter(phone_map.values()))

        try:
            self._save_settings()

            self.confirms = make_confirm(
                roster_data=self._invoices,
                col_mapping=self._col_mapping,
                output_dir=self.output_dir,
                template_path=self._s.get(
                    "templates", "confirm_path", default="assets/확인서템플릿.xlsx"
                ),
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
        except ValueError as e:
            QMessageBox.warning(self, "템플릿 오류", str(e))
        except Exception as e:
            QMessageBox.critical(self, "생성 오류", f"확인서 생성 실패:\n{e}")

    def _update_table(self):
        show = self.confirms[: self.MAX_RESULT_ROWS]
        self.confirm_table.setRowCount(len(show))
        for i, cf in enumerate(show):
            self.confirm_table.setItem(i, 0, QTableWidgetItem(str(i + 1)))
            self.confirm_table.setItem(i, 1, QTableWidgetItem(cf["name"]))
            self.confirm_table.setItem(i, 2, QTableWidgetItem(cf["dept"]))
            self.confirm_table.setItem(i, 3, QTableWidgetItem(f"{cf['amount']:,}원"))
            self.confirm_table.setItem(i, 4, QTableWidgetItem(cf["file"]))

        self.info_label.setText(
            f"생성된 확인서: {len(self.confirms)}건 (처음 {len(show)}건 표시)"
        )

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
        roster_headers: list[str] | None = None,
        roster_path: str | None = None,
    ):
        """청구서 데이터 + 매핑 + 제외일 전달받기"""
        self._invoices = invoices
        if col_mapping:
            self._col_mapping = col_mapping
        self._year = year
        self._month = month
        self._excluded_dates = excluded_dates or set()
        self._roster_path = roster_path
        self.generate_btn.setEnabled(bool(invoices))

        # 배송담당자 컬럼 콤보박스 채우기
        self._delivery_col_combo_update(roster_headers)

        # 저장된 배송담당자 컬럼/전화번호 복원
        saved_col = self._settings.value("confirm/delivery_col", "")
        if saved_col:
            idx = self.delivery_col_combo.findData(saved_col)
            if idx >= 0:
                self.delivery_col_combo.setCurrentIndex(idx)

        # 테이블이 채워진 후 전화번호 복원 (cellChanged signal flood 방지)
        self.delivery_table.blockSignals(True)
        phone_map = self._settings.value("confirm/delivery_phone_map", None)
        if isinstance(phone_map, dict) and phone_map:
            for row in range(self.delivery_table.rowCount()):
                name_item = self.delivery_table.item(row, 0)
                if name_item and name_item.text() in phone_map:
                    self.delivery_table.setItem(
                        row, 1, QTableWidgetItem(phone_map[name_item.text()])
                    )
        self.delivery_table.blockSignals(False)

        if invoices:
            self.info_label.setText(
                f"청구서 {len(invoices)}건 로드됨. 생성 버튼을 누르면 확인서를 만듭니다."
            )

    def _delivery_col_combo_update(self, roster_headers: list[str] | None = None):
        """콤보박스 아이템 채우기"""
        self.delivery_col_combo.blockSignals(True)
        self.delivery_col_combo.clear()
        self.delivery_col_combo.addItem("(선택 안 함)", "")

        # roster_headers 우선, 없으면 invoice 키에서 추출
        cols = (
            roster_headers
            if roster_headers
            else (list(self._invoices[0].keys()) if self._invoices else [])
        )
        for c in cols:
            # 내부 키 제외
            if c.startswith("_"):
                continue
            self.delivery_col_combo.addItem(c, c)
        self.delivery_col_combo.blockSignals(False)

        # 기존 col_mapping에 배송담당 컬럼이 있으면 자동 선택
        if self._col_mapping.get("roster_delivery_person_col"):
            old = self._col_mapping["roster_delivery_person_col"]
            idx = self.delivery_col_combo.findData(old)
            if idx >= 0:
                self.delivery_col_combo.setCurrentIndex(idx)
                self._on_delivery_col_changed()

    def get_confirms(self):
        return self.confirms
