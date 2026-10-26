"""Manual Juso update and PC-local API key settings."""
from __future__ import annotations

from datetime import UTC, datetime

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QMessageBox, QPushButton, QRadioButton, QTextEdit, QVBoxLayout)

from src.services.juso_api import JusoApiClient, TEST_ADDRESS
from src.services.juso_credentials import CredentialStore
from src.services.old_address_update import (NEW_COLUMN, prepare_address_update,
    source_options, target_options, validate_columns)
from src.storage.database_session import committed_roster_after_save
from src.storage.encrypted_database import RevisionConflictError
from .dialog_workflow import DialogRequest, dialog_workflow
from .import_controller import current_windows_actor
from .startup_loading import run_progress_task, save_revision
from .theme import application_stylesheet, register_application_fonts


def _shell(dialog, title):
    register_application_fonts()
    dialog.setWindowTitle(title)
    dialog.setStyleSheet(application_stylesheet())
    dialog.resize(640, 460)
    dialog.setMinimumSize(560, 400)
    root = QVBoxLayout(dialog)
    root.setContentsMargins(22, 20, 22, 20)
    root.setSpacing(14)
    heading = QLabel(title)
    heading.setObjectName("workspaceTitle")
    root.addWidget(heading)
    return root


def _note(text):
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    label.setObjectName("fieldHelp")
    return label


def _button(text, callback, primary=False):
    button = QPushButton(text)
    button.setObjectName("primaryAction" if primary else "quietAction")
    button.clicked.connect(callback)
    return button


class JusoKeyDialog(QDialog):
    def __init__(self, parent=None, *, store=None, client_type=JusoApiClient, message_box=QMessageBox):
        super().__init__(parent)
        self._store = store or CredentialStore()
        self._client_type, self._message_box = client_type, message_box
        root = _shell(self, "주소 API 키")
        root.addWidget(_note("주소정보누리집의 도로명주소 검색 API 승인키를 입력해주세요.\n"
            "이 PC의 현재 Windows 사용자에게만 저장됩니다. 다른 PC나 Windows 계정에서는 한 번씩 입력해주세요. "
            "운영 DB·백업·공통 설정 파일에는 키를 넣지 않습니다."))
        self.status = _note("")
        root.addWidget(self.status)
        self.key_edit = QLineEdit()
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_edit.setPlaceholderText("새 승인키 입력 · 빈칸이면 기존 키 유지")
        self.key_edit.setAccessibleName("도로명주소 검색 API 승인키")
        root.addWidget(self.key_edit)
        show = QCheckBox("입력한 키 표시")
        show.toggled.connect(lambda checked: self.key_edit.setEchoMode(
            QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password))
        root.addWidget(show)
        actions = QHBoxLayout()
        actions.addWidget(_button("키 확인", self._check))
        actions.addWidget(_button("저장된 키 삭제", self._delete))
        actions.addStretch()
        actions.addWidget(_button("키 저장", self._save, True))
        root.addLayout(actions)
        root.addWidget(_note("키 확인은 공개 예제 주소 1건을 조회합니다. 명단은 변경하지 않습니다."))
        root.addStretch()
        root.addWidget(_button("닫기", self.reject))
        self._refresh()

    def _refresh(self):
        try:
            self.status.setText("이 PC에 API 키가 저장되어 있습니다." if self._store.get_api_key()
                                else "저장된 API 키가 없습니다.")
        except Exception as error:
            self.status.setText(str(error))

    def _save(self):
        try:
            self._store.save_api_key(self.key_edit.text())
        except Exception as error:
            self._message_box.warning(self, "API 키를 저장하지 못했습니다", str(error))
            return
        self.key_edit.clear()
        self._refresh()

    def _delete(self):
        if self._message_box.question(self, "API 키 삭제", "이 PC에 저장된 주소 API 키를 삭제할까요?") != QMessageBox.StandardButton.Yes:
            return
        try:
            self._store.delete_api_key()
            self.key_edit.clear()
            self._refresh()
        except Exception as error:
            self._message_box.warning(self, "API 키를 삭제하지 못했습니다", str(error))

    def _check(self):
        try:
            key = self.key_edit.text().strip() or self._store.get_api_key()
            if not key:
                raise ValueError("API 키를 입력하거나 저장해주세요.")
            client = self._client_type(key)
            run_progress_task(lambda: client.lookup(TEST_ADDRESS), message="주소 API 승인키를 확인하는 중입니다.", parent=self)
        except Exception:
            self._message_box.warning(self, "API 키 확인 실패", "승인키, 검색 API 종류·사용 기간 및 인터넷 연결을 확인해주세요.")
            return
        self._message_box.information(self, "API 키 확인 완료", "주소 검색에 성공했습니다. 새로 입력한 키는 ‘키 저장’을 눌러 보관해주세요.")


class OldAddressDialog(QDialog):
    def __init__(self, roster, columns, service_day, parent=None):
        super().__init__(parent)
        self._roster = roster
        root = _shell(self, "구주소 업데이트")
        self.setMinimumHeight(440)
        root.addWidget(_note(f"{service_day:%Y-%m-%d} 기준 주소를 사용합니다. 전체 명단 {len(roster.persons)}명이 범위이며, "
            "메인 화면의 검색·필터·숨김 여부와 관계없이 선택한 조건으로 처리합니다."))
        form = QFormLayout()
        form.setVerticalSpacing(10)
        self.source_combo, self.target_combo = QComboBox(), QComboBox()
        self.source_combo.setMinimumHeight(36)
        self.target_combo.setMinimumHeight(36)
        sources, targets = source_options(roster), target_options(roster)
        for column in columns:
            if column.key in sources:
                self.source_combo.addItem(column.label, column.key)
            if column.key in targets:
                self.target_combo.addItem(column.label, column.key)
        self.source_combo.setCurrentIndex(max(0, self.source_combo.findData("address")))
        self.target_combo.addItem("구주소 (새 칼럼 만들기)", NEW_COLUMN)
        old_column = next((key for key, name in targets.items() if name == "구주소"), NEW_COLUMN)
        self.target_combo.setCurrentIndex(self.target_combo.findData(old_column))
        form.addRow("신주소를 읽을 칼럼", self.source_combo)
        form.addRow("구주소를 저장할 칼럼", self.target_combo)
        root.addLayout(form)
        self.empty_only = QRadioButton("구주소가 비어 있는 건만 업데이트")
        self.all_rows = QRadioButton("전체 업데이트 · 기존 구주소 덮어쓰기")
        self.empty_only.setChecked(True)
        root.addWidget(self.empty_only)
        root.addWidget(self.all_rows)
        root.addWidget(_note("결과는 문자형 사용자 칼럼에 저장합니다. 신주소가 비었거나 검색에 실패한 행은 기존 값을 유지합니다. "
            "버튼을 누를 때만 주소정보누리집에 조회하며, 변경 내용은 한 번의 변경 이력과 자동 백업으로 저장합니다."))
        self.error_label = _note("")
        root.addWidget(self.error_label)
        root.addStretch()
        actions = QHBoxLayout()
        actions.addStretch()
        actions.addWidget(_button("취소", self.reject))
        actions.addWidget(_button("업데이트 실행", self.accept, True))
        root.addLayout(actions)

    def accept(self):
        try:
            validate_columns(self._roster, self.source_combo.currentData(), self.target_combo.currentData())
        except ValueError as error:
            self.error_label.setText(str(error))
            return
        super().accept()


class OldAddressController:
    def __init__(self, session, *, store=None, client_type=JusoApiClient,
                 dialog_type=OldAddressDialog, message_box=QMessageBox):
        self._session = session
        self._store = store or CredentialStore()
        self._client_type, self._dialog_type, self._message_box = client_type, dialog_type, message_box

    @dialog_workflow
    def run(self, columns, service_day, parent=None):
        try:
            roster = self._session.database.current_roster()
            if roster is None:
                raise ValueError("먼저 명단을 가져와주세요.")
            snapshot = self._session.database.snapshot(roster.revision_id)
        except Exception as error:
            self._message_box.warning(parent, "명단을 읽지 못했습니다", str(error))
            return None
        dialog = self._dialog_type(roster, columns, service_day, parent)
        while (yield DialogRequest(dialog, self._session)) == QDialog.DialogCode.Accepted:
            try:
                if self._session.database.current_roster().revision_id != roster.revision_id:
                    raise RevisionConflictError("명단이 변경되었습니다.")
                key = self._store.get_api_key()
                if not key:
                    warning = QMessageBox(parent)
                    warning.setIcon(QMessageBox.Icon.Warning)
                    warning.setWindowTitle("주소 API 키가 필요합니다")
                    warning.setText("저장하지 않았습니다.\n주소 API 승인키를 먼저 저장해주세요.")
                    input_button = warning.addButton("API키 입력", QMessageBox.ButtonRole.ActionRole)
                    confirm_button = warning.addButton("확인", QMessageBox.ButtonRole.RejectRole)
                    warning.setDefaultButton(input_button)
                    warning.setEscapeButton(confirm_button)
                    yield DialogRequest(warning)
                    if warning.clickedButton() is input_button:
                        key_dialog = JusoKeyDialog(parent=parent, store=self._store,
                            client_type=self._client_type, message_box=self._message_box)
                        key_dialog.key_edit.setFocus()
                        yield DialogRequest(key_dialog)
                    continue
                source, target = dialog.source_combo.currentData(), dialog.target_combo.currentData()
                only_empty = dialog.empty_only.isChecked()
                client = self._client_type(key)
                actor, recorded_at = current_windows_actor(), datetime.now(UTC)
                result = run_progress_task(lambda: prepare_address_update(
                    roster, snapshot, client, source=source, target=target, only_empty=only_empty,
                    service_day=service_day, actor=actor, recorded_at=recorded_at),
                    message="신주소를 조회해 구주소로 변환하는 중입니다.", parent=parent)
                backup = save_revision(self._session, result.record, parent=parent) if result.record else None
            except RevisionConflictError:
                dialog.error_label.setText("명단이 변경되어 저장하지 않았습니다. 취소하고 다시 열어주세요.")
                continue
            except Exception as error:
                # API/network exceptions may contain echoed secrets; expose only known local messages.
                text = str(error) if isinstance(error, ValueError) else "API 키·인터넷 연결 또는 운영 파일 상태를 확인해주세요."
                dialog.error_label.setText("저장하지 않았습니다. " + text)
                continue
            summary = (f"변경 {result.updated}명 · 동일 {result.unchanged}명 · 건너뜀 {result.skipped}명 · 실패 {len(result.failures)}명")
            if backup is not None and backup.error is not None:
                summary += "\n주소는 저장됐지만 자동 백업에 실패했습니다. 백업 폴더를 확인해주세요."
            report = QDialog(parent)
            layout = _shell(report, "구주소 업데이트 결과")
            layout.addWidget(_note(summary))
            if result.failures:
                details = QTextEdit()
                details.setReadOnly(True)
                details.setPlainText("기존 값을 유지한 대상\n" + "\n".join(result.failures))
                layout.addWidget(details)
            layout.addWidget(_button("닫기", report.accept, True))
            # Refresh the main roster before opening the result window.
            committed = committed_roster_after_save(self._session, result.roster) if result.record else None
            if committed is not None and callable(getattr(self, "on_saved", None)):
                self.on_saved(committed)
            yield DialogRequest(report)
            return committed
        return None
