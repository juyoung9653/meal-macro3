"""User-confirmed historical restoration and branch switching."""
from __future__ import annotations

from .dialog_workflow import DialogRequest, dialog_workflow

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMessageBox,
    QPushButton, QRadioButton, QVBoxLayout, QWidget,
)

from src.services.revision_restore import create_revision_branch, restore_revision
from src.storage.database_session import DatabaseSession
from src.storage.encrypted_database import RevisionConflictError
from .import_controller import current_windows_actor
from .theme import application_stylesheet
from .startup_loading import run_progress_task


@dataclass(frozen=True)
class RevisionRestoreSelection:
    branch: bool
    destination: Path | None = None
    password: str = ""
    name: str = ""


class RevisionRestoreDialog(QDialog):
    def __init__(self, source_path, code, parent=None):
        super().__init__(parent)
        self.setWindowTitle("이전 변경번호 복구·분기")
        self.setStyleSheet(application_stylesheet())
        self.resize(650, 550)
        self.setMinimumWidth(560)
        root = QVBoxLayout(self)
        intro = QLabel(
            f"선택한 변경번호: {code}\n명단·상태 이력·운영 파일 설정 전체를 이 시점의 저장값으로 되돌립니다. "
            "변경 기록과 문서 생성 기록은 모두 보존합니다. PC 화면 설정은 바꾸지 않습니다."
        )
        intro.setWordWrap(True)
        root.addWidget(intro)
        self.branch_radio = QRadioButton("새 운영 파일로 분기 (현재 파일 보존)")
        self.restore_radio = QRadioButton("현재 운영 파일에 복구 기록 추가 후 종료")
        self.branch_radio.setChecked(True)
        root.addWidget(self.branch_radio)
        root.addWidget(self.restore_radio)
        self.branch_fields = QWidget()
        form = QFormLayout(self.branch_fields)
        self.name_edit = QLineEdit(f"분기 {code}")
        self.path_edit = QLineEdit(str(Path(source_path).with_name(f"{Path(source_path).stem}-분기-{code}.sqlite3")))
        path_row = QHBoxLayout()
        path_row.addWidget(self.path_edit, 1)
        browse = QPushButton("위치 선택")
        browse.clicked.connect(self._browse)
        path_row.addWidget(browse)
        self.password_edit = QLineEdit()
        self.confirm_edit = QLineEdit()
        for edit in (self.password_edit, self.confirm_edit):
            edit.setEchoMode(QLineEdit.EchoMode.Password)
        show = QCheckBox("비밀번호 표시")
        show.toggled.connect(lambda checked: [edit.setEchoMode(QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password) for edit in (self.password_edit, self.confirm_edit)])
        form.addRow("분기 이름", self.name_edit)
        form.addRow("새 파일 위치", path_row)
        form.addRow("분기 파일 비밀번호", self.password_edit)
        form.addRow("비밀번호 확인", self.confirm_edit)
        form.addRow(show)
        root.addWidget(self.branch_fields)
        self.branch_radio.toggled.connect(self.branch_fields.setEnabled)
        note = QLabel(
            "새 분기는 별도의 암호화 파일과 비밀번호 정보를 만듭니다. 같은 이름의 파일은 덮어쓰지 않습니다. "
            "완료 후 분기로 전환할지 선택할 수 있습니다. 현재 파일 복구는 새 변경번호로 저장하고 종료하며, 다시 실행하면 전체 설정을 다시 읽습니다."
        )
        note.setWordWrap(True)
        root.addWidget(note)
        self.error_label = QLabel()
        self.error_label.setWordWrap(True)
        root.addWidget(self.error_label)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("계속")
        buttons.button(QDialogButtonBox.StandardButton.Ok).setObjectName("primaryAction")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("취소")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setObjectName("quietAction")
        buttons.accepted.connect(self._submit)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _browse(self):
        value, _ = QFileDialog.getSaveFileName(
            self, "새 분기 파일 위치", self.path_edit.text(), "급식 운영 데이터 (*.sqlite3)",
            options=QFileDialog.Option.DontConfirmOverwrite,
        )
        if value:
            self.path_edit.setText(str(Path(value).with_suffix(".sqlite3")))

    def _submit(self):
        try:
            self.selection()
        except ValueError as error:
            self.error_label.setText(str(error))
            return
        self.accept()

    def selection(self):
        if not self.branch_radio.isChecked():
            return RevisionRestoreSelection(False)
        if not self.name_edit.text().strip() or not self.path_edit.text().strip():
            raise ValueError("분기 이름과 새 파일 위치를 입력해주세요.")
        if not self.password_edit.text():
            raise ValueError("분기 파일 비밀번호를 입력해주세요.")
        if self.password_edit.text() != self.confirm_edit.text():
            raise ValueError("비밀번호 확인이 일치하지 않습니다.")
        return RevisionRestoreSelection(True, Path(self.path_edit.text().strip()), self.password_edit.text(), self.name_edit.text().strip())


class RevisionRestoreController:
    def __init__(self, session, settings, on_restart, *, dialog_type=RevisionRestoreDialog,
                 message_box=QMessageBox, actor_provider=current_windows_actor,
                 restore_service=restore_revision, branch_service=create_revision_branch):
        self.session, self.settings, self.on_restart = session, settings, on_restart
        self.dialog_type, self.messages, self.actor_provider = dialog_type, message_box, actor_provider
        self.restore_service, self.branch_service = restore_service, branch_service

    @dialog_workflow
    def run(self, revision_id, parent=None):
        if revision_id is None:
            return
        try:
            head = self.session.database.latest_revision().metadata.revision_id
            code = self.session.database.revision_display_code(revision_id)
            dialog = self.dialog_type(self.session.path, code, parent)
            if (yield DialogRequest(dialog, self.session)) != QDialog.DialogCode.Accepted:
                return
            choice = dialog.selection()
            details = (
                f"{code}의 상태로 새 분기를 만듭니다.\n분기: {choice.name}\n저장 위치: {choice.destination}\n현재 파일과 모든 기존 이력을 보존합니다."
                if choice.branch else
                f"현재 파일: {self.session.path}\n{code}의 명단과 운영 파일 설정 전체를 새 변경번호로 복구하고 종료합니다. 기존 변경 이력은 보존합니다."
            )
            if self.messages.question(parent, "변경번호 복구 확인", details,
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                    QMessageBox.StandardButton.Cancel) != QMessageBox.StandardButton.Yes:
                return
            actor = self.actor_provider()
            if choice.branch:
                result = run_progress_task(
                    lambda: self.branch_service(self.session, revision_id, head, actor,
                                                choice.destination, choice.password, name=choice.name),
                    title="이전 내용으로 새 파일을 만드는 중입니다",
                    message="선택한 기록의 명단과 설정을 새 운영 파일에 저장하고 있습니다.",
                    parent=parent,
                )
            else:
                result = run_progress_task(
                    lambda: self.restore_service(self.session, revision_id, head, actor),
                    title="이전 내용으로 되돌리는 중입니다",
                    message="선택한 기록의 명단과 설정을 복구하고 있습니다.",
                    parent=parent,
                )
        except RevisionConflictError:
            self.messages.warning(parent, "변경번호가 달라졌습니다", "작업 중 새 변경이 저장되었습니다. 변경 이력을 닫고 다시 선택해주세요.")
            return
        except Exception:
            # Ledger payloads/passwords must never appear in exception text.
            self.messages.critical(parent, "복구·분기를 완료하지 못했습니다", "저장 위치와 파일 이름, 비밀번호를 확인해주세요. 기존 파일은 보존됩니다.")
            return
        if result.backup.error is not None:
            self.messages.warning(parent, "저장 완료 · 자동 백업 실패", "운영 파일 저장은 완료했으나 자동 백업을 만들지 못했습니다. 백업 폴더 연결과 쓰기 권한을 확인해주세요.")
        if not choice.branch:
            if result.revision_id is None:
                self.messages.information(parent, "변경 없음", "현재 저장값이 선택한 변경번호와 같습니다.")
                return
            self.messages.information(parent, "복구 완료", "새 변경번호로 복구했습니다. 프로그램을 종료합니다. 다시 실행하면 복구한 파일을 사용합니다.")
            self.on_restart()
            return
        self._offer_switch(result.path, parent)

    def _offer_switch(self, path, parent):
        answer = self.messages.question(
            parent, "분기 생성 완료",
            f"새 분기: {path}\n원본: {self.session.path}\n새 분기로 전환하고 종료할까요? "
            "다음 실행부터 분기를 사용합니다. 아니오를 선택하면 현재 파일을 계속 사용합니다. "
            "더보기 → 전체 변경 이력 → 다른 운영 파일 열기로 다시 전환할 수 있습니다.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._switch(path, parent)

    def _switch(self, path, parent):
        try:
            self.settings.save_database_path(path)
        except Exception:
            self.messages.warning(parent, "파일은 보존됨 · 전환 실패", f"다음 실행 파일 위치를 저장하지 못했습니다. 현재 파일을 계속 사용합니다. 새 파일은 {path}에 보존됩니다.")
            return
        self.on_restart()

    def open_existing(self, parent=None):
        filename, _ = QFileDialog.getOpenFileName(parent, "다른 운영 파일 열기", str(self.session.path.parent), "급식 운영 데이터 (*.sqlite3)")
        if not filename:
            return
        path = Path(filename).resolve()
        if path == self.session.path.resolve():
            self.messages.information(parent, "현재 운영 파일", "이미 사용 중인 파일입니다.")
            return
        password, accepted = QInputDialog.getText(parent, "운영 파일 열기", "선택한 파일의 비밀번호", QLineEdit.EchoMode.Password)
        if not accepted:
            return
        def open_and_check():
            opened = DatabaseSession.open(path, password)
            try:
                opened.database.check_integrity()
            except Exception:
                opened.close()
                raise
            return opened

        try:
            with run_progress_task(
                open_and_check,
                title="운영 파일을 확인하는 중입니다",
                message="선택한 파일과 저장된 내용을 확인하고 있습니다.",
                parent=parent,
            ) as opened:
                if self.messages.question(parent, "운영 파일 전환", f"{path} 파일로 전환하고 종료할까요? 다음 실행부터 선택한 파일을 사용합니다.",
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                        QMessageBox.StandardButton.Cancel) != QMessageBox.StandardButton.Yes:
                    return
                self._switch(path, parent)
        except Exception:
            self.messages.critical(parent, "운영 파일을 열지 못했습니다", "파일과 비밀번호 정보, 입력한 비밀번호를 확인해주세요. 현재 파일을 계속 사용합니다.")
