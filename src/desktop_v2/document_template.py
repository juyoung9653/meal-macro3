"""Choose and persist the invoice/confirmation template for one operating DB."""

from __future__ import annotations

from .user_messages import user_error

from .dialog_workflow import DialogRequest, dialog_workflow, invoke, launch

from .startup_loading import run_progress_task, save_revision

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
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
from .document_hub import DocumentAction
from .delivery_template import DeliveryTemplateDialog, DeliveryTemplateStore
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
        *,
        delivery_dialog_type=DeliveryTemplateDialog,
    ) -> None:
        if current is not None and not isinstance(current, DocumentTemplateSetting):
            raise TypeError("current는 DocumentTemplateSetting 또는 None이어야 합니다.")
        super().__init__(parent)
        self._current = current
        self._database_id = None
        self._delivery_dialog_type = delivery_dialog_type
        self._delivery_buttons = {}
        self._delivery_status = {}
        self._buttons: dict[DocumentTemplateProfile, QRadioButton] = {}
        self._cards: dict[DocumentTemplateProfile, QFrame] = {}
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)

        register_application_fonts()
        self.setObjectName("documentTemplateDialog")
        self.setWindowTitle("설정 · 문서 양식")
        self.setModal(True)
        self.resize(850, 650)
        self.setMinimumSize(790, 630)
        self.setStyleSheet(application_stylesheet())
        self._build_ui()
        self._refresh_delivery_status()
        self._refresh()

    def set_preferences_scope(self, database_id: str) -> None:
        self._database_id = database_id
        for button in self._delivery_buttons.values():
            button.setEnabled(True)
        self._refresh_delivery_status()

    def _refresh_delivery_status(self) -> None:
        for (profile, action), label in self._delivery_status.items():
            path = (DeliveryTemplateStore(self._database_id, profile=profile).selected(action)
                    if self._database_id is not None else None)
            status = "연결된 양식" if path else "기본 양식"
            if profile is DocumentTemplateProfile.HAPPY_LUNCHBOX and action is DocumentAction.DELIVERY_REPORT:
                lunch = DeliveryTemplateStore(self._database_id, profile=profile).selected(action, meal="중식") if self._database_id else None
                status = "조식·석식: " + status + " / 중식: " + ("연결된 양식" if lunch else "기본 양식")
            label.setText(f"{profile.value} · {action.label}: {status}")

    @dialog_workflow
    def _edit_delivery(self, profile, action) -> None:
        dialog = self._delivery_dialog_type(action, self._database_id, self, profile=profile)
        (yield DialogRequest(dialog))
        self._refresh_delivery_status()

    @property
    def confirmation_single_meal_first(self) -> bool:
        return self.confirmation_order.isChecked()

    @property
    def confirmation_include_next_month_weekend(self) -> bool:
        return self.confirmation_next_weekend.isChecked()

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
        title = QLabel("문서 양식 선택")
        title.setObjectName("workspaceTitle")
        subtitle = QLabel(
            "청구서·확인서·일일식수·배송목록에 사용할 양식을 선택합니다. 급식·행복도시락 기본 양식을 제공하며 행복도시락 일일식수는 중식·석식 양식이 따로 있습니다."
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
            for action in (DocumentAction.DELIVERY_REPORT, DocumentAction.DELIVERY_SUBSTITUTE):
                status = QLabel()
                status.setObjectName("documentTemplateDescription")
                self._delivery_status[profile, action] = status
                card_layout.addWidget(status)
                edit = QPushButton(f"{action.label} 양식 확인/수정")
                edit.setObjectName("quietAction")
                edit.setEnabled(False)
                edit.clicked.connect(lambda _checked=False, p=profile, a=action: launch(self._edit_delivery, p, a))
                self._delivery_buttons[profile, action] = edit
                card_layout.addWidget(edit)
            cards.addWidget(card, 1)
        root.addLayout(cards, 1)

        self.confirmation_order = QCheckBox("확인서: 1식짜리 먼저, 2식·3식 순으로 배치")
        self.confirmation_order.setChecked(bool(self._current and self._current.confirmation_single_meal_first))
        self.confirmation_order.setToolTip("관할동별 시트 안에서 1식·2식·3식 순으로 배치합니다. 해제하면 기존 출력 순서를 유지합니다.")
        root.addWidget(self.confirmation_order)

        self.confirmation_next_weekend = QCheckBox("확인서: 다음 달로 넘어가는 토·일요일을 마지막 주 날짜에 포함")
        self.confirmation_next_weekend.setChecked(bool(self._current and self._current.confirmation_include_next_month_weekend))
        self.confirmation_next_weekend.setToolTip(
            "월말이 금요일·토요일이면 다음 달 일요일까지 표시합니다. 예: 07.27~08.02 또는 10.26~11.01. "
            "날짜 표시만 바뀌며 식수는 식수·청구서 설정의 다음 달 식수 포함 여부를 따릅니다."
        )
        root.addWidget(self.confirmation_next_weekend)

        note = QLabel(
            "양식 선택과 확인서 옵션은 ‘이 양식 사용’을 눌러 운영 파일에 저장합니다. 배송 양식 파일 연결은 각 편집창에서 별도로 저장하며 이 PC에서 기억합니다."
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
            self._error.setText(user_error(error))
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

    @dialog_workflow
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
        return (yield from invoke(self._choose_and_save, roster, state, current, parent))

    @dialog_workflow
    def run(
        self,
        parent: QWidget | None = None,
    ) -> DocumentTemplateChangeOutcome:
        roster, state, current = self._current(parent)
        if roster is None:
            return DocumentTemplateChangeOutcome(DocumentTemplateChangeStatus.FAILED)
        return (yield from invoke(self._choose_and_save, roster, state, current, parent))

    def _current(self, parent):
        def load():
            roster = self._session.database.current_roster()
            if roster is None:
                raise ValueError("현재 명단이 없습니다. 기존 명단 불러오기를 먼저 완료해주세요.")
            snapshot = self._session.database.snapshot(roster.revision_id)
            state = snapshot.entity(DOCUMENT_TEMPLATE_SETTING_KIND, DOCUMENT_TEMPLATE_SETTING_ID)
            return roster, state, document_template_setting_from_state(state)

        try:
            return run_progress_task(
                load, title="문서 양식 설정을 불러오는 중입니다",
                message="문서 생성에 사용할 저장된 설정을 확인하는 중입니다.", parent=parent,
            )
        except Exception as error:
            self._message_box.critical(parent, "문서 양식 설정을 읽지 못했습니다", user_error(error))
            return None, None, None

    @dialog_workflow
    def _choose_and_save(self, roster, state, current, parent):
        dialog = self._dialog_type(current, parent)
        scope_setter = getattr(dialog, "set_preferences_scope", None)
        if callable(scope_setter):
            scope_setter(str(self._session.credentials.database_id))
        if (yield DialogRequest(dialog, self._session)) != QDialog.DialogCode.Accepted:
            return DocumentTemplateChangeOutcome(
                DocumentTemplateChangeStatus.CANCELLED,
                roster,
                current,
            )
        profile = dialog.selected_profile
        single_first = getattr(dialog, "confirmation_single_meal_first",
                               current.confirmation_single_meal_first if current else False)
        include_weekend = getattr(dialog, "confirmation_include_next_month_weekend",
                                 current.confirmation_include_next_month_weekend if current else False)
        if (current is not None and current.profile is profile
                and current.confirmation_single_meal_first == single_first
                and current.confirmation_include_next_month_weekend == include_weekend):
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
                confirmation_single_meal_first=single_first,
                confirmation_include_next_month_weekend=include_weekend,
                actor=self._actor_provider(),
                recorded_at=self._recorded_at_provider(),
                revision_id=self._revision_id_factory(),
                reason=(
                    "문서 양식 첫 설정"
                    if current is None
                    else "문서 양식 변경"
                ),
            )
            backup_result = save_revision(self._session, prepared.record, parent=parent)
            updated_roster = run_progress_task(
                self._session.database.current_roster,
                title="저장된 설정을 불러오는 중입니다",
                message="문서 생성에 사용할 최신 설정을 읽는 중입니다.", parent=parent,
            )
            if updated_roster is None:
                raise RuntimeError("저장 뒤 현재 명단을 다시 읽지 못했습니다.")
        except Exception as error:
            self._message_box.critical(
                parent,
                "문서 양식을 저장하지 못했습니다",
                "양식과 변경 이력은 저장하지 않았습니다. 다시 시도해주세요.\n\n"
                + user_error(error),
            )
            return DocumentTemplateChangeOutcome(DocumentTemplateChangeStatus.FAILED)
        if backup_result.warning_required:
            self._message_box.warning(
                parent,
                "문서 양식은 저장됐지만 자동 백업에 실패했습니다",
                "양식 선택은 운영 데이터에 저장되었습니다. 백업 폴더를 확인해주세요.\n\n"
                + user_error(backup_result.error),
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
