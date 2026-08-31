"""Multi-meal delivery-status editor and revision/backup controller."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from PySide6.QtCore import QDate
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDateEdit,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from src.domain import (
    InclusiveDateRange,
    Meal,
    MealStatus,
    Person,
    RevisionActor,
    RosterSnapshot,
    StatusHistory,
    build_meal_status_revision,
)
from src.storage import AutomaticBackupResult, DatabaseSession

from .import_controller import current_windows_actor
from .theme import application_stylesheet, register_application_fonts


class MealStatusChangeDialog(QDialog):
    """Apply one state and period to one or more selected meal lanes."""

    def __init__(
        self,
        person: Person,
        roster: RosterSnapshot,
        histories: Mapping[Meal, StatusHistory],
        anchor_day: date,
        initial_meal: Meal,
        initial_status: MealStatus,
        parent: QWidget | None = None,
    ) -> None:
        if not isinstance(person, Person):
            raise TypeError("person은 Person이어야 합니다.")
        if not isinstance(roster, RosterSnapshot):
            raise TypeError("roster는 RosterSnapshot이어야 합니다.")
        roster.person(person.person_id)
        if not isinstance(anchor_day, date):
            raise TypeError("anchor_day는 date여야 합니다.")
        if not isinstance(initial_meal, Meal):
            raise TypeError("initial meal은 Meal이어야 합니다.")
        if not isinstance(initial_status, MealStatus):
            raise TypeError("initial status는 MealStatus여야 합니다.")
        history_by_meal = dict(histories)
        if any(
            not isinstance(meal, Meal) or not isinstance(history, StatusHistory)
            for meal, history in history_by_meal.items()
        ):
            raise TypeError("histories는 Meal별 StatusHistory mapping이어야 합니다.")

        super().__init__(parent)
        self._person = person
        self._roster = roster
        self._histories = history_by_meal
        self._meal_checks: dict[Meal, QCheckBox] = {}
        self._meal_cards: dict[Meal, QFrame] = {}
        self._current_labels: dict[Meal, QLabel] = {}
        self._status_buttons: dict[MealStatus, QPushButton] = {}
        register_application_fonts()
        self.setObjectName("mealStatusChangeDialog")
        self.setWindowTitle(f"{person.profile.name} · 배송 상태 변경")
        self.resize(690, 650)
        self.setMinimumSize(620, 590)
        self.setStyleSheet(application_stylesheet())
        self._build_ui(anchor_day)
        self._meal_checks[initial_meal].setChecked(True)
        self._status_buttons[initial_status].setChecked(True)
        self._continue.setChecked(initial_status is not MealStatus.PAUSED)
        self._refresh_state()

    def selected_meals(self) -> tuple[Meal, ...]:
        return tuple(
            meal for meal in Meal if self._meal_checks[meal].isChecked()
        )

    def selected_status(self) -> MealStatus:
        for status in MealStatus:
            if self._status_buttons[status].isChecked():
                return status
        raise ValueError("변경할 상태를 선택해야 합니다.")

    def selected_period(self) -> InclusiveDateRange:
        starts_on = _python_date(self._starts_on)
        ends_on = None if self._continue.isChecked() else _python_date(self._ends_on)
        period = InclusiveDateRange(starts_on, ends_on)
        if self.selected_status() is MealStatus.PAUSED and ends_on is None:
            raise ValueError("일시중지는 종료일이 필요합니다.")
        return period

    def _build_ui(self, anchor_day: date) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 18, 18, 16)
        root.setSpacing(11)

        header = QFrame()
        header.setObjectName("statusChangeHeader")
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(16, 14, 16, 14)
        header_layout.setSpacing(3)
        eyebrow = QLabel("급식 노선 · 기간 변경")
        eyebrow.setObjectName("editEyebrow")
        header_layout.addWidget(eyebrow)
        title = QLabel(f"{self._person.profile.name} 배송 상태 변경")
        title.setObjectName("workspaceTitle")
        header_layout.addWidget(title)
        guide = QLabel(
            "여러 식종을 선택하면 같은 상태와 기간이 한 번에 적용됩니다."
        )
        guide.setObjectName("workspaceSubtitle")
        guide.setWordWrap(True)
        header_layout.addWidget(guide)
        root.addWidget(header)

        root.addWidget(_section_label("적용할 식종"))
        meal_row = QHBoxLayout()
        meal_row.setSpacing(8)
        for meal in Meal:
            card = QFrame()
            card.setObjectName("mealChoiceCard")
            card.setProperty("selected", False)
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(12, 10, 12, 10)
            check = QCheckBox(meal.value)
            check.setObjectName("mealChoiceCheck")
            check.toggled.connect(self._refresh_state)
            current = QLabel()
            current.setObjectName("mealCurrentState")
            current.setWordWrap(True)
            card_layout.addWidget(check)
            card_layout.addWidget(current)
            meal_row.addWidget(card, 1)
            self._meal_checks[meal] = check
            self._meal_cards[meal] = card
            self._current_labels[meal] = current
        root.addLayout(meal_row)

        root.addWidget(_section_label("변경할 상태"))
        status_row = QHBoxLayout()
        status_row.setSpacing(8)
        status_group = QButtonGroup(self)
        status_group.setExclusive(True)
        for status, hint in (
            (MealStatus.DELIVERY, "정상 배송·미가입은 자동 추가"),
            (MealStatus.PAUSED, "정해진 기간만 잠시 중단"),
            (MealStatus.STOPPED, "장기 또는 무기한 중지"),
        ):
            button = QPushButton(f"{status.value}\n{hint}")
            button.setObjectName("statusChoice")
            button.setCheckable(True)
            button.setProperty("statusKind", status.name.lower())
            button.clicked.connect(self._refresh_state)
            status_group.addButton(button)
            status_row.addWidget(button, 1)
            self._status_buttons[status] = button
        root.addLayout(status_row)

        period_frame = QFrame()
        period_frame.setObjectName("editSection")
        period_layout = QVBoxLayout(period_frame)
        period_layout.setContentsMargins(14, 12, 14, 13)
        period_layout.addWidget(_section_label("기간"))
        period_grid = QGridLayout()
        period_grid.setHorizontalSpacing(12)
        period_grid.addWidget(_field_label("시작일"), 0, 0)
        period_grid.addWidget(_field_label("종료일"), 0, 1)
        self._starts_on = _date_edit(anchor_day)
        self._ends_on = _date_edit(anchor_day)
        self._starts_on.dateChanged.connect(self._refresh_state)
        self._ends_on.dateChanged.connect(self._refresh_state)
        period_grid.addWidget(self._starts_on, 1, 0)
        period_grid.addWidget(self._ends_on, 1, 1)
        period_layout.addLayout(period_grid)
        self._continue = QCheckBox("종료일 없음 · 계속")
        self._continue.toggled.connect(self._on_continue_toggled)
        period_layout.addWidget(self._continue)
        root.addWidget(period_frame)

        summary = QFrame()
        summary.setObjectName("changeSummary")
        summary_layout = QVBoxLayout(summary)
        summary_layout.setContentsMargins(14, 11, 14, 11)
        summary_layout.addWidget(_section_label("변경 결과"))
        self._summary_text = QLabel()
        self._summary_text.setObjectName("summaryText")
        self._summary_text.setWordWrap(True)
        summary_layout.addWidget(self._summary_text)
        root.addWidget(summary)

        self._error = QLabel()
        self._error.setObjectName("errorMessage")
        self._error.setWordWrap(True)
        root.addWidget(self._error)

        actions = QHBoxLayout()
        actions.addStretch(1)
        cancel = QPushButton("취소")
        cancel.setObjectName("quietAction")
        cancel.clicked.connect(self.reject)
        actions.addWidget(cancel)
        self._save_button = QPushButton("변경 저장")
        self._save_button.setObjectName("primaryAction")
        self._save_button.clicked.connect(self._save)
        actions.addWidget(self._save_button)
        root.addLayout(actions)

    def _on_continue_toggled(self, checked: bool) -> None:
        self._ends_on.setEnabled(not checked)
        self._refresh_state()

    def _refresh_state(self, *_args) -> None:
        for meal, card in self._meal_cards.items():
            card.setProperty("selected", self._meal_checks[meal].isChecked())
            card.style().unpolish(card)
            card.style().polish(card)

        status = self._checked_status()
        if status is MealStatus.PAUSED and self._continue.isChecked():
            self._continue.blockSignals(True)
            self._continue.setChecked(False)
            self._continue.blockSignals(False)
            self._ends_on.setEnabled(True)
        self._continue.setEnabled(status is not MealStatus.PAUSED)

        starts_on = _python_date(self._starts_on)
        for meal, label in self._current_labels.items():
            label.setText(f"현재  {_current_state(self._roster, self._histories, self._person, meal, starts_on)}")

        error = self._validation_error()
        self._error.setText(error or "")
        self._error.setVisible(error is not None)
        self._save_button.setEnabled(error is None)
        self._summary_text.setText(self._summary())

    def _checked_status(self) -> MealStatus | None:
        return next(
            (
                status
                for status in MealStatus
                if self._status_buttons[status].isChecked()
            ),
            None,
        )

    def _validation_error(self) -> str | None:
        meals = self.selected_meals()
        if not meals:
            return "한 개 이상의 식종을 선택해주세요."
        status = self._checked_status()
        if status is None:
            return "변경할 상태를 선택해주세요."
        try:
            period = self.selected_period()
        except ValueError as error:
            return str(error)
        if status is MealStatus.DELIVERY:
            return None
        not_enrolled = tuple(
            meal
            for meal in meals
            if meal not in self._roster.meals_for(
                self._person.person_id,
                on=period.starts_on,
            )
        )
        if not_enrolled:
            labels = " · ".join(meal.value for meal in not_enrolled)
            return (
                f"{labels}은 시작일에 미가입입니다. "
                "미가입 식종은 배송으로 먼저 시작해주세요."
            )
        return None

    def _summary(self) -> str:
        meals = self.selected_meals()
        status = self._checked_status()
        if not meals or status is None:
            return "식종과 상태를 선택하면 변경 결과가 표시됩니다."
        try:
            period = self.selected_period()
        except ValueError:
            return "기간을 확인해주세요."
        period_text = _period_text(period)
        lines: list[str] = []
        for meal in meals:
            current = _current_state(
                self._roster,
                self._histories,
                self._person,
                meal,
                period.starts_on,
            )
            if status is MealStatus.DELIVERY and current == "미가입":
                after = "식종 추가 · 배송"
            else:
                after = status.value
            lines.append(f"{meal.value}   {current} → {after}   ·   {period_text}")
        return "\n".join(lines)

    def _save(self) -> None:
        error = self._validation_error()
        if error is not None:
            self._error.setText(error)
            self._error.show()
            return
        self.accept()


class MealStatusChangeStatus(StrEnum):
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class MealStatusChangeOutcome:
    status: MealStatusChangeStatus
    roster: RosterSnapshot | None = None
    backup_result: AutomaticBackupResult | None = None

    @property
    def completed(self) -> bool:
        return self.status is MealStatusChangeStatus.COMPLETED


class MealStatusChangeController:
    """Commit one accepted multi-meal status change and attempt a backup."""

    def __init__(
        self,
        session: DatabaseSession,
        *,
        actor_provider: Callable[[], RevisionActor] | None = None,
        dialog_type=MealStatusChangeDialog,
        message_box=QMessageBox,
        recorded_at_provider: Callable[[], datetime] | None = None,
        revision_id_factory: Callable[[], UUID] = uuid4,
        event_id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        if not isinstance(session, DatabaseSession):
            raise TypeError("session은 DatabaseSession이어야 합니다.")
        self._session = session
        self._actor_provider = actor_provider or current_windows_actor
        self._dialog_type = dialog_type
        self._message_box = message_box
        self._recorded_at_provider = recorded_at_provider or _utc_now
        self._revision_id_factory = revision_id_factory
        self._event_id_factory = event_id_factory

    def run(
        self,
        person_id: UUID,
        initial_meal: Meal,
        initial_status: MealStatus,
        anchor_day: date,
        parent: QWidget | None = None,
    ) -> MealStatusChangeOutcome:
        roster = self._session.database.current_roster()
        if roster is None:
            self._message_box.critical(
                parent,
                "배송 상태를 변경하지 못했습니다",
                "현재 명단이 없습니다. 기존 명단 가져오기를 먼저 완료해주세요.",
            )
            return MealStatusChangeOutcome(MealStatusChangeStatus.FAILED)
        try:
            person = roster.person(person_id)
        except (KeyError, TypeError) as error:
            self._message_box.critical(
                parent,
                "배송 상태를 변경하지 못했습니다",
                str(error),
            )
            return MealStatusChangeOutcome(MealStatusChangeStatus.FAILED)
        histories = {
            meal: self._session.database.status_history(person_id, meal)
            for meal in Meal
        }
        dialog = self._dialog_type(
            person,
            roster,
            histories,
            anchor_day,
            initial_meal,
            initial_status,
            parent,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return MealStatusChangeOutcome(MealStatusChangeStatus.CANCELLED)
        try:
            meals = dialog.selected_meals()
            prepared = build_meal_status_revision(
                roster,
                person_id,
                meals,
                dialog.selected_status(),
                dialog.selected_period(),
                actor=self._actor_provider(),
                recorded_at=self._recorded_at_provider(),
                revision_id=self._revision_id_factory(),
                event_ids={meal: self._event_id_factory() for meal in meals},
            )
            backup_result = self._session.append_revision(prepared.record)
        except Exception as error:
            self._message_box.critical(
                parent,
                "배송 상태를 저장하지 못했습니다",
                "저장된 명단에는 반영하지 않았습니다. "
                "내용을 확인한 뒤 다시 시도해주세요.\n\n"
                + str(error),
            )
            return MealStatusChangeOutcome(MealStatusChangeStatus.FAILED)
        if backup_result.error is not None:
            self._message_box.warning(
                parent,
                "배송 상태는 저장됐지만 자동 백업에 실패했습니다",
                "변경 내용과 이력은 DB에 정상 저장되었습니다. "
                "백업 폴더 연결을 확인한 뒤 백업을 다시 실행해주세요.\n\n"
                + str(backup_result.error),
            )
        return MealStatusChangeOutcome(
            MealStatusChangeStatus.COMPLETED,
            prepared.snapshot,
            backup_result,
        )


def _current_state(
    roster: RosterSnapshot,
    histories: Mapping[Meal, StatusHistory],
    person: Person,
    meal: Meal,
    on: date,
) -> str:
    if meal not in roster.meals_for(person.person_id, on=on):
        return "미가입"
    history = histories.get(meal)
    return (
        MealStatus.DELIVERY.value
        if history is None
        else history.status_on(on).value
    )


def _date_edit(value: date) -> QDateEdit:
    editor = QDateEdit(QDate(value.year, value.month, value.day))
    editor.setCalendarPopup(True)
    editor.setDisplayFormat("yyyy.MM.dd")
    return editor


def _python_date(editor: QDateEdit) -> date:
    value = editor.date()
    return date(value.year(), value.month(), value.day())


def _period_text(period: InclusiveDateRange) -> str:
    starts = period.starts_on.isoformat() if period.starts_on else "이전부터"
    ends = period.ends_on.isoformat() if period.ends_on else "계속"
    return f"{starts} ~ {ends}"


def _section_label(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("sectionLabel")
    return label


def _field_label(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("fieldLabel")
    return label


def _utc_now() -> datetime:
    return datetime.now(UTC)
