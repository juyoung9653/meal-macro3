"""Effective-dated breakfast delivery-policy editor and persistence controller."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QDateEdit,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from src.domain import (
    BREAKFAST_DELIVERY_POLICY_ID,
    BREAKFAST_DELIVERY_POLICY_KIND,
    BreakfastDeliveryPolicyChange,
    BreakfastDeliveryPolicySchedule,
    ClosedFridayMode,
    RevisionActor,
    RosterSnapshot,
    breakfast_delivery_policy_from_state,
    build_breakfast_delivery_policy_revision,
)
from src.storage import AutomaticBackupResult, DatabaseSession

from .import_controller import current_windows_actor
from .theme import application_stylesheet, register_application_fonts


_MODE_COPY = {
    ClosedFridayMode.THURSDAY_WEEKEND_ONLY: (
        "목요일에 토·일분 미리 배송",
        "금요일이 휴무인 주에는 토요일·일요일 조식을 목요일 배송에 함께 묶습니다.",
        "예: 금요일 휴무 → 목요일에 목·토·일분 배송 / 금요일분은 배송하지 않음",
    ),
    ClosedFridayMode.SKIP_FRIDAY_WEEKEND: (
        "금·토·일분 모두 미배송",
        "금요일이 휴무인 주에는 금요일부터 일요일까지 조식을 자동 배송하지 않습니다.",
        "예: 금요일 휴무 → 목요일에는 목요일분만 배송 / 금·토·일분 미배송",
    ),
}


class BreakfastDeliveryPolicyDialog(QDialog):
    """Choose how a closed Friday changes that week's breakfast deliveries."""

    def __init__(
        self,
        schedule: BreakfastDeliveryPolicySchedule,
        anchor_day: date,
        parent: QWidget | None = None,
    ) -> None:
        if not isinstance(schedule, BreakfastDeliveryPolicySchedule):
            raise TypeError("schedule은 BreakfastDeliveryPolicySchedule이어야 합니다.")
        if isinstance(anchor_day, datetime) or not isinstance(anchor_day, date):
            raise TypeError("anchor_day는 date여야 합니다.")
        super().__init__(parent)
        self._schedule = schedule
        self._mode_buttons: dict[ClosedFridayMode, QRadioButton] = {}
        self._mode_cards: dict[ClosedFridayMode, QFrame] = {}
        register_application_fonts()
        self.setObjectName("breakfastDeliveryPolicyDialog")
        self.setWindowTitle("조식 배송 규칙")
        self.setModal(True)
        self.resize(820, 640)
        self.setMinimumSize(720, 640)
        self.setStyleSheet(application_stylesheet())
        self._build_ui(anchor_day)
        self._mode_buttons[schedule.mode_on(anchor_day)].setChecked(True)
        self._refresh_state()

    def selected_mode(self) -> ClosedFridayMode:
        for mode, button in self._mode_buttons.items():
            if button.isChecked():
                return mode
        raise RuntimeError("조식 배송 규칙을 선택하지 않았습니다.")

    def selected_effective_on(self) -> date:
        value = self._effective_on.date()
        return date(value.year(), value.month(), value.day())

    def _build_ui(self, anchor_day: date) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 17)
        root.setSpacing(12)

        header = QFrame()
        header.setObjectName("breakfastPolicyHeader")
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(18, 15, 18, 16)
        header_layout.setSpacing(4)
        eyebrow = QLabel("BREAKFAST ROUTE · 금요일 휴무 주간")
        eyebrow.setObjectName("editEyebrow")
        title = QLabel("조식 배송 규칙")
        title.setObjectName("workspaceTitle")
        subtitle = QLabel(
            "금요일 배송이 쉬는 주에 토·일분 조식을 어떻게 처리할지 정합니다."
        )
        subtitle.setObjectName("workspaceSubtitle")
        subtitle.setWordWrap(True)
        header_layout.addWidget(eyebrow)
        header_layout.addWidget(title)
        header_layout.addWidget(subtitle)
        root.addWidget(header)

        current = QFrame()
        current.setObjectName("breakfastPolicyCurrent")
        current_layout = QHBoxLayout(current)
        current_layout.setContentsMargins(14, 11, 14, 11)
        current_layout.setSpacing(12)
        stamp = QLabel("현재")
        stamp.setObjectName("breakfastPolicyStamp")
        stamp.setAlignment(Qt.AlignmentFlag.AlignCenter)
        stamp.setFixedSize(52, 42)
        self._current_summary = QLabel()
        self._current_summary.setObjectName("breakfastPolicyCurrentText")
        self._current_summary.setWordWrap(True)
        current_layout.addWidget(stamp)
        current_layout.addWidget(self._current_summary, 1)
        root.addWidget(current)

        section = QLabel("적용할 방식")
        section.setObjectName("sectionLabel")
        root.addWidget(section)

        self._button_group = QButtonGroup(self)
        self._button_group.setExclusive(True)
        for mode in ClosedFridayMode:
            card = QFrame()
            card.setObjectName("breakfastPolicyCard")
            card.setProperty("selected", False)
            layout = QVBoxLayout(card)
            layout.setContentsMargins(16, 12, 16, 12)
            layout.setSpacing(5)
            label, description, example = _MODE_COPY[mode]
            radio = QRadioButton(label)
            radio.setObjectName("breakfastPolicyChoice")
            radio.setAccessibleDescription(f"{description} {example}")
            detail = QLabel(description)
            detail.setObjectName("breakfastPolicyDescription")
            detail.setWordWrap(True)
            example_label = QLabel(example)
            example_label.setObjectName("breakfastPolicyExample")
            example_label.setWordWrap(True)
            layout.addWidget(radio)
            layout.addWidget(detail)
            layout.addWidget(example_label)
            self._button_group.addButton(radio)
            self._mode_buttons[mode] = radio
            self._mode_cards[mode] = card
            radio.toggled.connect(self._refresh_state)
            root.addWidget(card)

        date_panel = QFrame()
        date_panel.setObjectName("breakfastPolicyDatePanel")
        date_layout = QGridLayout(date_panel)
        date_layout.setContentsMargins(14, 11, 14, 12)
        date_layout.setHorizontalSpacing(18)
        date_layout.setVerticalSpacing(5)
        date_label = QLabel("적용일")
        date_label.setObjectName("fieldLabel")
        date_guide = QLabel(
            "이 날짜부터 새 규칙을 사용합니다. 이전 기간의 DB 이력과 이미 만든 Excel은 바뀌지 않습니다."
        )
        date_guide.setObjectName("breakfastPolicyDateGuide")
        date_guide.setWordWrap(True)
        self._effective_on = QDateEdit(
            QDate(anchor_day.year, anchor_day.month, anchor_day.day)
        )
        self._effective_on.setObjectName("breakfastPolicyDate")
        self._effective_on.setCalendarPopup(True)
        self._effective_on.setDisplayFormat("yyyy.MM.dd")
        self._effective_on.dateChanged.connect(self._refresh_state)
        date_layout.addWidget(date_label, 0, 0)
        date_layout.addWidget(self._effective_on, 1, 0)
        date_layout.addWidget(date_guide, 0, 1, 2, 1)
        date_layout.setColumnStretch(1, 1)
        root.addWidget(date_panel)

        note = QLabel(
            "휴무일 화면에서 ‘대체식 제공’을 따로 저장한 날은 이 기본 규칙보다 우선하여 실제 배송일에 잡힙니다."
        )
        note.setObjectName("breakfastPolicyNote")
        note.setWordWrap(True)
        root.addWidget(note)

        actions = QHBoxLayout()
        actions.addStretch(1)
        cancel = QPushButton("닫기")
        cancel.setObjectName("quietAction")
        cancel.clicked.connect(self.reject)
        actions.addWidget(cancel)
        self._save_button = QPushButton("조식 규칙 저장")
        self._save_button.setObjectName("primaryAction")
        self._save_button.clicked.connect(self.accept)
        actions.addWidget(self._save_button)
        root.addLayout(actions)

    def _refresh_state(self, *_args) -> None:
        effective_on = self.selected_effective_on()
        current_mode = self._schedule.mode_on(effective_on)
        current_label = _MODE_COPY[current_mode][0]
        source = self._schedule.effective_change_on(effective_on)
        source_text = (
            "기본 규칙"
            if source is None
            else f"{source.effective_on.strftime('%Y.%m.%d')}부터 저장된 규칙"
        )
        self._current_summary.setText(
            f"{effective_on.strftime('%Y.%m.%d')} 기준  ·  {current_label}\n{source_text}"
        )
        for mode, card in self._mode_cards.items():
            selected = self._mode_buttons[mode].isChecked()
            card.setProperty("selected", selected)
            card.style().unpolish(card)
            card.style().polish(card)


class BreakfastDeliveryPolicyStatus(StrEnum):
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class BreakfastDeliveryPolicyOutcome:
    status: BreakfastDeliveryPolicyStatus
    roster: RosterSnapshot | None = None
    schedule: BreakfastDeliveryPolicySchedule | None = None
    change: BreakfastDeliveryPolicyChange | None = None
    backup_result: AutomaticBackupResult | None = None
    changed: bool = False

    @property
    def completed(self) -> bool:
        return self.status is BreakfastDeliveryPolicyStatus.COMPLETED


class BreakfastDeliveryPolicyController:
    """Save a policy revision and run the same automatic backup as other edits."""

    def __init__(
        self,
        session: DatabaseSession,
        *,
        actor_provider: Callable[[], RevisionActor] | None = None,
        dialog_type=BreakfastDeliveryPolicyDialog,
        message_box=QMessageBox,
        recorded_at_provider: Callable[[], datetime] | None = None,
        revision_id_factory: Callable[[], UUID] = uuid4,
        change_id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        if not isinstance(session, DatabaseSession):
            raise TypeError("session은 DatabaseSession이어야 합니다.")
        self._session = session
        self._actor_provider = actor_provider or current_windows_actor
        self._dialog_type = dialog_type
        self._message_box = message_box
        self._recorded_at_provider = recorded_at_provider or _utc_now
        self._revision_id_factory = revision_id_factory
        self._change_id_factory = change_id_factory

    def run(
        self,
        anchor_day: date,
        parent: QWidget | None = None,
    ) -> BreakfastDeliveryPolicyOutcome:
        roster = self._session.database.current_roster()
        if roster is None:
            self._message_box.critical(
                parent,
                "조식 배송 규칙을 설정하지 못했습니다",
                "현재 명단이 없습니다. 기존 명단 가져오기를 먼저 완료해주세요.",
            )
            return BreakfastDeliveryPolicyOutcome(
                BreakfastDeliveryPolicyStatus.FAILED
            )
        source = self._session.database.snapshot(roster.revision_id)
        stored_state = source.entity(
            BREAKFAST_DELIVERY_POLICY_KIND,
            BREAKFAST_DELIVERY_POLICY_ID,
        )
        try:
            schedule = breakfast_delivery_policy_from_state(stored_state)
        except Exception as error:
            self._message_box.critical(
                parent,
                "조식 배송 규칙을 읽지 못했습니다",
                str(error),
            )
            return BreakfastDeliveryPolicyOutcome(
                BreakfastDeliveryPolicyStatus.FAILED
            )

        dialog = self._dialog_type(schedule, anchor_day, parent)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return BreakfastDeliveryPolicyOutcome(
                BreakfastDeliveryPolicyStatus.CANCELLED,
                roster,
                schedule,
            )
        effective_on = dialog.selected_effective_on()
        selected_mode = dialog.selected_mode()
        effective_change = schedule.effective_change_on(effective_on)
        if (
            effective_change is not None
            and effective_change.closed_friday_mode is selected_mode
        ):
            return BreakfastDeliveryPolicyOutcome(
                BreakfastDeliveryPolicyStatus.COMPLETED,
                roster,
                schedule,
                effective_change,
                changed=False,
            )

        try:
            prepared = build_breakfast_delivery_policy_revision(
                roster.revision_id,
                stored_state,
                effective_on,
                selected_mode,
                actor=self._actor_provider(),
                recorded_at=self._recorded_at_provider(),
                revision_id=self._revision_id_factory(),
                change_id=self._change_id_factory(),
            )
            backup_result = self._session.append_revision(prepared.record)
            updated_roster = self._session.database.current_roster()
            if updated_roster is None:
                raise RuntimeError("저장 뒤 현재 명단을 다시 읽지 못했습니다.")
        except Exception as error:
            self._message_box.critical(
                parent,
                "조식 배송 규칙을 저장하지 못했습니다",
                "규칙과 변경 이력은 저장하지 않았습니다. 내용을 확인한 뒤 다시 시도해주세요.\n\n"
                + str(error),
            )
            return BreakfastDeliveryPolicyOutcome(
                BreakfastDeliveryPolicyStatus.FAILED
            )
        if backup_result.error is not None:
            self._message_box.warning(
                parent,
                "조식 규칙은 저장됐지만 자동 백업에 실패했습니다",
                "규칙과 이력은 DB에 정상 저장되었습니다. 백업 폴더 연결을 확인한 뒤 백업을 다시 실행해주세요.\n\n"
                + str(backup_result.error),
            )
        return BreakfastDeliveryPolicyOutcome(
            BreakfastDeliveryPolicyStatus.COMPLETED,
            updated_roster,
            prepared.schedule,
            prepared.change,
            backup_result,
            True,
        )


def _utc_now() -> datetime:
    return datetime.now(UTC)
