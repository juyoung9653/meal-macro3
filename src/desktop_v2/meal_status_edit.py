"""Multi-meal delivery-status editor and revision/backup controller."""

from __future__ import annotations

from .dialog_workflow import DialogRequest, dialog_workflow, invoke

from .startup_loading import save_revision

from src.storage.database_session import committed_roster_after_save

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from PySide6.QtCore import QDate, QEvent, Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QComboBox,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from src.domain.roster import (
    InclusiveDateRange,
    Meal,
    Person,
    RosterSnapshot,
)
from src.domain.delivery_dates import (
    actual_delivery_weekdays,
    next_actual_delivery_date,
    validate_actual_delivery_date,
    weekday_text,
)
from src.domain.status import (
    MealStatus,
    StatusHistory,
)
from src.domain.revisions import RevisionActor
from src.domain.meal_status_changes import build_meal_status_revision
from src.domain.meal_status_changes import build_batch_meal_status_revision, validate_meal_status_transition
from src.domain.intake import IntakeChannel

from .date_edit import WheelSafeDateEdit
from .delivery_date_edit import DeliveryDateEdit
from src.storage.database_session import (
    AutomaticBackupResult,
    DatabaseSession,
)

from .import_controller import current_windows_actor
from .theme import application_stylesheet, register_application_fonts


class _MealChoiceCard(QFrame):
    """Let the card body toggle its checkbox without intercepting the checkbox."""

    def __init__(self) -> None:
        super().__init__()
        self._check: QCheckBox | None = None

    def set_check(self, check: QCheckBox, *click_targets: QWidget) -> None:
        self._check = check
        for target in click_targets:
            target.installEventFilter(self)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.isEnabled():
            self._toggle_check()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def eventFilter(self, watched, event) -> bool:
        if (
            event.type() == QEvent.Type.MouseButtonRelease
            and event.button() == Qt.MouseButton.LeftButton
            and self.isEnabled()
        ):
            self._toggle_check()
            return True
        return super().eventFilter(watched, event)

    def _toggle_check(self) -> None:
        if self._check is not None and self._check.isEnabled():
            self._check.toggle()


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
        *,
        targets: tuple[tuple[UUID, Meal], ...] | None = None,
        all_histories: Mapping[tuple[UUID, Meal], StatusHistory] | None = None,
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
        self._seed_targets = (
            tuple(dict.fromkeys(targets)) if targets is not None else None
        )
        self._all_histories = dict(all_histories or {})
        self._all_histories.update(
            {
                (person.person_id, meal): history
                for meal, history in history_by_meal.items()
            }
        )
        self._meal_checks: dict[Meal, QCheckBox] = {}
        self._meal_cards: dict[Meal, QFrame] = {}
        self._current_labels: dict[Meal, QLabel] = {}
        self._status_buttons: dict[MealStatus, QPushButton] = {}
        self._starts_on_by_meal: dict[Meal, WheelSafeDateEdit] = {}
        self._ends_on_by_meal: dict[Meal, WheelSafeDateEdit] = {}
        self._continue_by_meal: dict[Meal, QCheckBox] = {}
        self._last_status: MealStatus | None = None
        register_application_fonts()
        self.setObjectName("mealStatusChangeDialog")
        self.setWindowTitle(
            "선택한 식종 일괄 변경"
            if self._seed_targets is not None
            else f"{person.profile.name} · 배송 상태 변경"
        )
        self.resize(730, 760)
        self.setMinimumSize(640, 700)
        self.setStyleSheet(application_stylesheet())
        self._build_ui(anchor_day)
        for meal in (
            {m for _, m in self._seed_targets} if self._seed_targets else {initial_meal}
        ):
            self._meal_checks[meal].setChecked(True)
        self._status_buttons[initial_status].setChecked(True)
        for check in self._continue_by_meal.values():
            check.setChecked(initial_status is not MealStatus.PAUSED)
        # Compatibility for older dialog adapters that inspect one common period.
        self._starts_on = self._starts_on_by_meal[initial_meal]
        self._ends_on = self._ends_on_by_meal[initial_meal]
        self._continue = self._continue_by_meal[initial_meal]
        self._last_status = initial_status
        self._refresh_state()

    def selected_meals(self) -> tuple[Meal, ...]:
        return tuple(meal for meal in Meal if self._meal_checks[meal].isChecked())

    def selected_status(self) -> MealStatus:
        for status in MealStatus:
            if self._status_buttons[status].isChecked():
                return status
        raise ValueError("변경할 상태를 선택해야 합니다.")

    def selected_period(self) -> InclusiveDateRange:
        periods = self.selected_periods()
        return periods[next(meal for meal in Meal if meal in periods)]

    def selected_periods(self) -> dict[Meal, InclusiveDateRange]:
        status = self.selected_status()
        periods = {}
        for meal in self.selected_meals():
            starts_on = _python_date(self._starts_on_by_meal[meal])
            ends_on = (
                None
                if self._continue_by_meal[meal].isChecked()
                else _python_date(self._ends_on_by_meal[meal])
            )
            if status is MealStatus.PAUSED and ends_on is None:
                raise ValueError(f"{meal.value} 일시중지는 종료일이 필요합니다.")
            periods[meal] = InclusiveDateRange(starts_on, ends_on)
        return periods

    def selected_intake_channel(self) -> IntakeChannel | None:
        value = self._intake_channel.currentData()
        return None if value is None else IntakeChannel(value)

    def selected_note(self) -> str:
        return self._note.toPlainText().strip()

    def _build_ui(self, anchor_day: date) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(8)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget()
        root = QVBoxLayout(body)
        root.setContentsMargins(6, 6, 6, 4)
        root.setSpacing(11)

        header = QFrame()
        header.setObjectName("statusChangeHeader")
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(16, 14, 16, 14)
        header_layout.setSpacing(3)
        eyebrow = QLabel("배송 상태 · 기간 변경")
        eyebrow.setObjectName("editEyebrow")
        header_layout.addWidget(eyebrow)
        title = QLabel(
            "선택한 식종 일괄 변경"
            if self._seed_targets is not None
            else f"{self._person.profile.name} 배송 상태 변경"
        )
        title.setObjectName("workspaceTitle")
        header_layout.addWidget(title)
        guide = QLabel(
            "여러 식종에 같은 상태를 적용하면서 시작일·종료일은 식종별로 입력합니다."
        )
        guide.setObjectName("workspaceSubtitle")
        guide.setWordWrap(True)
        header_layout.addWidget(guide)
        root.addWidget(header)

        self._same_household = QCheckBox("같은 가구에도 일괄 적용 · 변경 시작일 기준")
        self._same_household.setObjectName("sameHouseholdCheck")
        self._same_household.toggled.connect(self._refresh_state)
        root.addWidget(self._same_household)
        scope_note = QLabel(
            "일시중지·중지 일괄 적용 시 이용하지 않는 식종은 제외합니다. 배송은 식종 이용을 함께 시작합니다."
        )
        scope_note.setWordWrap(True)
        scope_note.setObjectName("muted")
        root.addWidget(scope_note)
        root.addWidget(_section_label("적용할 식종 · 여러 개 선택 가능"))
        meal_row = QHBoxLayout()
        meal_row.setSpacing(8)
        for meal in (Meal.DINNER, Meal.BREAKFAST, Meal.LUNCH):
            card = _MealChoiceCard()
            card.setObjectName("mealChoiceCard")
            card.setProperty("selected", False)
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(12, 10, 12, 10)
            check = QCheckBox(meal.value)
            check.setObjectName("mealChoiceCheck")
            if self._seed_targets is not None:
                card.setEnabled(any(m is meal for _, m in self._seed_targets))
            check.toggled.connect(self._refresh_state)
            current = QLabel()
            current.setObjectName("mealCurrentState")
            current.setWordWrap(True)
            card.set_check(check, current)
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
            (MealStatus.DELIVERY, "정상 배송·처음 이용하는 식종도 시작"),
            (MealStatus.PAUSED, "정해진 기간만 잠시 중단"),
            (MealStatus.STOPPED, "장기 또는 무기한 중지"),
        ):
            button = QPushButton(f"{_status_text(status)}\n{hint}")
            button.setObjectName("statusChoice")
            button.setCheckable(True)
            button.setProperty("statusKind", status.name.lower())
            button.clicked.connect(
                lambda _checked=False, selected=status: self._on_status_clicked(selected)
            )
            status_group.addButton(button)
            status_row.addWidget(button, 1)
            self._status_buttons[status] = button
        root.addLayout(status_row)

        intake_row = QHBoxLayout()
        intake_row.addWidget(_field_label("접수 경로"))
        self._intake_channel = QComboBox()
        self._intake_channel.setObjectName("intakeChannel")
        self._intake_channel.addItem("-", None)
        for channel in IntakeChannel:
            self._intake_channel.addItem(channel.value, channel)
        intake_row.addWidget(self._intake_channel, 1)
        root.addLayout(intake_row)

        period_frame = QFrame()
        period_frame.setObjectName("editSection")
        period_layout = QVBoxLayout(period_frame)
        period_layout.setContentsMargins(14, 12, 14, 13)
        period_layout.addWidget(_section_label("기간"))
        period_grid = QGridLayout()
        period_grid.setHorizontalSpacing(12)
        period_grid.addWidget(_field_label("식종"), 0, 0)
        period_grid.addWidget(_field_label("시작일"), 0, 1)
        period_grid.addWidget(_field_label("종료일"), 0, 2)
        period_grid.addWidget(_field_label("기간"), 0, 3)
        for row, meal in enumerate((Meal.DINNER, Meal.BREAKFAST, Meal.LUNCH), 1):
            default_day = next_actual_delivery_date(
                self._roster.delivery_defaults, meal, anchor_day
            )
            weekdays = actual_delivery_weekdays(self._roster.delivery_defaults, meal)
            starts_on = _date_edit(default_day, weekdays)
            ends_on = _date_edit(default_day, weekdays)
            starts_on.setObjectName(f"statusStartsOn_{meal.name}")
            ends_on.setObjectName(f"statusEndsOn_{meal.name}")
            starts_on.dateChanged.connect(self._refresh_state)
            ends_on.dateChanged.connect(self._refresh_state)
            no_end = QCheckBox("종료일 없음")
            no_end.toggled.connect(
                lambda checked, selected=meal: self._on_continue_toggled(
                    selected, checked
                )
            )
            label = QLabel(
                f"{meal.value}\n실제 배송: "
                + _compact_weekday_text(
                    actual_delivery_weekdays(self._roster.delivery_defaults, meal)
                )
            )
            label.setObjectName("muted")
            label.setWordWrap(True)
            label.setMaximumWidth(170)
            period_grid.addWidget(label, row, 0)
            period_grid.addWidget(starts_on, row, 1)
            period_grid.addWidget(ends_on, row, 2)
            period_grid.addWidget(no_end, row, 3)
            self._starts_on_by_meal[meal] = starts_on
            self._ends_on_by_meal[meal] = ends_on
            self._continue_by_meal[meal] = no_end
        period_layout.addLayout(period_grid)
        root.addWidget(period_frame)

        root.addWidget(_field_label("비고"))
        self._note = QPlainTextEdit()
        self._note.setObjectName("mealStatusNote")
        self._note.setAccessibleName("식종 변경 비고")
        self._note.setPlaceholderText("필요한 내용을 입력해주세요. 선택한 대상·식종의 변경 이력에 함께 저장됩니다.")
        self._note.setFixedHeight(72)
        root.addWidget(self._note)

        summary = QFrame()
        summary.setObjectName("changeSummary")
        summary_layout = QVBoxLayout(summary)
        summary_layout.setContentsMargins(14, 11, 14, 11)
        summary_layout.addWidget(_section_label("변경 결과"))
        self._summary_text = QLabel()
        self._summary_text.setObjectName("summaryText")
        self._summary_text.setWordWrap(True)
        summary_scroll = QScrollArea()
        summary_scroll.setWidgetResizable(True)
        summary_scroll.setMinimumHeight(75)
        summary_scroll.setMaximumHeight(160)
        summary_scroll.setWidget(self._summary_text)
        summary_layout.addWidget(summary_scroll)
        root.addWidget(summary)

        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

        self._error = QLabel()
        self._error.setObjectName("errorMessage")
        self._error.setWordWrap(True)
        outer.addWidget(self._error)

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
        outer.addLayout(actions)

    def _on_continue_toggled(self, meal: Meal, checked: bool) -> None:
        self._ends_on_by_meal[meal].setEnabled(
            self._meal_checks[meal].isChecked() and not checked
        )
        self._refresh_state()

    def _on_status_clicked(self, status: MealStatus) -> None:
        if status is not self._last_status and status is MealStatus.STOPPED:
            for meal in Meal:
                no_end = self._continue_by_meal[meal]
                no_end.blockSignals(True)
                no_end.setChecked(True)
                no_end.blockSignals(False)
                self._ends_on_by_meal[meal].setEnabled(False)
        self._last_status = status
        self._refresh_state()

    def _refresh_state(self, *_args) -> None:
        for meal, card in self._meal_cards.items():
            card.setProperty("selected", self._meal_checks[meal].isChecked())
            card.style().unpolish(card)
            card.style().polish(card)

        status = self._checked_status()
        for meal in Meal:
            selected = self._meal_checks[meal].isChecked()
            no_end = self._continue_by_meal[meal]
            if status is MealStatus.PAUSED and selected and no_end.isChecked():
                no_end.blockSignals(True)
                no_end.setChecked(False)
                no_end.blockSignals(False)
            self._starts_on_by_meal[meal].setEnabled(selected)
            no_end.setEnabled(selected and status is not MealStatus.PAUSED)
            self._ends_on_by_meal[meal].setEnabled(
                selected and not no_end.isChecked()
            )

        for meal, label in self._current_labels.items():
            if self._seed_targets is not None:
                count = sum(selected is meal for _, selected in self._seed_targets)
                label.setText(f"명단에서 선택한 셀 {count}개")
            else:
                label.setText(
                    f"현재  {_current_state(self._roster, self._histories, self._person, meal, _python_date(self._starts_on_by_meal[meal]))}"
                )

        error = self._validation_error()
        self._error.setText(error or "")
        self._error.setVisible(error is not None)
        self._save_button.setEnabled(error is None)
        self._summary_text.setText(self._summary())
        count = len(self.selected_targets())
        self._save_button.setText(f"{count}개 변경 저장")

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
            periods = self.selected_periods()
        except ValueError as error:
            return str(error)
        for meal, period in periods.items():
            try:
                validate_actual_delivery_date(
                    self._roster.delivery_defaults,
                    meal,
                    period.starts_on,
                    field_name="시작일",
                )
                if period.ends_on is not None:
                    validate_actual_delivery_date(
                        self._roster.delivery_defaults,
                        meal,
                        period.ends_on,
                        field_name="종료일",
                    )
            except (TypeError, ValueError) as error:
                return str(error)
        try:
            validate_meal_status_transition(self._roster, self._all_histories,
                self.selected_targets(), status, periods)
        except ValueError as error:
            return str(error)
        if status is MealStatus.DELIVERY:
            return None if self.selected_targets() else "적용할 항목을 선택해주세요."
        targets = self.selected_targets()
        if not targets:
            return "적용할 항목이 없습니다. 선택한 식종과 시작일을 확인해주세요."
        invalid = [
            (pid, meal)
            for pid, meal in targets
            if meal not in self._roster.meals_for(
                pid, on=periods[meal].starts_on
            )
        ]
        if invalid:
            labels = " · ".join(
                f"{self._roster.person(pid).profile.name} {meal.value}"
                for pid, meal in invalid
            )
            return f"{labels}은 시작일에 이용 중인 식종이 아닙니다. 먼저 해당 식종을 배송 상태로 시작해주세요."
        return None

    def _summary(self) -> str:
        meals = self.selected_meals()
        status = self._checked_status()
        if not meals or status is None:
            return "식종과 상태를 선택하면 변경 결과가 표시됩니다."
        try:
            periods = self.selected_periods()
        except ValueError:
            return "기간을 확인해주세요."
        targets = self.selected_targets()
        requested = self._requested_targets()
        lines = [
            f"{len({pid for pid, _ in targets})}명 · 선택한 식종 {len(meals)}개 · 총 {len(targets)}개 적용"
        ]
        for person_id, meal in requested:
            person = self._roster.person(person_id)
            history = self._all_histories.get((person_id, meal))
            current = _current_state(
                self._roster,
                {meal: history} if history else {},
                person,
                meal,
                periods[meal].starts_on,
            )
            if (person_id, meal) not in targets:
                lines.append(
                    f"{person.profile.name} · {meal.value} · 이용 중이 아니어서 제외"
                )
                continue
            after = (
                "식종 추가 · 배송"
                if status is MealStatus.DELIVERY and current == "-"
                else _status_text(status)
            )
            lines.append(
                f"{person.profile.name} · {meal.value}   {current} → {after}   ·   {_period_text(periods[meal])}"
            )
        return "\n".join(lines)

    def _requested_targets(self) -> tuple[tuple[UUID, Meal], ...]:
        meals = self.selected_meals()
        seeds = (
            tuple((pid, meal) for pid, meal in self._seed_targets if meal in meals)
            if self._seed_targets is not None
            else tuple((self._person.person_id, meal) for meal in meals)
        )
        targets = dict.fromkeys(seeds)
        if self._same_household.isChecked():
            for person_id, meal in seeds:
                on = _python_date(self._starts_on_by_meal[meal])
                household_ids = {
                    m.household_id
                    for m in self._roster.memberships
                    if m.person_id == person_id and m.period.contains(on)
                }
                for household_id in household_ids:
                    for member in self._roster.household_members(household_id, on=on):
                        targets[(member.person_id, meal)] = None
        return tuple(targets)

    def selected_targets(self) -> tuple[tuple[UUID, Meal], ...]:
        targets = self._requested_targets()
        bulk = self._same_household.isChecked() or self._seed_targets is not None
        if bulk and self._checked_status() in (MealStatus.PAUSED, MealStatus.STOPPED):
            return tuple(
                (pid, meal)
                for pid, meal in targets
                if meal
                in self._roster.meals_for(
                    pid, on=_python_date(self._starts_on_by_meal[meal])
                )
            )
        return targets

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

    @dialog_workflow
    def run_batch(
        self, targets, initial_status, anchor_day, parent=None
    ) -> MealStatusChangeOutcome:
        targets = tuple(dict.fromkeys(targets))
        if not targets:
            return MealStatusChangeOutcome(MealStatusChangeStatus.CANCELLED)
        person_id, meal = targets[0]
        return (yield from invoke(self.run,
            person_id, meal, initial_status, anchor_day, parent, targets=targets
        ))

    @dialog_workflow
    def run(
        self,
        person_id: UUID,
        initial_meal: Meal,
        initial_status: MealStatus,
        anchor_day: date,
        parent: QWidget | None = None,
        *,
        targets: tuple[tuple[UUID, Meal], ...] | None = None,
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
        seed_ids = {pid for pid, _ in targets} if targets else {person_id}
        households = {
            m.household_id for m in roster.memberships if m.person_id in seed_ids
        }
        related_ids = seed_ids | {
            m.person_id for m in roster.memberships if m.household_id in households
        }
        all_histories = {
            (pid, meal): self._session.database.status_history(pid, meal)
            for pid in related_ids
            for meal in Meal
        }
        histories = {meal: all_histories[(person_id, meal)] for meal in Meal}
        kwargs = {}
        if self._dialog_type is MealStatusChangeDialog or targets is not None:
            kwargs = {"targets": targets, "all_histories": all_histories}
        dialog = self._dialog_type(
            person,
            roster,
            histories,
            anchor_day,
            initial_meal,
            initial_status,
            parent,
            **kwargs,
        )
        if (yield DialogRequest(dialog, self._session)) != QDialog.DialogCode.Accepted:
            return MealStatusChangeOutcome(MealStatusChangeStatus.CANCELLED)
        try:
            meals = dialog.selected_meals()
            periods_getter = getattr(dialog, "selected_periods", None)
            common_period = dialog.selected_period()
            periods = (
                periods_getter()
                if callable(periods_getter)
                else {meal: common_period for meal in meals}
            )
            intake_getter = getattr(dialog, "selected_intake_channel", None)
            intake_channel = intake_getter() if callable(intake_getter) else None
            note_getter = getattr(dialog, "selected_note", None)
            note = note_getter() if callable(note_getter) else ""
            target_getter = getattr(dialog, "selected_targets", None)
            selected = target_getter() if callable(target_getter) else ()
            is_batch = targets is not None or any(
                selected_person_id != person_id
                for selected_person_id, _meal in selected
            )
            changed_targets = tuple(selected) if is_batch else tuple((person_id, meal) for meal in meals)
            validate_meal_status_transition(roster, all_histories, changed_targets,
                dialog.selected_status(), periods)
            if is_batch:
                used_meals = {meal for _person_id, meal in selected}
                prepared = build_batch_meal_status_revision(
                    roster,
                    selected,
                    dialog.selected_status(),
                    common_period,
                    actor=self._actor_provider(),
                    recorded_at=self._recorded_at_provider(),
                    revision_id=self._revision_id_factory(),
                    event_ids={target: self._event_id_factory() for target in selected},
                    periods_by_meal={
                        meal: period
                        for meal, period in periods.items()
                        if meal in used_meals
                    },
                    intake_channel=intake_channel,
                    note=note,
                )
            else:
                prepared = build_meal_status_revision(
                    roster,
                    person_id,
                    meals,
                    dialog.selected_status(),
                    common_period,
                    actor=self._actor_provider(),
                    recorded_at=self._recorded_at_provider(),
                    revision_id=self._revision_id_factory(),
                    event_ids={meal: self._event_id_factory() for meal in meals},
                    periods_by_meal=periods,
                    intake_channel=intake_channel,
                    note=note,
                )
            backup_result = save_revision(self._session, prepared.record, parent=parent)
        except Exception as error:  # noqa: BLE001 - report persistence failures at the UI boundary
            self._message_box.critical(
                parent,
                "배송 상태를 저장하지 못했습니다",
                "저장된 명단에는 반영하지 않았습니다. "
                "내용을 확인한 뒤 다시 시도해주세요.\n\n" + str(error),
            )
            return MealStatusChangeOutcome(MealStatusChangeStatus.FAILED)
        if backup_result.error is not None:
            self._message_box.warning(
                parent,
                "배송 상태는 저장됐지만 자동 백업에 실패했습니다",
                "변경 내용과 이력은 운영 데이터에 정상 저장되었습니다. "
                "백업 폴더 연결을 확인한 뒤 백업을 다시 실행해주세요.\n\n"
                + str(backup_result.error),
            )
        return MealStatusChangeOutcome(
            MealStatusChangeStatus.COMPLETED,
            committed_roster_after_save(self._session, prepared.snapshot),
            backup_result,
        )


def _status_text(status: MealStatus) -> str:
    return "중지" if status is MealStatus.STOPPED else status.value


def _compact_weekday_text(weekdays: tuple[int, ...]) -> str:
    text = weekday_text(weekdays)
    return text if text == "없음" else text.replace("요일", "") + "요일"


def _current_state(
    roster: RosterSnapshot,
    histories: Mapping[Meal, StatusHistory],
    person: Person,
    meal: Meal,
    on: date,
) -> str:
    if meal not in roster.meals_for(person.person_id, on=on):
        return "-"
    history = histories.get(meal)
    return (
        MealStatus.DELIVERY.value
        if history is None
        else _status_text(history.status_on(on))
    )


def _date_edit(value: date, weekdays: tuple[int, ...]) -> WheelSafeDateEdit:
    editor = DeliveryDateEdit(QDate(value.year, value.month, value.day), weekdays)
    editor.setDisplayFormat("yyyy.MM.dd")
    return editor


def _python_date(editor: WheelSafeDateEdit) -> date:
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
