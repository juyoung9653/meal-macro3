"""공용 db 파일에 최신 한-행 명단을 병합하는 전용 화면."""

from __future__ import annotations

import os
from pathlib import Path

from PyQt6.QtCore import QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..excel.annual_workbook import (
    WorkbookPasswordError,
    WorkbookPasswordRequiredError,
)
from ..services.roster_merge_service import (
    RosterMergeRequest,
    RosterMergeResult,
    SourceLayout,
    merge_roster,
    read_source_layout,
)
from .roster_merge_mapping_dialog import RosterMergeMappingDialog


class RosterMergeTab(QWidget):
    """파일 선택·열 매핑·백업 병합을 한 화면에서 수행한다."""

    merge_completed = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self._target_path: Path | None = None
        self._target_mapping: dict[str, str] = {}
        self._source_path: Path | None = None
        self._source_layout: SourceLayout | None = None
        self._source_columns: dict[str, int] = {}
        self._source_password = ""
        self._source_requires_password = False
        self._last_result: RosterMergeResult | None = None
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        info = QLabel(
            "최신 명단을 이름+생년월일 앞 6자리로 대조합니다. "
            "합치기 직전에 원본 db 백업을 만들고 모든 변경·추가 이력을 Excel 로그로 남깁니다."
        )
        info.setWordWrap(True)
        info.setStyleSheet("color: #666;")
        layout.addWidget(info)

        target_group = QGroupBox("공용 db 파일")
        target_layout = QHBoxLayout(target_group)
        self.target_label = QLabel("db 파일을 먼저 불러오세요.")
        self.target_label.setWordWrap(True)
        target_layout.addWidget(self.target_label)
        layout.addWidget(target_group)

        source_group = QGroupBox("합칠 최신 명단 파일")
        source_layout = QHBoxLayout(source_group)
        self.source_label = QLabel("선택된 파일 없음")
        self.source_label.setWordWrap(True)
        self.source_button = QPushButton("명단 파일 선택 및 열 매핑")
        self.source_button.setEnabled(False)
        self.source_button.clicked.connect(self._select_source)
        source_layout.addWidget(self.source_label, stretch=1)
        source_layout.addWidget(self.source_button)
        layout.addWidget(source_group)

        self.mapping_label = QLabel("Excel 열 매핑 대기 중")
        self.mapping_label.setWordWrap(True)
        self.mapping_label.setStyleSheet("color: #888;")
        layout.addWidget(self.mapping_label)

        button_layout = QHBoxLayout()
        button_layout.addStretch()
        self.merge_button = QPushButton("백업 후 명단 합치기")
        self.merge_button.setEnabled(False)
        self.merge_button.clicked.connect(self._run_merge)
        self.open_backup_button = QPushButton("백업 열기")
        self.open_backup_button.setEnabled(False)
        self.open_backup_button.clicked.connect(self._open_backup)
        self.open_log_button = QPushButton("변경 로그 열기")
        self.open_log_button.setEnabled(False)
        self.open_log_button.clicked.connect(self._open_log)
        button_layout.addWidget(self.merge_button)
        button_layout.addWidget(self.open_backup_button)
        button_layout.addWidget(self.open_log_button)
        layout.addLayout(button_layout)

        self.result_label = QLabel("합치기 결과 없음")
        self.result_label.setWordWrap(True)
        self.result_label.setStyleSheet("color: #666;")
        layout.addWidget(self.result_label)
        layout.addStretch()

    def set_target(self, path: str | None, mapping: dict[str, str] | None) -> None:
        self._target_path = Path(path) if path else None
        self._target_mapping = dict(mapping or {})
        if self._target_path is None:
            self.target_label.setText("db 파일을 먼저 불러오세요.")
            self.source_button.setEnabled(False)
        else:
            self.target_label.setText(str(self._target_path))
            self.source_button.setEnabled(True)
        self._update_merge_enabled()

    def _select_source(self) -> None:
        initial = str(self._target_path.parent) if self._target_path else ""
        path, _selected_filter = QFileDialog.getOpenFileName(
            self,
            "합칠 최신 명단 파일 선택",
            initial,
            "Excel 통합 문서 (*.xlsx *.xlsm);;모든 파일 (*.*)",
        )
        if not path:
            return
        opened = self._open_source_with_password(Path(path))
        if opened is None:
            return
        source_layout, password, requires_password = opened
        columns = RosterMergeMappingDialog.get_selection(source_layout, parent=self)
        if columns is None:
            return
        self._source_path = Path(path)
        self._source_layout = source_layout
        self._source_columns = columns
        self._source_password = password
        self._source_requires_password = requires_password
        self.source_label.setText(path)
        selected = ", ".join(
            f"{role}={source_layout.headers[column - 1] or column}"
            for role, column in columns.items()
        )
        self.mapping_label.setText(f"선택한 Excel 열: {selected}")
        self.mapping_label.setStyleSheet("color: #080;")
        self._last_result = None
        self.open_backup_button.setEnabled(False)
        self.open_log_button.setEnabled(False)
        self._update_merge_enabled()

    def _run_merge(self) -> None:
        if (
            self._target_path is None
            or self._source_path is None
            or self._source_layout is None
        ):
            QMessageBox.warning(self, "알림", "db 파일과 합칠 명단 파일을 선택하세요.")
            return
        if self._source_requires_password and not self._source_password:
            opened = self._open_source_with_password(self._source_path)
            if opened is None:
                return
            source_layout, password, _requires_password = opened
            if source_layout != self._source_layout:
                self._source_password = ""
                QMessageBox.warning(
                    self,
                    "명단 파일 변경됨",
                    "합칠 명단 파일의 구조가 바뀌었습니다. 파일과 Excel 열을 다시 선택하세요.",
                )
                return
            self._source_password = password
        answer = QMessageBox.question(
            self,
            "명단 합치기 확인",
            "원본 db 파일의 상태 열을 다시 분류하고 최신 명단을 반영합니다.\n"
            "실행 전에 같은 폴더에 백업을 만든 뒤 진행합니다. 계속할까요?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer is not QMessageBox.StandardButton.Yes:
            self._source_password = ""
            return
        try:
            result = merge_roster(
                RosterMergeRequest(
                    target_path=self._target_path,
                    source_path=self._source_path,
                    target_header_mapping=self._target_mapping,
                    source_layout=self._source_layout,
                    source_columns=self._source_columns,
                    source_password=self._source_password,
                )
            )
        except Exception as error:  # noqa: BLE001 - 사용자용 오류로 표시
            QMessageBox.critical(self, "명단 합치기 오류", str(error))
            return
        finally:
            self._source_password = ""

        self._last_result = result
        self.open_backup_button.setEnabled(True)
        self.open_log_button.setEnabled(True)
        self.result_label.setText(
            f"갱신 {result.updated_people}명 · 기존 {result.existing_people}명 · "
            f"신규 {result.new_people}명 · 명단에 없음 {result.untouched_people}명\n"
            f"변경 셀 {result.changed_cells}개 · 추가 행 {result.added_rows}개\n"
            f"백업: {result.backup_path}\n로그: {result.log_path}"
        )
        self.merge_completed.emit(str(self._target_path))
        QMessageBox.information(
            self,
            "명단 합치기 완료",
            self.result_label.text(),
        )

    def _open_source_with_password(
        self,
        path: Path,
    ) -> tuple[SourceLayout, str, bool] | None:
        password = ""
        requires_password = False
        retry = False
        while True:
            try:
                layout = read_source_layout(path, password=password)
                return layout, password, requires_password
            except WorkbookPasswordRequiredError:
                requires_password = True
            except WorkbookPasswordError:
                requires_password = True
                retry = True
            except Exception as error:  # noqa: BLE001 - 사용자용 오류로 표시
                QMessageBox.critical(self, "명단 파일 오류", str(error))
                return None

            password = self._prompt_source_password(path.name, retry=retry)
            if password is None:
                return None

    def _prompt_source_password(self, filename: str, *, retry: bool) -> str | None:
        message = (
            f"'{filename}'의 비밀번호가 올바르지 않습니다. 다시 입력해 주세요."
            if retry
            else f"'{filename}'의 비밀번호를 입력해 주세요."
        )
        while True:
            password, accepted = QInputDialog.getText(
                self,
                "합칠 명단 파일 비밀번호",
                message,
                QLineEdit.EchoMode.Password,
            )
            if not accepted:
                return None
            if password:
                return password
            message = "비밀번호를 입력해야 합칠 명단 파일을 열 수 있습니다."

    def _update_merge_enabled(self) -> None:
        self.merge_button.setEnabled(
            self._target_path is not None
            and self._source_path is not None
            and self._source_layout is not None
            and bool(self._source_columns)
        )

    def _open_backup(self) -> None:
        if self._last_result is not None:
            _open_file(self._last_result.backup_path)

    def _open_log(self) -> None:
        if self._last_result is not None:
            _open_file(self._last_result.log_path)


def _open_file(path: Path) -> None:
    if path.exists():
        QDesktopServices.openUrl(QUrl.fromLocalFile(os.fspath(path)))
