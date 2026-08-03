import os

from PyQt6.QtCore import Qt, QUrl
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
from ..core.user_preferences import UserPreferences
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
        self._preferences = UserPreferences(self._s)
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
        if hdr2 is not None:
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
        self._preferences.set_output_dir(self.output_dir or "")

    def _restore_state(self):
        raw = self._preferences.get_output_dir()
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

    def _active_batch(self):
        """Return the immutable active-delivery snapshot, if one is available."""
        batch = self._data.batch
        return batch if batch is not None and batch.deliveries else None

    def _generate_confirms(self):
        """확인서 생성"""
        active_batch = self._active_batch()
        if active_batch is None:
            QMessageBox.warning(
                self,
                "알림",
                "실제 배송 건수가 있는 청구서 데이터가 없습니다. "
                "청구서 탭에서 먼저 생성하세요.",
            )
            return
        if not self.output_dir:
            QMessageBox.warning(self, "알림", "출력 경로를 먼저 선택하세요.")
            return

        try:
            self._save_settings()
            self._invalidate_result()

            template_path = self._s.get_confirm_template_path(self._meal_type)
            self.confirms = make_confirm(
                roster_data=active_batch,
                col_mapping=self._data.col_mapping,
                output_dir=self.output_dir,
                template_path=template_path,
                year=self._data.year,
                month=self._data.month,
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
        except Exception as e:  # noqa: BLE001 - 사용자용 오류 대화상자로 변환
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
        self._invalidate_result()
        self._update_input_state()

    def _invalidate_result(self):
        self.confirms.clear()
        self.confirm_table.setRowCount(0)
        self.info_label.setText("생성된 확인서: 0건")
        self._last_output_path = None
        self.open_file_btn.setEnabled(False)

    def _update_input_state(self):
        batch = self._active_batch()
        active_count = len(batch.deliveries) if batch is not None else 0
        self.generate_btn.setEnabled(active_count > 0)
        if active_count:
            self.info_label.setText(
                f"활성 청구서 {active_count}건 로드됨. "
                "생성 버튼을 누르면 확인서를 만듭니다."
            )
        elif self._data.batch is not None:
            self.info_label.setText("실제 배송 건수가 있는 청구서가 없습니다.")
        else:
            self.info_label.setText("청구서 데이터가 없습니다.")

    def _clear(self):
        self._invalidate_result()
        self._update_input_state()

    def set_invoice_data(self, data: InvoiceData):
        """새 청구 결과일 때만 확인서 출력 상태를 초기화한다."""
        if data == self._data:
            self._update_input_state()
            return
        self._data = data
        self._invalidate_result()
        self._update_input_state()
