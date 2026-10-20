"""Settings dialog and controller for delivery-sequence editing mode."""

from __future__ import annotations

from .dialog_workflow import DialogRequest, dialog_workflow

from .startup_loading import save_revision

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QMessageBox,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from src.domain.revisions import RevisionActor
from src.domain.roster import RosterSnapshot
from src.domain.sequence_settings import (
    SEQUENCE_MODE_SETTING_ID,
    SEQUENCE_MODE_SETTING_KIND,
    SequenceMode,
    build_sequence_mode_revision,
    sequence_mode_from_state,
)
from src.storage.database_session import DatabaseSession

from .import_controller import current_windows_actor
from .theme import application_stylesheet, register_application_fonts


class SequenceModeDialog(QDialog):
    def __init__(self, current: SequenceMode, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        register_application_fonts()
        self.setStyleSheet(application_stylesheet())
        self.setWindowTitle("배송순서 입력 방식")
        self.setMinimumWidth(500)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(12)
        title = QLabel("배송순서 입력 방식")
        title.setObjectName("workspaceTitle")
        layout.addWidget(title)
        intro = QLabel(
            "대상자의 배송순서를 식종별로 따로 입력할지, 한 번 입력해 세 식종에 같이 적용할지 정합니다. "
            "방식을 바꾸어도 이미 저장된 순서는 바뀌지 않습니다."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.separate = QRadioButton("식종별로 따로 입력")
        self.separate.setToolTip("석식·조식·중식의 순서를 각각 입력합니다")
        self.unified = QRadioButton("한 번 입력해 모든 식종에 적용")
        self.unified.setToolTip("명시적으로 저장한 순서를 석식·조식·중식 모두에 적용합니다")
        self._group = QButtonGroup(self)
        self._group.addButton(self.separate)
        self._group.addButton(self.unified)
        (self.separate if current is SequenceMode.SEPARATE else self.unified).setChecked(True)
        layout.addWidget(self.separate)
        layout.addWidget(self.unified)
        note = QLabel(
            "통합 입력을 열었을 때 현재 식종별 순서가 서로 다르면 각 값을 보여주고 입력칸은 비워 둡니다. "
            "새 순서를 입력해 저장한 경우에만 세 식종을 같은 값으로 바꿉니다."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("저장")
        buttons.button(QDialogButtonBox.StandardButton.Save).setObjectName("primaryAction")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("취소")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setObjectName("quietAction")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def selected_mode(self) -> SequenceMode:
        return SequenceMode.UNIFIED if self.unified.isChecked() else SequenceMode.SEPARATE


class SequenceModeStatus(StrEnum):
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class SequenceModeOutcome:
    status: SequenceModeStatus
    roster: RosterSnapshot | None = None

    @property
    def completed(self) -> bool:
        return self.status is SequenceModeStatus.COMPLETED


class SequenceModeController:
    def __init__(
        self,
        session: DatabaseSession,
        *,
        actor_provider: Callable[[], RevisionActor] | None = None,
        dialog_type=SequenceModeDialog,
        message_box=QMessageBox,
        recorded_at_provider: Callable[[], datetime] | None = None,
        revision_id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        self._session = session
        self._actor_provider = actor_provider or current_windows_actor
        self._dialog_type = dialog_type
        self._message_box = message_box
        self._recorded_at_provider = recorded_at_provider or (lambda: datetime.now(UTC))
        self._revision_id_factory = revision_id_factory

    @dialog_workflow
    def run(self, parent: QWidget | None = None) -> SequenceModeOutcome:
        roster = self._session.database.current_roster()
        if roster is None:
            self._message_box.critical(parent, "배송순서 입력 방식을 저장하지 못했습니다", "현재 명단이 없습니다.")
            return SequenceModeOutcome(SequenceModeStatus.FAILED)
        snapshot = self._session.database.snapshot(roster.revision_id)
        stored_state = snapshot.entity(SEQUENCE_MODE_SETTING_KIND, SEQUENCE_MODE_SETTING_ID)
        current = sequence_mode_from_state(stored_state)
        dialog = self._dialog_type(current, parent)
        if (yield DialogRequest(dialog, self._session)) != QDialog.DialogCode.Accepted:
            return SequenceModeOutcome(SequenceModeStatus.CANCELLED)
        selected = dialog.selected_mode()
        if selected is current:
            return SequenceModeOutcome(SequenceModeStatus.CANCELLED)
        try:
            prepared = build_sequence_mode_revision(
                roster.revision_id,
                stored_state,
                selected,
                actor=self._actor_provider(),
                recorded_at=self._recorded_at_provider(),
                revision_id=self._revision_id_factory(),
            )
            backup = save_revision(self._session, prepared.record, parent=parent)
        except Exception as error:  # noqa: BLE001 - save failure is reported at the UI boundary
            self._message_box.critical(parent, "배송순서 입력 방식을 저장하지 못했습니다", str(error))
            return SequenceModeOutcome(SequenceModeStatus.FAILED)
        if backup.error is not None:
            self._message_box.warning(
                parent,
                "배송순서 입력 방식은 저장됐지만 자동 백업에 실패했습니다",
                str(backup.error),
            )
        return SequenceModeOutcome(
            SequenceModeStatus.COMPLETED,
            self._session.database.current_roster() or roster,
        )
