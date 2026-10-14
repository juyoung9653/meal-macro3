"""Shared generated-Excel password dialog and revision controller."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from src.domain.export_security import (
    EXPORT_PASSWORD_SETTING_ID,
    EXPORT_PASSWORD_SETTING_KIND,
    ExportPasswordSetting,
    build_export_password_revision,
    export_password_setting_from_state,
)
from src.domain.revisions import RevisionActor
from src.domain.roster import RosterSnapshot
from src.storage.database_session import (
    AutomaticBackupResult,
    DatabaseSession,
)

from .database_access import _DatabaseAccessDialog
from .import_controller import current_windows_actor


CURRENT_PASSWORD_ACCESS_MILLISECONDS = 30_000
_CLIPBOARD_COPY_SEQUENCE = 0


class ExportPasswordDialog(_DatabaseAccessDialog):
    """Inspect the current shared password briefly or replace it."""

    def __init__(
        self,
        current: ExportPasswordSetting | None,
        parent: QWidget | None = None,
    ) -> None:
        if current is not None and not isinstance(current, ExportPasswordSetting):
            raise TypeError("current는 ExportPasswordSetting 또는 None이어야 합니다.")
        self._current = current
        super().__init__(
            window_title="설정 · 생성 Excel 비밀번호",
            title="생성 Excel 비밀번호",
            subtitle=(
                "센터에서 함께 사용할 비밀번호를 한 번 정하면 청구서·확인서·명단 "
                "Excel을 만들 때 자동으로 적용합니다."
            ),
            rail_title="내보내는 파일을 한 번 더 잠급니다",
            rail_copy=(
                "운영 데이터와 별개로, 외부에 전달하는 Excel 파일을 현재 센터의 공통 "
                "비밀번호로 보호합니다."
            ),
            route_stops=(
                "공통 비밀번호 한 번 입력",
                "생성 파일에 자동 적용",
                "변경 뒤 새 파일부터 사용",
            ),
            parent=parent,
        )
        self.resize(790, 680 if current is not None else 560)

        self._reveal_timer = QTimer(self)
        self._reveal_timer.setSingleShot(True)
        self._reveal_timer.setInterval(CURRENT_PASSWORD_ACCESS_MILLISECONDS)
        self._reveal_timer.timeout.connect(self._mask_current_password)
        self._copy_feedback_timer = QTimer(self)
        self._copy_feedback_timer.setSingleShot(True)
        self._copy_feedback_timer.setInterval(CURRENT_PASSWORD_ACCESS_MILLISECONDS)
        self._copy_feedback_timer.timeout.connect(self._reset_copy_feedback)
        self._clipboard_copy_sequence: int | None = None
        self.current_password_edit: QLineEdit | None = None
        self.reveal_current_password_button: QPushButton | None = None
        self.copy_current_password_button: QPushButton | None = None

        self._status = QLabel(
            (
                f"파일 잠금 설정됨 · 버전 {current.version}"
                if current is not None
                else "파일 잠금이 아직 설정되지 않았습니다"
            )
        )
        self._status.setObjectName("exportPasswordStatus")
        self._status.setProperty("configured", current is not None)
        self._content.addWidget(self._status)

        if current is not None:
            self._add_current_password_controls(current.password)

        self.password_edit = QLineEdit()
        self.password_edit.setObjectName("exportPasswordEdit")
        self.confirmation_edit = QLineEdit()
        self.confirmation_edit.setObjectName("exportPasswordConfirmationEdit")
        self._add_password_field(
            "새 공통 비밀번호",
            self.password_edit,
            placeholder="센터에서 공유할 Excel 비밀번호",
        )
        self._add_password_field(
            "새 공통 비밀번호 다시 입력",
            self.confirmation_edit,
        )
        self._add_show_passwords((self.password_edit, self.confirmation_edit))

        notice = QLabel(
            "기존에 만든 Excel은 만들 당시 비밀번호를 계속 사용합니다. "
            "여기서 바꾼 비밀번호는 이후 생성하는 파일부터 적용됩니다."
        )
        notice.setObjectName("recoveryNotice")
        notice.setWordWrap(True)
        self._content.addWidget(notice)
        self.save_button = self._add_actions(
            "Excel 비밀번호 저장",
            self._submit,
            secondary_text="닫기",
        )

    def _add_current_password_controls(self, password: str) -> None:
        panel = QFrame()
        panel.setObjectName("currentExportPasswordPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 11, 12, 11)
        layout.setSpacing(7)

        label = QLabel("현재 공통 비밀번호")
        label.setObjectName("fieldLabel")
        layout.addWidget(label)

        row = QHBoxLayout()
        row.setSpacing(7)
        self.current_password_edit = QLineEdit(password)
        self.current_password_edit.setObjectName("currentExportPasswordEdit")
        self.current_password_edit.setReadOnly(True)
        self.current_password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.current_password_edit.setContextMenuPolicy(
            Qt.ContextMenuPolicy.NoContextMenu
        )
        self.current_password_edit.setAccessibleName("현재 공통 비밀번호")
        row.addWidget(self.current_password_edit, 1)

        self.reveal_current_password_button = QPushButton("현재 비밀번호 보기")
        self.reveal_current_password_button.setObjectName("quietAction")
        self.reveal_current_password_button.clicked.connect(
            self._toggle_current_password_visibility
        )
        row.addWidget(self.reveal_current_password_button)

        self.copy_current_password_button = QPushButton("복사")
        self.copy_current_password_button.setObjectName("quietAction")
        self.copy_current_password_button.clicked.connect(
            self._copy_current_password
        )
        row.addWidget(self.copy_current_password_button)
        layout.addLayout(row)

        hint = QLabel(
            "보이면 30초 뒤 다시 가립니다. 복사한 값도 30초 뒤 "
            "클립보드에서 지웁니다."
        )
        hint.setObjectName("currentExportPasswordHint")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self._content.addWidget(panel)

    def _toggle_current_password_visibility(self) -> None:
        if self.current_password_edit is None:
            return
        if (
            self.current_password_edit.echoMode()
            is QLineEdit.EchoMode.Normal
        ):
            self._mask_current_password()
            return
        self.current_password_edit.setEchoMode(QLineEdit.EchoMode.Normal)
        if self.reveal_current_password_button is not None:
            self.reveal_current_password_button.setText("다시 가리기")
        self._reveal_timer.start()

    def _mask_current_password(self) -> None:
        if self.current_password_edit is None:
            return
        self.current_password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.current_password_edit.deselect()
        if self.reveal_current_password_button is not None:
            self.reveal_current_password_button.setText("현재 비밀번호 보기")
        self._reveal_timer.stop()

    def _copy_current_password(self) -> None:
        if self._current is None:
            return
        self._clipboard_copy_sequence = _copy_password_temporarily(
            self._current.password
        )
        if self.copy_current_password_button is not None:
            self.copy_current_password_button.setText("복사됨")
        self._copy_feedback_timer.start()

    def _reset_copy_feedback(self) -> None:
        if self.copy_current_password_button is not None:
            self.copy_current_password_button.setText("복사")
        self._copy_feedback_timer.stop()

    def selected_password(self) -> str:
        return self.password_edit.text()

    def _submit(self) -> None:
        self._clear_error()
        password = self.selected_password()
        if password != self.confirmation_edit.text():
            self._show_error("새 공통 비밀번호 두 번의 입력이 서로 다릅니다.")
            return
        if not password or not password.strip():
            self._show_error("새 공통 비밀번호를 입력해주세요.")
            return
        if password != password.strip():
            self._show_error("비밀번호 앞뒤에는 공백을 사용할 수 없습니다.")
            return
        if self._current is not None and password == self._current.password:
            self._show_error("현재와 같은 비밀번호입니다. 다른 비밀번호를 입력해주세요.")
            return
        self.accept()


class ExportPasswordChangeStatus(StrEnum):
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ExportPasswordChangeOutcome:
    status: ExportPasswordChangeStatus
    roster: RosterSnapshot | None = None
    setting: ExportPasswordSetting | None = None
    backup_result: AutomaticBackupResult | None = None

    @property
    def completed(self) -> bool:
        return self.status is ExportPasswordChangeStatus.COMPLETED


class ExportPasswordController:
    """Save one password-setting revision and run the normal automatic backup."""

    def __init__(
        self,
        session: DatabaseSession,
        *,
        actor_provider: Callable[[], RevisionActor] | None = None,
        dialog_type=ExportPasswordDialog,
        message_box=QMessageBox,
        recorded_at_provider: Callable[[], datetime] | None = None,
        revision_id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        if not isinstance(session, DatabaseSession):
            raise TypeError("session은 DatabaseSession이어야 합니다.")
        self._session = session
        self._actor_provider = actor_provider or current_windows_actor
        self._dialog_type = dialog_type
        self._message_box = message_box
        self._recorded_at_provider = recorded_at_provider or _utc_now
        self._revision_id_factory = revision_id_factory

    def ensure_configured(
        self,
        parent: QWidget | None = None,
    ) -> ExportPasswordChangeOutcome:
        """Prompt only when this database has no generated-Excel password yet."""

        roster, stored_state, current = self._current(parent)
        if roster is None:
            return ExportPasswordChangeOutcome(ExportPasswordChangeStatus.FAILED)
        if current is not None:
            return ExportPasswordChangeOutcome(
                ExportPasswordChangeStatus.COMPLETED,
                roster,
                current,
            )
        return self._choose_and_save(
            roster,
            stored_state,
            current,
            parent,
        )

    def run(self, parent: QWidget | None = None) -> ExportPasswordChangeOutcome:
        roster, stored_state, current = self._current(parent)
        if roster is None:
            return ExportPasswordChangeOutcome(ExportPasswordChangeStatus.FAILED)
        return self._choose_and_save(
            roster,
            stored_state,
            current,
            parent,
        )

    def _current(self, parent):
        roster = self._session.database.current_roster()
        if roster is None:
            self._message_box.critical(
                parent,
                "Excel 비밀번호를 저장하지 못했습니다",
                "현재 명단이 없습니다. 기존 명단 가져오기를 먼저 완료해주세요.",
            )
            return None, None, None
        source = self._session.database.snapshot(roster.revision_id)
        stored_state = source.entity(
            EXPORT_PASSWORD_SETTING_KIND,
            EXPORT_PASSWORD_SETTING_ID,
        )
        try:
            current = export_password_setting_from_state(stored_state)
        except Exception as error:
            self._message_box.critical(
                parent,
                "Excel 비밀번호 설정을 읽지 못했습니다",
                str(error),
            )
            return None, None, None
        return roster, stored_state, current

    def _choose_and_save(self, roster, stored_state, current, parent):
        dialog = self._dialog_type(current, parent)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return ExportPasswordChangeOutcome(ExportPasswordChangeStatus.CANCELLED)

        try:
            prepared = build_export_password_revision(
                roster.revision_id,
                stored_state,
                dialog.selected_password(),
                actor=self._actor_provider(),
                recorded_at=self._recorded_at_provider(),
                revision_id=self._revision_id_factory(),
                reason=(
                    "생성 Excel 비밀번호 설정"
                    if current is None
                    else "생성 Excel 비밀번호 변경"
                ),
            )
            backup_result = self._session.append_revision(prepared.record)
            updated_roster = self._session.database.current_roster()
        except Exception as error:
            self._message_box.critical(
                parent,
                "Excel 비밀번호를 저장하지 못했습니다",
                "비밀번호와 변경 이력은 저장하지 않았습니다. 내용을 확인한 뒤 다시 시도해주세요.\n\n"
                + str(error),
            )
            return ExportPasswordChangeOutcome(ExportPasswordChangeStatus.FAILED)
        if updated_roster is None:
            self._message_box.critical(
                parent,
                "저장한 Excel 비밀번호를 확인하지 못했습니다",
                "비밀번호는 저장됐지만 현재 상태를 다시 읽지 못했습니다. 프로그램을 다시 열어주세요.",
            )
            return ExportPasswordChangeOutcome(ExportPasswordChangeStatus.FAILED)
        if backup_result.error is not None:
            self._message_box.warning(
                parent,
                "비밀번호는 저장됐지만 자동 백업에 실패했습니다",
                "Excel 비밀번호와 이력은 운영 데이터에 정상 저장되었습니다. 백업 폴더 연결을 확인한 뒤 백업을 다시 실행해주세요.\n\n"
                + str(backup_result.error),
            )
        return ExportPasswordChangeOutcome(
            ExportPasswordChangeStatus.COMPLETED,
            updated_roster,
            prepared.setting,
            backup_result,
        )


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _copy_password_temporarily(password: str) -> int:
    """Copy one password and schedule clearing without touching newer content."""

    global _CLIPBOARD_COPY_SEQUENCE
    if QApplication.instance() is None:
        raise RuntimeError("비밀번호를 복사하려면 QApplication이 실행 중이어야 합니다.")
    _CLIPBOARD_COPY_SEQUENCE += 1
    sequence = _CLIPBOARD_COPY_SEQUENCE
    QApplication.clipboard().setText(password)
    QTimer.singleShot(
        CURRENT_PASSWORD_ACCESS_MILLISECONDS,
        lambda: _clear_copied_password_if_current(sequence, password),
    )
    return sequence


def _clear_copied_password_if_current(sequence: int, password: str) -> bool:
    """Clear only this module's latest, unchanged clipboard password."""

    global _CLIPBOARD_COPY_SEQUENCE
    if sequence != _CLIPBOARD_COPY_SEQUENCE:
        return False
    _CLIPBOARD_COPY_SEQUENCE += 1
    if QApplication.instance() is None:
        return False
    clipboard = QApplication.clipboard()
    if clipboard.text() != password:
        return False
    clipboard.clear()
    return True
