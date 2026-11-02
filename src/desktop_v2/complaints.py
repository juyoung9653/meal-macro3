"""Complaint registration dialog and its revision-backed controller."""

from __future__ import annotations

from .user_messages import user_error

from .dialog_workflow import DialogRequest, dialog_workflow

from .startup_loading import save_revision

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from PySide6.QtCore import QDateTime
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from src.domain.complaints import (
    MAX_COMPLAINT_CONTENT_LENGTH,
    Complaint,
    build_complaints_revision,
)
from src.domain.intake import IntakeChannel
from src.domain.revisions import RevisionActor
from src.domain.roster import Meal, Person, RosterSnapshot
from src.storage.database_session import DatabaseSession

from .date_edit import WheelSafeDateTimeEdit
from .import_controller import current_windows_actor
from .theme import application_stylesheet, register_application_fonts


class ComplaintDialog(QDialog):
    def __init__(
        self,
        person: Person,
        meal: Meal | None = None,
        *,
        roster: RosterSnapshot | None = None,
        household_day: date | None = None,
        received_at: datetime | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        if not isinstance(person, Person):
            raise TypeError("person은 Person이어야 합니다.")
        if meal is not None and not isinstance(meal, Meal):
            raise TypeError("meal은 Meal 또는 None이어야 합니다.")
        if roster is not None and not isinstance(roster, RosterSnapshot):
            raise TypeError("roster는 RosterSnapshot 또는 None이어야 합니다.")
        self._roster = roster
        self._household_day = household_day
        self._person_id = person.person_id
        register_application_fonts()
        self.setStyleSheet(application_stylesheet())
        self.setWindowTitle("민원 접수")
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self._person = QLabel(person.profile.name)
        self._person.setObjectName("complaintPerson")
        form.addRow("대상자", self._person)
        self._received_at = WheelSafeDateTimeEdit()
        self._received_at.setObjectName("complaintReceivedAt")
        self._received_at.setCalendarPopup(True)
        self._received_at.setDisplayFormat("yyyy-MM-dd HH:mm")
        initial = received_at or datetime.now().astimezone()
        if initial.tzinfo is None or initial.utcoffset() is None:
            raise ValueError("received_at에는 시간대가 있어야 합니다.")
        self._local_timezone = datetime.now().astimezone().tzinfo
        assert self._local_timezone is not None
        self._received_at.setDateTime(QDateTime(initial.astimezone(self._local_timezone)))
        form.addRow("접수 시각", self._received_at)
        self._channel = QComboBox()
        self._channel.setObjectName("complaintChannel")
        for channel in IntakeChannel:
            self._channel.addItem(channel.value, channel)
        form.addRow("접수 경로", self._channel)
        meal_row = QHBoxLayout()
        self._meal_checks: dict[Meal, QCheckBox] = {}
        for choice in Meal:
            check = QCheckBox(choice.value)
            check.setObjectName(f"complaintMeal{choice.name.title()}")
            check.setChecked(choice is meal)
            self._meal_checks[choice] = check
            meal_row.addWidget(check)
        form.addRow("관련 식종", meal_row)
        layout.addLayout(form)
        self._same_household = QCheckBox("같은 가구에도 함께 접수")
        self._same_household.setObjectName("complaintSameHousehold")
        eligible = self._household_person_ids()
        self._same_household.setEnabled(len(eligible) > 1)
        layout.addWidget(self._same_household)
        self._household_day_label = QLabel(
            "가구 기준일: " + (household_day.isoformat() if household_day else "없음")
        )
        layout.addWidget(self._household_day_label)
        self._targets = QLabel()
        self._targets.setObjectName("complaintTargets")
        layout.addWidget(self._targets)
        layout.addWidget(QLabel("민원 내용"))
        self._content = QPlainTextEdit()
        self._content.setObjectName("complaintContent")
        self._content.setMaximumBlockCount(32_768)
        layout.addWidget(self._content)
        self._error = QLabel()
        self._error.setObjectName("complaintError")
        self._error.setStyleSheet("color: #b42318;")
        self._error.hide()
        layout.addWidget(self._error)
        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        self._save_button = self._buttons.button(QDialogButtonBox.StandardButton.Save)
        self._save_button.setText("저장")
        self._buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("취소")
        self._buttons.accepted.connect(self._accept_if_valid)
        self._buttons.rejected.connect(self.reject)
        self._content.textChanged.connect(self._validate)
        self._same_household.toggled.connect(self._refresh_targets)
        layout.addWidget(self._buttons)
        self._refresh_targets()
        self._validate()

    def received_at(self) -> datetime:
        value = self._received_at.dateTime().toPython()
        return value.replace(tzinfo=None).replace(tzinfo=self._local_timezone)

    def channel(self) -> IntakeChannel:
        return IntakeChannel(self._channel.currentData())

    def content(self) -> str:
        return self._content.toPlainText().strip()

    def meal(self) -> Meal | None:
        meals = self.selected_meals()
        return meals[0] if len(meals) == 1 else None

    def selected_meals(self) -> tuple[Meal, ...]:
        return tuple(meal for meal in Meal if self._meal_checks[meal].isChecked())

    def selected_person_ids(self) -> tuple[UUID, ...]:
        return self._household_person_ids() if self._same_household.isChecked() else (self._person_id,)

    def _household_person_ids(self) -> tuple[UUID, ...]:
        if self._roster is None or self._household_day is None:
            return (self._person_id,)
        household_ids = {
            membership.household_id
            for membership in self._roster.memberships
            if membership.person_id == self._person_id
            and membership.period.contains(self._household_day)
        }
        people = {self._person_id}
        for household_id in household_ids:
            people.update(
                member.person_id
                for member in self._roster.household_members(household_id, on=self._household_day)
            )
        return tuple(person_id for person_id in (person.person_id for person in self._roster.persons) if person_id in people)

    def _refresh_targets(self) -> None:
        person_ids = self.selected_person_ids()
        if self._roster is None:
            names = self._person.text()
        else:
            names = ", ".join(self._roster.person(person_id).profile.name for person_id in person_ids)
        self._targets.setText(f"접수 대상 {len(person_ids)}명: {names}")

    def _validate(self) -> bool:
        content_length = len(self.content())
        valid = 0 < content_length <= MAX_COMPLAINT_CONTENT_LENGTH
        self._save_button.setEnabled(valid)
        if valid:
            self._error.hide()
        else:
            self._error.setText(
                "민원 내용을 입력해주세요."
                if content_length == 0
                else f"민원 내용은 {MAX_COMPLAINT_CONTENT_LENGTH:,}자 이하여야 합니다."
            )
            self._error.show()
        return valid

    def _accept_if_valid(self) -> None:
        if self._validate():
            self.accept()


class ComplaintStatus(StrEnum):
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ComplaintOutcome:
    status: ComplaintStatus
    roster: RosterSnapshot | None = None

    @property
    def completed(self) -> bool:
        return self.status is ComplaintStatus.COMPLETED


class ComplaintController:
    def __init__(
        self,
        session: DatabaseSession,
        *,
        actor_provider: Callable[[], RevisionActor] | None = None,
        dialog_type=ComplaintDialog,
        message_box=QMessageBox,
        now_provider: Callable[[], datetime] | None = None,
        recorded_at_provider: Callable[[], datetime] | None = None,
        complaint_id_factory: Callable[[], UUID] = uuid4,
        revision_id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        self._session = session
        self._actor_provider = actor_provider or current_windows_actor
        self._dialog_type = dialog_type
        self._message_box = message_box
        self._now_provider = now_provider or (lambda: datetime.now().astimezone())
        self._recorded_at_provider = recorded_at_provider or self._now_provider
        self._complaint_id_factory = complaint_id_factory
        self._revision_id_factory = revision_id_factory

    @dialog_workflow
    def run(
        self,
        person_id: UUID,
        meal: Meal | None = None,
        parent: QWidget | None = None,
        *,
        household_day: date | None = None,
    ) -> ComplaintOutcome:
        if not isinstance(person_id, UUID):
            raise TypeError("person_id는 UUID여야 합니다.")
        if meal is not None and not isinstance(meal, Meal):
            raise TypeError("meal은 Meal 또는 None이어야 합니다.")
        roster = self._session.database.current_roster()
        if roster is None:
            return ComplaintOutcome(ComplaintStatus.FAILED)
        try:
            person = roster.person(person_id)
        except KeyError as error:
            self._message_box.critical(parent, "민원을 접수하지 못했습니다", user_error(error))
            return ComplaintOutcome(ComplaintStatus.FAILED)
        received_at = self._now_provider()
        if received_at.tzinfo is None or received_at.utcoffset() is None:
            raise ValueError("now_provider는 시간대가 있는 datetime을 반환해야 합니다.")
        selected_day = household_day or received_at.astimezone().date()
        dialog_type = self._dialog_type
        if isinstance(dialog_type, type) and issubclass(dialog_type, ComplaintDialog):
            dialog = self._dialog_type(
                person, meal, roster=roster, household_day=selected_day,
                received_at=received_at, parent=parent,
            )
        else:
            dialog = self._dialog_type(person, meal, received_at=received_at, parent=parent)
        if (yield DialogRequest(dialog, self._session)) != QDialog.DialogCode.Accepted:
            return ComplaintOutcome(ComplaintStatus.CANCELLED)
        try:
            selected_meals = (
                dialog.selected_meals()
                if hasattr(dialog, "selected_meals")
                else (() if dialog.meal() is None else (dialog.meal(),))
            )
            person_ids = (
                dialog.selected_person_ids()
                if hasattr(dialog, "selected_person_ids")
                else (person_id,)
            )
            complaints = tuple(
                Complaint(
                    self._complaint_id_factory(), target_id, dialog.received_at(),
                    dialog.channel(), dialog.content(), meals=selected_meals,
                )
                for target_id in person_ids
            )
            record = build_complaints_revision(
                roster, complaints, actor=self._actor_provider(),
                recorded_at=self._recorded_at_provider(), revision_id=self._revision_id_factory(),
            )
            backup = save_revision(self._session, record, parent=parent)
        except Exception as error:  # noqa: BLE001 - report save failures at UI boundary
            self._message_box.critical(parent, "민원을 접수하지 못했습니다", user_error(error))
            return ComplaintOutcome(ComplaintStatus.FAILED)
        if backup.warning_required:
            self._message_box.warning(
                parent, "민원은 저장됐지만 자동 백업에 실패했습니다", user_error(backup.error)
            )
        return ComplaintOutcome(
            ComplaintStatus.COMPLETED, self._session.database.current_roster() or roster
        )
