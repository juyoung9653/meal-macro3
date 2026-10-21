"""User-confirmed historical restoration and named branches in one database."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QInputDialog, QLabel, QLineEdit, QMessageBox, QRadioButton, QVBoxLayout, QWidget,
)

from src.services.revision_branches import checkout_branch, create_branch
from src.services.revision_restore import restore_revision
from src.storage.database_session import DatabaseSession
from src.storage.encrypted_database import RevisionConflictError
from .dialog_workflow import DialogRequest, dialog_workflow
from .import_controller import current_windows_actor
from .theme import application_stylesheet
from .startup_loading import run_progress_task


@dataclass(frozen=True)
class RevisionRestoreSelection:
    branch: bool
    name: str = ""


class RevisionRestoreDialog(QDialog):
    def __init__(self, source_path, code, parent=None):
        super().__init__(parent)
        self.setWindowTitle("이전 변경번호 복구·분기")
        self.setStyleSheet(application_stylesheet())
        self.resize(650, 390)
        self.setMinimumWidth(560)
        root = QVBoxLayout(self)
        intro = QLabel(
            f"선택한 변경번호: {code}\n명단·상태 이력·운영 파일 설정 전체를 이 시점의 저장값으로 되돌립니다. "
            "변경 기록과 문서 생성 기록은 모두 보존합니다. PC 화면 설정은 바꾸지 않습니다."
        )
        intro.setWordWrap(True)
        root.addWidget(intro)
        self.branch_radio = QRadioButton("새 분기를 만들고 전환 (같은 운영 파일)")
        self.restore_radio = QRadioButton("현재 분기에 복구 기록 추가")
        self.branch_radio.setChecked(True)
        root.addWidget(self.branch_radio)
        root.addWidget(self.restore_radio)
        self.branch_fields = QWidget()
        form = QFormLayout(self.branch_fields)
        self.name_edit = QLineEdit(f"분기 {code}")
        self.name_edit.setAccessibleName("새 분기 이름")
        form.addRow("분기 이름", self.name_edit)
        root.addWidget(self.branch_fields)
        self.branch_radio.toggled.connect(self.branch_fields.setEnabled)
        note = QLabel(
            "새 분기는 선택한 변경번호부터 시작합니다. 현재 분기의 작업은 그대로 남으며, "
            "전체 변경 이력에서 분기를 골라 다시 전환할 수 있습니다. 분기끼리 합치지는 않습니다.\n"
            "저장 후 프로그램을 종료합니다. 다시 실행하면 선택한 분기의 명단과 설정 전체를 읽습니다."
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
        name = self.name_edit.text().strip()
        if not name:
            raise ValueError("분기 이름을 입력해주세요.")
        return RevisionRestoreSelection(True, name)


class RevisionRestoreController:
    def __init__(self, session, settings, on_restart, *, dialog_type=RevisionRestoreDialog,
                 message_box=QMessageBox, actor_provider=current_windows_actor,
                 restore_service=restore_revision, branch_service=create_branch,
                 checkout_service=checkout_branch):
        self.session, self.settings, self.on_restart = session, settings, on_restart
        self.dialog_type, self.messages, self.actor_provider = dialog_type, message_box, actor_provider
        self.restore_service, self.branch_service = restore_service, branch_service
        self.checkout_service = checkout_service

    def _head(self):
        latest = self.session.database.latest_revision()
        return None if latest is None else latest.metadata.revision_id

    @dialog_workflow
    def run(self, revision_id, parent=None):
        if revision_id is None:
            return
        try:
            head = self._head()
            code = self.session.database.revision_display_code(revision_id)
            dialog = self.dialog_type(self.session.path, code, parent)
            if (yield DialogRequest(dialog, self.session)) != QDialog.DialogCode.Accepted:
                return
            choice = dialog.selection()
            details = (
                f"{code}에서 새 분기 ‘{choice.name}’를 만들고 전환합니다.\n"
                "같은 운영 파일에 저장하며 현재 분기의 작업과 기존 이력을 보존합니다.\n"
                "프로그램을 종료합니다. 다시 실행하면 새 분기의 명단과 설정을 읽습니다."
                if choice.branch else
                f"현재 분기를 {code}의 명단과 운영 파일 설정 전체로 복구하고 종료합니다. "
                "새 변경번호로 저장하며 기존 변경 이력은 보존합니다."
            )
            if self.messages.question(parent, "변경번호 복구·분기 확인", details,
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                    QMessageBox.StandardButton.Cancel) != QMessageBox.StandardButton.Yes:
                return
            actor = self.actor_provider()
            if choice.branch:
                result = run_progress_task(
                    lambda: self.branch_service(self.session, revision_id, head, actor, name=choice.name),
                    title="새 분기로 전환하는 중입니다",
                    message="선택한 기록에서 분기를 만들고 명단과 설정을 적용하고 있습니다.",
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
            self._conflict(parent)
            return
        except ValueError:
            self.messages.warning(parent, "복구·분기 내용을 확인해주세요", "분기 이름의 중복 여부와 선택한 변경번호를 확인해주세요. 다른 분기의 기록이면 새 분기 만들기 또는 분기 전환을 사용해주세요.")
            return
        except Exception:
            # Ledger payloads/passwords must never appear in exception text.
            self.messages.critical(parent, "복구·분기를 완료하지 못했습니다", "운영 파일의 연결과 쓰기 권한을 확인해주세요. 변경 이력을 다시 열어 현재 분기를 확인해주세요.")
            return
        self._complete(result, "분기 생성·전환 완료" if choice.branch else "복구 완료", parent)

    def checkout(self, branch_id, name, parent=None):
        """Switch the active branch with an expected-head guard and full reload."""
        try:
            head = self._head()
            if self.messages.question(
                parent, "분기 전환 확인",
                f"‘{name}’ 분기의 마지막 저장 상태로 전환하고 프로그램을 종료합니다.\n"
                "현재 분기의 작업은 보존합니다. 다시 실행하면 선택한 분기의 명단과 설정 전체를 읽습니다.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            ) != QMessageBox.StandardButton.Yes:
                return
            actor = self.actor_provider()
            result = run_progress_task(
                lambda: self.checkout_service(self.session, branch_id, head, actor),
                title="분기를 전환하는 중입니다",
                message="선택한 분기의 명단과 설정을 적용하고 있습니다.",
                parent=parent,
            )
        except RevisionConflictError:
            self._conflict(parent)
            return
        except Exception:
            self.messages.critical(parent, "분기를 전환하지 못했습니다", "운영 파일의 연결과 쓰기 권한을 확인하고 변경 이력을 다시 열어주세요.")
            return
        self._complete(result, "분기 전환 완료", parent)

    def _conflict(self, parent):
        self.messages.warning(parent, "변경번호가 달라졌습니다", "작업 중 새 변경이 저장되었습니다. 변경 이력을 닫고 다시 선택해주세요.")

    def _complete(self, result, title, parent):
        if result.revision_id is None:
            self.messages.information(parent, "변경 없음", "현재 분기와 저장값이 선택한 내용과 같습니다.")
            return
        if result.backup.error is not None:
            self.messages.warning(parent, "저장 완료 · 자동 백업 실패", "운영 파일 저장은 완료했으나 자동 백업을 만들지 못했습니다. 백업 폴더 연결과 쓰기 권한을 확인해주세요.")
        self.messages.information(parent, title, "같은 운영 파일에 저장했습니다. 프로그램을 종료합니다. 다시 실행하면 선택한 분기의 명단과 설정 전체를 읽습니다.")
        self.on_restart()

    def _switch(self, path, parent):
        try:
            self.settings.save_database_path(path)
        except Exception:
            self.messages.warning(parent, "파일은 보존됨 · 전환 실패", f"다음 실행 파일 위치를 저장하지 못했습니다. 현재 파일을 계속 사용합니다. 새 파일은 {path}에 보존됩니다.")
            return
        self.on_restart()

    def open_existing(self, parent=None):
        """Compatibility entry point for explicitly opening another database file."""
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
