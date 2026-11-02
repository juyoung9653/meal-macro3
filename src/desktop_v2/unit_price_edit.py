"""Effective-dated multi-meal unit price editor and persistence controller."""

from __future__ import annotations

from .user_messages import user_error

from .dialog_workflow import DialogRequest, dialog_workflow

from .startup_loading import save_revision

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from PySide6.QtCore import QDate
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.domain.unit_pricing import (
    DEFAULT_MEAL_UNIT_PRICE,
    UNIT_PRICE_SCHEDULE_ID,
    UNIT_PRICE_SCHEDULE_KIND,
    UnitPriceSchedule,
    build_unit_price_revision,
    unit_price_schedule_from_state,
)
from src.domain.roster import (
    Meal,
    RosterSnapshot,
)
from src.domain.revisions import RevisionActor
from src.storage.database_session import (
    AutomaticBackupResult,
    DatabaseSession,
)

from .date_edit import WheelSafeDateEdit
from .import_controller import current_windows_actor
from .theme import application_stylesheet, register_application_fonts


class UnitPriceChangeDialog(QDialog):
    """Show the current rate card and record one multi-meal price change."""

    def __init__(
        self,
        schedule: UnitPriceSchedule,
        anchor_day: date,
        parent: QWidget | None = None,
    ) -> None:
        if not isinstance(schedule, UnitPriceSchedule):
            raise TypeError("schedule은 UnitPriceSchedule이어야 합니다.")
        if isinstance(anchor_day, datetime) or not isinstance(anchor_day, date):
            raise TypeError("anchor day는 date여야 합니다.")
        super().__init__(parent)
        self._schedule = schedule
        self._meal_checks: dict[Meal, QCheckBox] = {}
        self._meal_cards: dict[Meal, QFrame] = {}
        self._current_labels: dict[Meal, QLabel] = {}
        register_application_fonts()
        self.setObjectName("unitPriceChangeDialog")
        self.setWindowTitle("청구 단가 관리")
        self.resize(720, 720)
        self.setMinimumSize(660, 660)
        self.setStyleSheet(application_stylesheet())
        self._build_ui(anchor_day)
        self._refresh_state()

    def selected_meals(self) -> tuple[Meal, ...]:
        return tuple(
            meal for meal in Meal if self._meal_checks[meal].isChecked()
        )

    def selected_unit_price(self) -> int:
        return self._price.value()

    def selected_effective_on(self) -> date:
        value = self._effective_on.date()
        return date(value.year(), value.month(), value.day())

    def _build_ui(self, anchor_day: date) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 18, 18, 16)
        root.setSpacing(11)

        header = QFrame()
        header.setObjectName("unitPriceHeader")
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(16, 14, 16, 14)
        header_layout.setSpacing(3)
        eyebrow = QLabel("청구 기준 · 적용일 이력")
        eyebrow.setObjectName("editEyebrow")
        header_layout.addWidget(eyebrow)
        title = QLabel("식종별 단가 관리")
        title.setObjectName("workspaceTitle")
        header_layout.addWidget(title)
        guide = QLabel(
            "기본 단가는 모두 10,000원입니다. 여러 식종을 고르면 같은 단가와 적용일이 한 번에 저장됩니다."
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
            card_layout.addWidget(check)
            card_layout.addWidget(current)
            meal_row.addWidget(card, 1)
            self._meal_checks[meal] = check
            self._meal_cards[meal] = card
            self._current_labels[meal] = current
        root.addLayout(meal_row)

        change_frame = QFrame()
        change_frame.setObjectName("editSection")
        change_layout = QGridLayout(change_frame)
        change_layout.setContentsMargins(14, 12, 14, 13)
        change_layout.setHorizontalSpacing(16)
        change_layout.setVerticalSpacing(7)
        change_layout.addWidget(_field_label("새 단가"), 0, 0)
        change_layout.addWidget(_field_label("적용일"), 0, 1)
        self._price = QSpinBox()
        self._price.setObjectName("unitPriceInput")
        self._price.setRange(0, 10_000_000)
        self._price.setSingleStep(500)
        self._price.setGroupSeparatorShown(True)
        self._price.setSuffix(" 원")
        self._price.setValue(DEFAULT_MEAL_UNIT_PRICE)
        self._price.valueChanged.connect(self._refresh_state)
        self._effective_on = WheelSafeDateEdit(
            QDate(anchor_day.year, anchor_day.month, anchor_day.day)
        )
        self._effective_on.setObjectName("unitPriceDate")
        self._effective_on.setCalendarPopup(True)
        self._effective_on.setDisplayFormat("yyyy.MM.dd")
        self._effective_on.dateChanged.connect(self._refresh_state)
        change_layout.addWidget(self._price, 1, 0)
        change_layout.addWidget(self._effective_on, 1, 1)
        root.addWidget(change_frame)

        summary = QFrame()
        summary.setObjectName("priceChangeSummary")
        summary_layout = QHBoxLayout(summary)
        summary_layout.setContentsMargins(0, 0, 0, 0)
        stamp = QFrame()
        stamp.setObjectName("effectiveDateStamp")
        stamp_layout = QVBoxLayout(stamp)
        stamp_layout.setContentsMargins(12, 10, 12, 10)
        stamp_label = QLabel("적용일")
        stamp_label.setObjectName("priceStampLabel")
        self._stamp_date = QLabel()
        self._stamp_date.setObjectName("priceStampDate")
        stamp_layout.addWidget(stamp_label)
        stamp_layout.addWidget(self._stamp_date)
        summary_layout.addWidget(stamp)
        summary_text_layout = QVBoxLayout()
        summary_text_layout.setContentsMargins(13, 10, 13, 10)
        summary_text_layout.addWidget(_section_label("변경 결과"))
        self._summary_text = QLabel()
        self._summary_text.setObjectName("summaryText")
        self._summary_text.setWordWrap(True)
        summary_text_layout.addWidget(self._summary_text)
        summary_layout.addLayout(summary_text_layout, 1)
        root.addWidget(summary)

        self._error = QLabel()
        self._error.setObjectName("errorMessage")
        self._error.setWordWrap(True)
        root.addWidget(self._error)

        history_label = _section_label("최근 단가 변경")
        root.addWidget(history_label)
        self._history = QTableWidget()
        self._history.setObjectName("unitPriceHistory")
        self._history.setColumnCount(4)
        self._history.setHorizontalHeaderLabels(("적용일", "식종", "단가", "기록일"))
        self._history.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._history.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self._history.verticalHeader().setVisible(False)
        self._history.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self._fill_history()
        root.addWidget(self._history, 1)

        actions = QHBoxLayout()
        actions.addStretch(1)
        cancel = QPushButton("닫기")
        cancel.setObjectName("quietAction")
        cancel.clicked.connect(self.reject)
        actions.addWidget(cancel)
        self._save_button = QPushButton("단가 변경 저장")
        self._save_button.setObjectName("primaryAction")
        self._save_button.clicked.connect(self._save)
        actions.addWidget(self._save_button)
        root.addLayout(actions)

    def _fill_history(self) -> None:
        changes = tuple(reversed(self._schedule.changes[-8:]))
        self._history.setRowCount(max(1, len(changes)))
        if not changes:
            empty = QTableWidgetItem("아직 저장한 단가 변경이 없습니다")
            self._history.setItem(0, 0, empty)
            self._history.setSpan(0, 0, 1, 4)
            return
        for row, change in enumerate(changes):
            values = (
                change.effective_on.strftime("%Y.%m.%d"),
                " · ".join(meal.value for meal in change.meals),
                f"{change.unit_price:,}원",
                change.recorded_at.astimezone().strftime("%Y.%m.%d"),
            )
            for column, value in enumerate(values):
                self._history.setItem(row, column, QTableWidgetItem(value))

    def _refresh_state(self, *_args) -> None:
        effective_on = self.selected_effective_on()
        for meal, card in self._meal_cards.items():
            selected = self._meal_checks[meal].isChecked()
            card.setProperty("selected", selected)
            card.style().unpolish(card)
            card.style().polish(card)
            self._current_labels[meal].setText(
                f"적용일 현재  {self._schedule.price_for(meal, effective_on):,}원"
            )
        self._stamp_date.setText(effective_on.strftime("%Y\n%m.%d"))
        error = self._validation_error()
        show_error = error is not None and bool(self.selected_meals())
        self._error.setText(error if show_error else "")
        self._error.setVisible(show_error)
        self._save_button.setEnabled(error is None)
        self._summary_text.setText(self._summary())

    def _validation_error(self) -> str | None:
        meals = self.selected_meals()
        if not meals:
            return "단가를 변경할 식종을 한 개 이상 선택해주세요."
        effective_on = self.selected_effective_on()
        unit_price = self.selected_unit_price()
        if all(
            self._schedule.price_for(meal, effective_on) == unit_price
            for meal in meals
        ):
            return "선택한 식종은 적용일에 이미 같은 단가입니다."
        return None

    def _summary(self) -> str:
        meals = self.selected_meals()
        if not meals:
            return "식종을 선택하면 현재 단가와 변경 후 단가가 표시됩니다."
        effective_on = self.selected_effective_on()
        unit_price = self.selected_unit_price()
        return "\n".join(
            f"{meal.value}   {self._schedule.price_for(meal, effective_on):,}원 → {unit_price:,}원"
            for meal in meals
        )

    def _save(self) -> None:
        error = self._validation_error()
        if error is not None:
            self._error.setText(error)
            self._error.show()
            return
        self.accept()


class UnitPriceChangeStatus(StrEnum):
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class UnitPriceChangeOutcome:
    status: UnitPriceChangeStatus
    roster: RosterSnapshot | None = None
    schedule: UnitPriceSchedule | None = None
    backup_result: AutomaticBackupResult | None = None

    @property
    def completed(self) -> bool:
        return self.status is UnitPriceChangeStatus.COMPLETED


class UnitPriceChangeController:
    """Commit an accepted price change and attempt the normal automatic backup."""

    def __init__(
        self,
        session: DatabaseSession,
        *,
        actor_provider: Callable[[], RevisionActor] | None = None,
        dialog_type=UnitPriceChangeDialog,
        message_box=QMessageBox,
        today_provider: Callable[[], date] | None = None,
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
        self._today_provider = today_provider or date.today
        self._recorded_at_provider = recorded_at_provider or _utc_now
        self._revision_id_factory = revision_id_factory
        self._change_id_factory = change_id_factory

    @dialog_workflow
    def run(self, parent: QWidget | None = None) -> UnitPriceChangeOutcome:
        roster = self._session.database.current_roster()
        if roster is None:
            self._message_box.critical(
                parent,
                "단가를 변경하지 못했습니다",
                "현재 명단이 없습니다. 기존 명단 불러오기를 먼저 완료해주세요.",
            )
            return UnitPriceChangeOutcome(UnitPriceChangeStatus.FAILED)
        source = self._session.database.snapshot(roster.revision_id)
        stored_state = source.entity(
            UNIT_PRICE_SCHEDULE_KIND,
            UNIT_PRICE_SCHEDULE_ID,
        )
        try:
            schedule = unit_price_schedule_from_state(stored_state)
        except Exception as error:
            self._message_box.critical(
                parent,
                "단가 이력을 읽지 못했습니다",
                user_error(error),
            )
            return UnitPriceChangeOutcome(UnitPriceChangeStatus.FAILED)
        dialog = self._dialog_type(schedule, self._today_provider(), parent)
        if (yield DialogRequest(dialog, self._session)) != QDialog.DialogCode.Accepted:
            return UnitPriceChangeOutcome(UnitPriceChangeStatus.CANCELLED)
        try:
            prepared = build_unit_price_revision(
                roster.revision_id,
                stored_state,
                dialog.selected_meals(),
                dialog.selected_unit_price(),
                dialog.selected_effective_on(),
                actor=self._actor_provider(),
                recorded_at=self._recorded_at_provider(),
                revision_id=self._revision_id_factory(),
                change_id=self._change_id_factory(),
            )
            backup_result = save_revision(self._session, prepared.record, parent=parent)
            updated_roster = self._session.database.current_roster()
        except Exception as error:
            self._message_box.critical(
                parent,
                "단가를 저장하지 못했습니다",
                "단가와 변경 이력은 저장하지 않았습니다. 내용을 확인한 뒤 다시 시도해주세요.\n\n"
                + user_error(error),
            )
            return UnitPriceChangeOutcome(UnitPriceChangeStatus.FAILED)
        if updated_roster is None:
            self._message_box.critical(
                parent,
                "저장한 단가를 표시하지 못했습니다",
                "단가는 저장됐지만 현재 명단을 다시 읽지 못했습니다. 프로그램을 다시 열어주세요.",
            )
            return UnitPriceChangeOutcome(UnitPriceChangeStatus.FAILED)
        if backup_result.warning_required:
            self._message_box.warning(
                parent,
                "단가는 저장됐지만 자동 백업에 실패했습니다",
                "단가와 이력은 운영 데이터에 정상 저장되었습니다. 백업 폴더 연결을 확인한 뒤 백업을 다시 실행해주세요.\n\n"
                + user_error(backup_result.error),
            )
        return UnitPriceChangeOutcome(
            UnitPriceChangeStatus.COMPLETED,
            updated_roster,
            prepared.schedule,
            backup_result,
        )


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
