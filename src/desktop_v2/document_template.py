"""Choose and persist the invoice/confirmation template for one operating DB."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from src.domain.document_templates import (
    DOCUMENT_TEMPLATE_SETTING_ID,
    DOCUMENT_TEMPLATE_SETTING_KIND,
    DocumentTemplateProfile,
    DocumentTemplateSetting,
    build_document_template_revision,
    document_template_setting_from_state,
)
from src.domain.revisions import RevisionActor
from src.domain.roster import RosterSnapshot
from src.storage.database_session import (
    AutomaticBackupResult,
    DatabaseSession,
)

from .import_controller import current_windows_actor
from .theme import application_stylesheet, register_application_fonts


_PROFILE_COPY = {
    DocumentTemplateProfile.SCHOOL_MEAL: (
        "급식 양식",
        "학교명·학년·관할동을 포함한 급식 청구서와 수정구 확인서 형식입니다.",
        "급식",
    ),
    DocumentTemplateProfile.HAPPY_LUNCHBOX: (
        "행복도시락 양식",
        "학기중·방학 시트를 구분하는 행복도시락 청구서와 확인서 형식입니다.",
        "도시락",
    ),
}


class DocumentTemplateDialog(QDialog):
    """One explicit, nontechnical choice saved per vendor database."""

    def __init__(
        self,
        current: DocumentTemplateSetting | None,
        parent: QWidget | None = None,
    ) -> None:
        if current is not None and not isinstance(current, DocumentTemplateSetting):
            raise TypeError("current는 DocumentTemplateSetting 또는 None이어야 합니다.")
        super().__init__(parent)
        self._current = current
        self._buttons: dict[DocumentTemplateProfile, QRadioButton] = {}
        self._cards: dict[DocumentTemplateProfile, QFrame] = {}
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)

        register_application_fonts()
        self.setObjectName("documentTemplateDialog")
        self.setWindowTitle("설정 · 청구서·확인서 양식")
        self.setModal(True)
        self.resize(790, 520)
        self.setMinimumSize(700, 480)
        self.setStyleSheet(application_stylesheet())
        self._build_ui()
        self._refresh()

    @property
    def selected_profile(self) -> DocumentTemplateProfile:
        for profile, button in self._buttons.items():
            if button.isChecked():
                return profile
        raise ValueError("이 운영 데이터에서 사용할 문서 양식을 선택해주세요.")

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 22, 22, 19)
        root.setSpacing(13)

        header = QFrame()
        header.setObjectName("documentTemplateHeader")
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(18, 15, 18, 16)
        header_layout.setSpacing(4)
        eyebrow = QLabel("문서 양식 · 공통 설정")
        eyebrow.setObjectName("editEyebrow")
        title = QLabel("청구서·확인서 양식 선택")
        title.setObjectName("workspaceTitle")
        subtitle = QLabel(
            "이 운영 데이터는 업체 한 곳의 양식을 기억합니다. 이후 문서를 만들 때 자동으로 같은 양식을 사용합니다."
        )
        subtitle.setObjectName("workspaceSubtitle")
        subtitle.setWordWrap(True)
        header_layout.addWidget(eyebrow)
        header_layout.addWidget(title)
        header_layout.addWidget(subtitle)
        root.addWidget(header)

        cards = QHBoxLayout()
        cards.setSpacing(11)
        for profile in DocumentTemplateProfile:
            card = QFrame()
            card.setObjectName("documentTemplateCard")
            card.setProperty("selected", "false")
            self._cards[profile] = card
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(15, 14, 15, 15)
            card_layout.setSpacing(8)
            label, description, stamp = _PROFILE_COPY[profile]
            top = QHBoxLayout()
            marker = QLabel(stamp)
            marker.setObjectName("documentTemplateStamp")
            marker.setAlignment(Qt.AlignmentFlag.AlignCenter)
            marker.setFixedSize(66, 54)
            button = QRadioButton(label)
            button.setObjectName("documentTemplateChoice")
            button.toggled.connect(self._refresh)
            self._group.addButton(button)
            self._buttons[profile] = button
            top.addWidget(marker)
            top.addWidget(button, 1)
            card_layout.addLayout(top)
            copy = QLabel(description)
            copy.setObjectName("documentTemplateDescription")
            copy.setWordWrap(True)
            card_layout.addWidget(copy)
            if self._current is not None and self._current.profile is profile:
                current_badge = QLabel(f"현재 사용 중 · 버전 {self._current.version}")
                current_badge.setObjectName("documentTemplateCurrent")
                card_layout.addWidget(current_badge)
                button.setChecked(True)
            card_layout.addStretch(1)
            cards.addWidget(card, 1)
        root.addLayout(cards, 1)

        note = QLabel(
            "양식을 바꾸면 새 변경번호와 자동 백업이 만들어집니다. 예전에 만든 Excel 파일은 바뀌지 않습니다."
        )
        note.setObjectName("settingsHubNote")
        note.setWordWrap(True)
        root.addWidget(note)

        self._error = QLabel("문서 양식을 하나 선택해주세요.")
        self._error.setObjectName("errorMessage")
        self._error.hide()
        root.addWidget(self._error)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        self._save_button: QPushButton = buttons.button(
            QDialogButtonBox.StandardButton.Save
        )
        self._save_button.setText("이 양식 사용")
        self._save_button.setObjectName("primaryAction")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("닫기")
        buttons.accepted.connect(self._accept_if_valid)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _refresh(self, _checked=None) -> None:
        selected = any(button.isChecked() for button in self._buttons.values())
        for profile, card in self._cards.items():
            card.setProperty(
                "selected",
                "true" if self._buttons[profile].isChecked() else "false",
            )
            card.style().unpolish(card)
            card.style().polish(card)
        if hasattr(self, "_save_button"):
            self._save_button.setEnabled(selected)
        if selected and hasattr(self, "_error"):
            self._error.hide()

    def _accept_if_valid(self) -> None:
        try:
            self.selected_profile
        except ValueError as error:
            self._error.setText(str(error))
            self._error.show()
            return
        self.accept()


class DocumentTemplateChangeStatus(StrEnum):
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class DocumentTemplateChangeOutcome:
    status: DocumentTemplateChangeStatus
    roster: RosterSnapshot | None = None
    setting: DocumentTemplateSetting | None = None
    backup_result: AutomaticBackupResult | None = None
    changed: bool = False

    @property
    def completed(self) -> bool:
        return self.status is DocumentTemplateChangeStatus.COMPLETED


class DocumentTemplateController:
    def __init__(
        self,
        session: DatabaseSession,
        *,
        actor_provider: Callable[[], RevisionActor] | None = None,
        dialog_type=DocumentTemplateDialog,
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
    ) -> DocumentTemplateChangeOutcome:
        roster, state, current = self._current(parent)
        if roster is None:
            return DocumentTemplateChangeOutcome(DocumentTemplateChangeStatus.FAILED)
        if current is not None:
            return DocumentTemplateChangeOutcome(
                DocumentTemplateChangeStatus.COMPLETED,
                roster,
                current,
                changed=False,
            )
        return self._choose_and_save(roster, state, current, parent)

    def run(
        self,
        parent: QWidget | None = None,
    ) -> DocumentTemplateChangeOutcome:
        roster, state, current = self._current(parent)
        if roster is None:
            return DocumentTemplateChangeOutcome(DocumentTemplateChangeStatus.FAILED)
        return self._choose_and_save(roster, state, current, parent)

    def _current(self, parent):
        roster = self._session.database.current_roster()
        if roster is None:
            self._message_box.critical(
                parent,
                "문서 양식을 설정하지 못했습니다",
                "현재 명단이 없습니다. 기존 명단 가져오기를 먼저 완료해주세요.",
            )
            return None, None, None
        snapshot = self._session.database.snapshot(roster.revision_id)
        state = snapshot.entity(
            DOCUMENT_TEMPLATE_SETTING_KIND,
            DOCUMENT_TEMPLATE_SETTING_ID,
        )
        try:
            current = document_template_setting_from_state(state)
        except Exception as error:
            self._message_box.critical(
                parent,
                "문서 양식 설정을 읽지 못했습니다",
                str(error),
            )
            return None, None, None
        return roster, state, current

    def _choose_and_save(self, roster, state, current, parent):
        dialog = self._dialog_type(current, parent)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return DocumentTemplateChangeOutcome(
                DocumentTemplateChangeStatus.CANCELLED,
                roster,
                current,
            )
        profile = dialog.selected_profile
        if current is not None and current.profile is profile:
            return DocumentTemplateChangeOutcome(
                DocumentTemplateChangeStatus.COMPLETED,
                roster,
                current,
                changed=False,
            )
        try:
            prepared = build_document_template_revision(
                roster.revision_id,
                state,
                profile,
                actor=self._actor_provider(),
                recorded_at=self._recorded_at_provider(),
                revision_id=self._revision_id_factory(),
                reason=(
                    "문서 양식 첫 설정"
                    if current is None
                    else "문서 양식 변경"
                ),
            )
            backup_result = self._session.append_revision(prepared.record)
            updated_roster = self._session.database.current_roster()
            if updated_roster is None:
                raise RuntimeError("저장 뒤 현재 명단을 다시 읽지 못했습니다.")
        except Exception as error:
            self._message_box.critical(
                parent,
                "문서 양식을 저장하지 못했습니다",
                "양식과 변경 이력은 저장하지 않았습니다. 다시 시도해주세요.\n\n"
                + str(error),
            )
            return DocumentTemplateChangeOutcome(DocumentTemplateChangeStatus.FAILED)
        if backup_result.error is not None:
            self._message_box.warning(
                parent,
                "문서 양식은 저장됐지만 자동 백업에 실패했습니다",
                "양식 선택은 운영 데이터에 저장되었습니다. 백업 폴더를 확인해주세요.\n\n"
                + str(backup_result.error),
            )
        return DocumentTemplateChangeOutcome(
            DocumentTemplateChangeStatus.COMPLETED,
            updated_roster,
            prepared.setting,
            backup_result,
            True,
        )


def _utc_now() -> datetime:
    return datetime.now(UTC)
