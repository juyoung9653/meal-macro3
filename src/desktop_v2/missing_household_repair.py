"""Preview and commit the narrowly evidenced missing-household repair."""

from __future__ import annotations

from .dialog_workflow import DialogRequest, dialog_workflow

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from uuid import uuid4

from PySide6.QtWidgets import QDialog, QFrame, QHBoxLayout, QLabel, QMessageBox, QPushButton, QScrollArea, QVBoxLayout, QWidget

from src.domain.roster import RosterSnapshot
from src.services.missing_household_repair import load_missing_household_context, plan_missing_household_repair
from src.storage.database_session import committed_roster_after_save
from src.storage.encrypted_database import RevisionConflictError
from .import_controller import current_windows_actor
from .startup_loading import run_progress_task, save_revision
from .theme import application_stylesheet, register_application_fonts


def _candidate_label(item):
    profile = item.person.profile
    identity = [profile.name]
    if profile.birth_date:
        identity.append(profile.birth_date.isoformat())
    if profile.school_name:
        identity.append(profile.school_name)
    identity.append(f"적용 시작일 {item.starts_on.isoformat() if item.starts_on else '제한 없음'}")
    return " · ".join(identity)


class MissingHouseholdRepairDialog(QDialog):
    def __init__(self, context, parent=None):
        super().__init__(parent)
        register_application_fonts()
        self.setStyleSheet(application_stylesheet())
        self.setWindowTitle("가구 소속 기록 복구")
        self.resize(640, 490)
        self.setMinimumSize(540, 410)
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 20)
        title = QLabel("가구 소속 기록 복구")
        title.setObjectName("workspaceTitle")
        root.addWidget(title)
        note = QLabel("과거 명단 행 추가 또는 방학 중식 가져오기에서 소속 기록이 빠진 대상자에게 각각 별도 가구를 만듭니다. 식종 가입·상태·인적사항은 변경하지 않습니다.")
        note.setWordWrap(True)
        root.addWidget(note)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget()
        content = QVBoxLayout(body)
        self.preview_label = QLabel()
        self.preview_label.setWordWrap(True)
        self.preview_label.setText(
            f"복구 대상 {len(context.candidates)}명\n"
            + "\n".join(_candidate_label(item) for item in context.candidates)
            if context.candidates else "복구할 대상이 없습니다."
        )
        content.addWidget(self.preview_label)
        skipped = QLabel("제외 대상\n" + "\n".join(context.skipped) if context.skipped else "")
        skipped.setWordWrap(True)
        content.addWidget(skipped)
        history = QLabel("저장하면 새 변경 이력과 자동 백업이 생성됩니다. 이전 변경번호의 내용은 유지됩니다.")
        history.setWordWrap(True)
        content.addWidget(history)
        content.addStretch(1)
        scroll.setWidget(body)
        root.addWidget(scroll, 1)
        actions = QHBoxLayout()
        actions.addStretch(1)
        cancel = QPushButton("취소")
        cancel.setObjectName("quietAction")
        cancel.clicked.connect(self.reject)
        actions.addWidget(cancel)
        self.save_button = QPushButton("복구 저장")
        self.save_button.setObjectName("primaryAction")
        self.save_button.setEnabled(bool(context.candidates))
        self.save_button.clicked.connect(self.accept)
        actions.addWidget(self.save_button)
        root.addLayout(actions)


@dataclass(frozen=True, slots=True)
class MissingHouseholdRepairOutcome:
    completed: bool = False
    roster: RosterSnapshot | None = None


class MissingHouseholdRepairController:
    def __init__(self, session, *, dialog_type=MissingHouseholdRepairDialog, message_box=QMessageBox,
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
            context = run_progress_task(lambda: load_missing_household_context(self._session.database),
                                        message="누락된 가구 소속 기록을 확인하는 중입니다.", parent=parent)
        except Exception as error:
            self._message_box.critical(parent, "가구 소속 기록을 확인하지 못했습니다", str(error))
            return MissingHouseholdRepairOutcome()
        dialog = self._dialog_type(context, parent)
        if (yield DialogRequest(dialog, self._session)) != QDialog.DialogCode.Accepted or not context.candidates:
            return MissingHouseholdRepairOutcome()
        try:
            plan = plan_missing_household_repair(context)
            record = run_progress_task(
                lambda: plan.revision(actor=self._actor_provider(), recorded_at=self._recorded_at_provider(),
                                      revision_id=self._revision_id_factory()),
                message="가구 소속 복구 내용을 확인하는 중입니다.", parent=parent,
            )
            if record is None:
                return MissingHouseholdRepairOutcome()
            backup = save_revision(self._session, record, parent=parent)
        except Exception as error:
            text = "확인하는 동안 명단이 변경됐습니다. 설정을 다시 열어 확인해주세요." if isinstance(error, RevisionConflictError) else str(error)
            self._message_box.critical(parent, "가구 소속 기록을 저장하지 못했습니다", text)
            return MissingHouseholdRepairOutcome()
        if backup.error is not None:
            self._message_box.warning(parent, "복구는 저장됐지만 자동 백업에 실패했습니다", str(backup.error))
        roster = committed_roster_after_save(self._session, replace(plan.roster, revision_id=record.metadata.revision_id))
        return MissingHouseholdRepairOutcome(True, roster)
