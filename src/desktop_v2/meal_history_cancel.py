"""Confirmation and atomic persistence for one selected meal history action."""
from datetime import UTC, datetime
from uuid import uuid4

from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QMessageBox, QPushButton, QVBoxLayout

from src.domain.meal_history_cancellation import build_meal_history_cancellation
from src.storage.encrypted_database import RevisionConflictError
from .dialog_workflow import DialogRequest, dialog_workflow
from .import_controller import current_windows_actor
from .ledger_reader import load_revision_chain
from .meal_history import build_meal_history_entries_by_person
from .startup_loading import run_progress_task, save_revision
from .theme import application_stylesheet


class MealHistoryCancelDialog(QDialog):
    def __init__(self, name, row, parent=None):
        super().__init__(parent)
        self.setWindowTitle("식종 변경 취소")
        self.setStyleSheet(application_stylesheet())
        self.setMinimumWidth(440)
        layout = QVBoxLayout(self)
        label = QLabel(f"{name} · {row[0]} {row[1]}\n{row[2]}\n변경번호: {row[4]}")
        label.setWordWrap(True)
        layout.addWidget(label)
        note = QLabel("선택한 한 건을 취소합니다. 이 기록으로 추가한 가입 기간도 함께 되돌립니다. "
                      "나머지 상태 기록은 유지하며, 취소한 항목은 개인 이력에서 사라집니다. "
                      "원본과 취소 기록은 전체 변경 이력에 보존됩니다.")
        note.setWordWrap(True)
        layout.addWidget(note)
        buttons = QHBoxLayout()
        back = QPushButton("돌아가기")
        back.setObjectName("quietAction")
        back.clicked.connect(self.reject)
        confirm = QPushButton("선택한 변경 취소")
        confirm.setObjectName("primaryAction")
        confirm.clicked.connect(self.accept)
        buttons.addWidget(back)
        buttons.addWidget(confirm)
        layout.addLayout(buttons)


class MealHistoryCancelController:
    def __init__(self, session, *, dialog_type=MealHistoryCancelDialog,
                 message_box=QMessageBox, actor_provider=current_windows_actor):
        self.session = session
        self.dialog_type = dialog_type
        self.messages = message_box
        self.actor_provider = actor_provider

    @dialog_workflow
    def run(self, target, expected_revision_id, parent=None):
        try:
            def prepare():
                current = self.session.database.current_roster()
                if current is None or current.revision_id != expected_revision_id:
                    raise RevisionConflictError("다른 작업에서 운영 데이터가 변경되었습니다. 이력을 다시 연 뒤 취소해주세요.")
                snapshot = self.session.database.snapshot(current.revision_id)
                revisions = load_revision_chain(self.session.database, current.revision_id)
                matched = next(((pid, entry) for pid, entries in build_meal_history_entries_by_person(revisions).items()
                                for entry in entries if entry.target == target), None)
                if matched is None:
                    raise ValueError("선택한 이력이 현재 목록에 없습니다. 이력을 다시 열어주세요.")
                person_id, entry = matched
                roster, record = build_meal_history_cancellation(
                    snapshot, revisions, target, actor=self.actor_provider(),
                    recorded_at=datetime.now(UTC), revision_id=uuid4(),
                )
                return current.person(person_id).profile.name, entry.row, roster, record
            name, row, roster, record = run_progress_task(
                prepare, title="식종 변경 취소를 준비하는 중입니다",
                message="선택한 기록과 이후 가입·상태 이력을 확인합니다.", parent=parent,
            )
            dialog = self.dialog_type(name, row, parent)
            if (yield DialogRequest(dialog, self.session)) != QDialog.DialogCode.Accepted:
                return None
            backup = save_revision(self.session, record, parent=parent)
        except Exception as error:
            self.messages.critical(parent, "식종 변경을 취소하지 못했습니다",
                                   "운영 데이터는 변경하지 않았습니다.\n\n" + str(error))
            return None
        if backup.error is not None:
            self.messages.warning(parent, "취소는 저장됐지만 자동 백업에 실패했습니다",
                                  "취소 내용과 변경 이력은 저장되었습니다.\n\n" + str(backup.error))
        return roster
