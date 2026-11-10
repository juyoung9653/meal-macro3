"""Edit optional details without changing the original meal action."""
from datetime import UTC, datetime
from dataclasses import replace
from uuid import uuid4
from PySide6.QtCore import Qt

from PySide6.QtWidgets import QComboBox, QDialog, QFormLayout, QHBoxLayout, QLabel, QMessageBox, QPlainTextEdit, QPushButton, QVBoxLayout

from src.domain.intake import IntakeChannel
from src.domain.meal_history_details import build_meal_history_details_revision
from src.storage.encrypted_database import RevisionConflictError
from src.storage.database_session import committed_roster_after_save
from .dialog_workflow import DialogRequest, dialog_workflow
from .import_controller import current_windows_actor
from .ledger_reader import load_revision_chain
from .meal_history import build_meal_history_entries_by_person
from .startup_loading import run_progress_task, save_revision
from .theme import application_stylesheet
from .user_messages import user_error


class MealHistoryDetailsDialog(QDialog):
    def __init__(self, name, row, parent=None):
        super().__init__(parent)
        self.setWindowTitle("접수 경로·비고 수정")
        self.setStyleSheet(application_stylesheet())
        self.resize(520, 360)
        layout = QVBoxLayout(self)
        label = QLabel(f"{name} · {row[0]} {row[1]}\n{row[3]}\n변경번호: {row[6]}")
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        layout.addWidget(label)
        form = QFormLayout()
        self.channel = QComboBox()
        self.channel.addItem("-", "")
        for channel in IntakeChannel:
            self.channel.addItem(channel.value, channel.value)
        self.channel.setCurrentIndex(max(0, self.channel.findData(row[2])))
        form.addRow("접수 경로", self.channel)
        self.note = QPlainTextEdit()
        self.note.setPlainText(row[4])
        form.addRow("비고", self.note)
        layout.addLayout(form)
        hint = QLabel("선택한 기록의 접수 경로와 비고를 수정합니다. 수정 전 내용은 전체 변경 이력에 보존됩니다.")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        actions = QHBoxLayout()
        actions.addStretch()
        for text, style, callback in (("취소", "quietAction", self.reject), ("저장", "primaryAction", self.accept)):
            button = QPushButton(text)
            button.setObjectName(style)
            button.clicked.connect(callback)
            actions.addWidget(button)
        layout.addLayout(actions)


class MealHistoryDetailsController:
    def __init__(self, session, *, dialog_type=MealHistoryDetailsDialog,
                 message_box=QMessageBox, actor_provider=current_windows_actor):
        self.session, self.dialog_type = session, dialog_type
        self.messages, self.actor_provider = message_box, actor_provider

    @dialog_workflow
    def run(self, target, expected_revision_id, parent=None):
        try:
            def prepare():
                current = self.session.database.current_roster()
                if current is None or current.revision_id != expected_revision_id:
                    raise RevisionConflictError("이력 조회 이후 명단이 변경되었습니다.")
                snapshot = self.session.database.snapshot(current.revision_id)
                revisions = load_revision_chain(self.session.database, current.revision_id)
                available = {entry.target: (pid, entry)
                             for pid, entries in build_meal_history_entries_by_person(revisions).items()
                             for entry in entries}
                if target not in available:
                    raise ValueError("선택한 식종 변경 이력이 현재 목록에 없습니다. 이력을 다시 열어주세요.")
                pid, entry = available[target]
                return current, snapshot, revisions, current.person(pid).profile.name, entry.row
            current, snapshot, revisions, name, row = run_progress_task(
                prepare, message="수정할 식종 변경 이력을 확인하는 중입니다.", parent=parent)
        except Exception as error:
            self.messages.critical(parent, "식종 이력을 열지 못했습니다", user_error(error))
            return None
        dialog = self.dialog_type(name, row, parent)
        while (yield DialogRequest(dialog, self.session)) == QDialog.DialogCode.Accepted:
            try:
                latest = self.session.database.current_roster()
                if latest is None or latest.revision_id != current.revision_id:
                    raise RevisionConflictError("이력 조회 이후 명단이 변경되었습니다.")
                record = build_meal_history_details_revision(
                    snapshot, revisions, target, dialog.channel.currentData(), dialog.note.toPlainText(),
                    actor=self.actor_provider(), recorded_at=datetime.now(UTC), revision_id=uuid4())
                if record is None:
                    return None
                backup = save_revision(self.session, record, parent=dialog)
            except Exception as error:
                self.messages.critical(dialog, "접수 경로·비고를 저장하지 못했습니다", user_error(error))
                continue
            if backup.warning_required:
                self.messages.warning(parent, "수정은 저장됐지만 자동 백업에 실패했습니다",
                                      "접수 경로·비고와 변경 이력은 저장되었습니다.\n\n" + user_error(backup.error))
            return committed_roster_after_save(self.session, replace(current, revision_id=record.metadata.revision_id))
        return None
