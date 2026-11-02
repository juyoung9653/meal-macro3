"""Review and save an earlier effective date for the first imported roster."""
from __future__ import annotations

from .user_messages import user_error

from .dialog_workflow import DialogRequest, dialog_workflow

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from uuid import uuid4

from PySide6.QtCore import QDate
from PySide6.QtWidgets import QDialog, QFrame, QHBoxLayout, QLabel, QMessageBox, QPushButton, QScrollArea, QVBoxLayout, QWidget

from src.domain.roster import RosterSnapshot
from src.services.initial_roster_date import load_initial_roster_date_context, plan_initial_roster_date
from src.storage.database_session import committed_roster_after_save
from src.storage.encrypted_database import RevisionConflictError
from .date_edit import WheelSafeDateEdit
from .import_controller import current_windows_actor
from .startup_loading import run_progress_task, save_revision
from .theme import application_stylesheet, register_application_fonts


class InitialRosterDateDialog(QDialog):
    def __init__(self, context, parent=None):
        super().__init__(parent)
        self._context = context
        register_application_fonts()
        self.setStyleSheet(application_stylesheet())
        self.setWindowTitle("초기 명단 적용일")
        self.resize(640, 500)
        self.setMinimumSize(540, 410)
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 20)
        title = QLabel("초기 명단 적용일")
        title.setObjectName("workspaceTitle")
        root.addWidget(title)
        note = QLabel("처음 가져온 명단이 실제로 적용되는 시작일을 앞당깁니다. 이후 추가한 사람과 일시중지·중지·이사 기록은 그대로 유지합니다.")
        note.setWordWrap(True)
        root.addWidget(note)
        source = QLabel(f"처음 가져온 명단: {context.initial_date:%Y-%m-%d}부터 · {len(context.initial_roster.persons)}명")
        root.addWidget(source)
        row = QHBoxLayout()
        row.addWidget(QLabel("앞당길 적용일"))
        initial = context.initial_date.replace(day=1)
        self.date_edit = WheelSafeDateEdit(QDate(initial.year, initial.month, initial.day))
        self.date_edit.setDisplayFormat("yyyy-MM-dd")
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setMaximumDate(QDate(context.initial_date.year, context.initial_date.month, context.initial_date.day))
        row.addWidget(self.date_edit)
        row.addStretch(1)
        root.addLayout(row)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget()
        content = QVBoxLayout(body)
        self.preview_label = QLabel()
        self.preview_label.setWordWrap(True)
        content.addWidget(self.preview_label)
        self.skipped_label = QLabel()
        self.skipped_label.setWordWrap(True)
        content.addWidget(self.skipped_label)
        footer = QLabel("변경한 날짜부터 기존 제공 요일·학교 일정·휴무 규칙에 따라 집계합니다. 이미 저장된 과거 변경번호와 생성 문서는 바뀌지 않습니다.")
        footer.setWordWrap(True)
        content.addWidget(footer)
        content.addStretch(1)
        scroll.setWidget(body)
        root.addWidget(scroll, 1)
        actions = QHBoxLayout()
        actions.addStretch(1)
        cancel = QPushButton("취소")
        cancel.setObjectName("quietAction")
        cancel.clicked.connect(self.reject)
        actions.addWidget(cancel)
        self.save_button = QPushButton("적용일 저장")
        self.save_button.setObjectName("primaryAction")
        self.save_button.clicked.connect(self.accept)
        actions.addWidget(self.save_button)
        root.addLayout(actions)
        self.date_edit.dateChanged.connect(self._refresh)
        self._refresh()

    def effective_on(self):
        return self.date_edit.date().toPython()

    def _refresh(self, *_args):
        plan = plan_initial_roster_date(self._context, self.effective_on())
        self.preview_label.setText(
            f"{plan.affected_people}명의 처음 가입 기간을 {plan.effective_on:%Y-%m-%d}부터로 앞당깁니다."
            if plan.affected_people else "이 날짜로 앞당길 대상이 없습니다. 이미 적용된 기간은 줄이지 않습니다."
        )
        self.skipped_label.setText("유지하거나 제외하는 항목\n" + "\n".join(plan.skipped) if plan.skipped else "")
        self.save_button.setEnabled(plan.affected_people > 0)


@dataclass(frozen=True, slots=True)
class InitialRosterDateOutcome:
    completed: bool = False
    roster: RosterSnapshot | None = None


class InitialRosterDateController:
    def __init__(self, session, *, dialog_type=InitialRosterDateDialog, message_box=QMessageBox,
                 actor_provider=current_windows_actor, recorded_at_provider=None, revision_id_factory=uuid4):
        self._session = session
        self._dialog_type = dialog_type
        self._message_box = message_box
        self._actor_provider = actor_provider
        self._recorded_at_provider = recorded_at_provider or (lambda: datetime.now(UTC))
        self._revision_id_factory = revision_id_factory

    @dialog_workflow
    def run(self, parent=None):
        try:
            context = run_progress_task(lambda: load_initial_roster_date_context(self._session.database),
                                        message="처음 가져온 명단을 확인하는 중입니다.", parent=parent)
        except Exception as error:
            self._message_box.critical(parent, "초기 명단을 확인하지 못했습니다", user_error(error))
            return InitialRosterDateOutcome()
        dialog = self._dialog_type(context, parent)
        if (yield DialogRequest(dialog, self._session)) != QDialog.DialogCode.Accepted:
            return InitialRosterDateOutcome()
        try:
            plan = plan_initial_roster_date(context, dialog.effective_on())
            actor, recorded_at, revision_id = self._actor_provider(), self._recorded_at_provider(), self._revision_id_factory()
            record = run_progress_task(
                lambda: plan.revision(actor=actor, recorded_at=recorded_at, revision_id=revision_id),
                message="적용일 변경 내용을 확인하는 중입니다.", parent=parent,
            )
            if record is None:
                return InitialRosterDateOutcome()
            backup = save_revision(self._session, record, parent=parent)
        except Exception as error:
            text = "확인하는 동안 명단이 변경됐습니다. 설정을 다시 열어 확인해주세요." if isinstance(error, RevisionConflictError) else user_error(error)
            self._message_box.critical(parent, "초기 명단 적용일을 저장하지 못했습니다", text)
            return InitialRosterDateOutcome()
        if backup.warning_required:
            self._message_box.warning(parent, "적용일은 저장됐지만 자동 백업에 실패했습니다", user_error(backup.error))
        roster = committed_roster_after_save(self._session, replace(plan.roster, revision_id=record.metadata.revision_id))
        return InitialRosterDateOutcome(True, roster)
