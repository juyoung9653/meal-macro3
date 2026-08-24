"""명단 인적사항 불일치를 별도 파일로 만드는 검토 화면."""

from __future__ import annotations

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

from ..core.delivery_matrix import AnnualDeliveryBook
from ..core.settings import get_settings
from ..core.user_preferences import UserPreferences
from ..generators.review_generator import make_profile_review


class ReviewTab(QWidget):
    """청구서와 무관하게 명단 불일치를 검토 파일로 출력한다."""

    def __init__(self, preferences: UserPreferences | None = None):
        super().__init__()
        self._book: AnnualDeliveryBook | None = None
        self.output_dir: str | None = None
        self._last_output_path: str | None = None
        self._preferences = preferences or UserPreferences(get_settings())
        self._setup_ui()
        self._restore_state()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)

        info_label = QLabel(
            "명단에서 같은 사람의 인적사항이 서로 다른 항목만 별도 파일로 만듭니다."
        )
        info_label.setStyleSheet("color: #666; font-style: italic;")
        layout.addWidget(info_label)

        path_group = QGroupBox("검토용 파일 저장 위치")
        path_layout = QHBoxLayout(path_group)
        self.path_label = QLabel("선택된 경로 없음")
        self.path_label.setStyleSheet("color: #888;")
        self.path_btn = QPushButton("폴더 선택")
        self.path_btn.clicked.connect(self._select_output_dir)
        path_layout.addWidget(self.path_label, stretch=1)
        path_layout.addWidget(self.path_btn)
        layout.addWidget(path_group)

        button_layout = QHBoxLayout()
        button_layout.addStretch()
        self.generate_btn = QPushButton("검토용 시트 만들기")
        self.generate_btn.setEnabled(False)
        self.generate_btn.clicked.connect(self._generate_review)
        self.open_file_btn = QPushButton("파일 열기")
        self.open_file_btn.setEnabled(False)
        self.open_file_btn.clicked.connect(self._open_output_file)
        button_layout.addWidget(self.generate_btn)
        button_layout.addWidget(self.open_file_btn)
        layout.addLayout(button_layout)

        table_group = QGroupBox("명단 불일치 미리보기")
        table_layout = QVBoxLayout(table_group)
        self.review_table = QTableWidget()
        self.review_table.setColumnCount(5)
        self.review_table.setHorizontalHeaderLabels(
            [
                "성명",
                "생년월일",
                "핸드폰",
                "불일치 항목",
                "원본별 값",
            ]
        )
        self.review_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.review_table.setAlternatingRowColors(True)
        self.review_table.setWordWrap(True)
        self.review_table.setTextElideMode(Qt.TextElideMode.ElideNone)
        header = self.review_table.horizontalHeader()
        if header is not None:
            for column in range(4):
                header.setSectionResizeMode(
                    column,
                    QHeaderView.ResizeMode.ResizeToContents,
                )
            header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        vertical_header = self.review_table.verticalHeader()
        if vertical_header is not None:
            vertical_header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        table_layout.addWidget(self.review_table)
        layout.addWidget(table_group, stretch=1)

        self.info_label = QLabel("db 파일을 불러오세요.")
        self.info_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        layout.addWidget(self.info_label)

    def _restore_state(self) -> None:
        self.set_output_dir(self._preferences.get_output_dir())

    def _select_output_dir(self) -> None:
        selected = QFileDialog.getExistingDirectory(
            self,
            "검토용 파일 저장 폴더 선택",
            self.output_dir or "",
        )
        if selected:
            self.set_output_dir(selected)

    def _generate_review(self) -> None:
        if self._book is None:
            QMessageBox.warning(self, "알림", "db 파일을 먼저 불러오세요.")
            return
        if not self.output_dir:
            QMessageBox.warning(self, "알림", "저장 위치를 먼저 선택하세요.")
            return

        output_path = os.path.join(
            self.output_dir,
            f"DB검토_{self._book.year}.xlsx",
        )
        try:
            count = make_profile_review(self._book.profile_conflicts, output_path)
        except Exception as error:  # noqa: BLE001 - 사용자용 오류로 표시
            QMessageBox.critical(
                self,
                "생성 오류",
                f"검토용 파일 생성 실패:\n{error}",
            )
            return

        self._last_output_path = output_path
        self.open_file_btn.setEnabled(True)
        QMessageBox.information(
            self,
            "완료",
            f"명단 불일치 {count}건을 검토용 파일로 만들었습니다.\n{output_path}",
        )

    def _open_output_file(self) -> None:
        if self._last_output_path and os.path.exists(self._last_output_path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(self._last_output_path))
            return
        QMessageBox.warning(
            self,
            "알림",
            "열 파일이 없습니다. 먼저 검토용 파일을 만드세요.",
        )

    def set_annual_book(self, book: AnnualDeliveryBook | None) -> None:
        self._book = book
        self._last_output_path = None
        self.open_file_btn.setEnabled(False)
        conflicts = book.profile_conflicts if book is not None else ()
        self.review_table.setRowCount(len(conflicts))
        for row_index, conflict in enumerate(conflicts):
            values = (
                conflict.name,
                conflict.birth_date,
                conflict.phone,
                conflict.field,
                "\n".join(
                    f"{row}행: {value}" for row, value in conflict.values_by_row
                ),
            )
            for column, value in enumerate(values):
                text = "" if value is None else str(value)
                item = QTableWidgetItem(text)
                item.setToolTip(text)
                self.review_table.setItem(row_index, column, item)
        self.review_table.resizeRowsToContents()

        self.generate_btn.setEnabled(book is not None)
        if book is None:
            self.info_label.setText("db 파일을 불러오세요.")
        elif conflicts:
            self.info_label.setText(f"검토할 명단 불일치: {len(conflicts)}건")
        else:
            self.info_label.setText("검토할 명단 불일치가 없습니다.")

    def set_output_dir(self, path: str | None) -> None:
        if not path:
            return
        self.output_dir = str(path)
        self.path_label.setText(self.output_dir)
        self.path_label.setStyleSheet("color: #000;")
        self._preferences.set_output_dir(self.output_dir)
