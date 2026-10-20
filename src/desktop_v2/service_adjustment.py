"""Vendor closure and replacement-meal editor with revision persistence."""

from __future__ import annotations

from .dialog_workflow import DialogRequest, dialog_workflow

from .startup_loading import save_revision

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from uuid import UUID, uuid4

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.domain.service_adjustments import (
    SERVICE_ADJUSTMENT_SCHEDULE_ID,
    SERVICE_ADJUSTMENT_SCHEDULE_KIND,
    ReplacementDeliveryOverride,
    ServiceAdjustmentSchedule,
    ServiceTreatment,
    build_public_holiday_review_revision,
    build_service_adjustment_revision,
    previous_common_operating_day,
    service_adjustment_schedule_from_state,
)
from src.domain.roster import (
    InclusiveDateRange,
    Meal,
    RosterSnapshot,
)
from src.domain.public_holidays import (
    PublicHolidayCandidate,
    south_korea_public_holiday_candidates,
)
from src.domain.revisions import RevisionActor
from src.services.meal_service_availability import default_service_weekdays
from src.storage.database_session import (
    AutomaticBackupResult,
    DatabaseSession,
)

from .date_edit import WheelSafeDateEdit
from .import_controller import current_windows_actor
from .theme import application_stylesheet, register_application_fonts


_WEEKDAY_LABELS = ("월", "화", "수", "목", "금", "토", "일")


class ServiceAdjustmentAction(StrEnum):
    MANUAL = "manual"
    PUBLIC_HOLIDAY = "public_holiday"


@dataclass(frozen=True, slots=True)
class ServiceAdjustmentRequest:
    action: ServiceAdjustmentAction
    public_holidays: tuple[PublicHolidayCandidate, ...] = ()
    calendar_holidays: tuple[PublicHolidayCandidate, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.action, ServiceAdjustmentAction):
            raise TypeError("휴무·공휴일 작업 종류가 올바르지 않습니다.")
        selected = tuple(self.public_holidays)
        calendar = tuple(self.calendar_holidays)
        if any(
            not isinstance(candidate, PublicHolidayCandidate)
            for candidate in (*selected, *calendar)
        ):
            raise TypeError("공휴일 후보가 올바르지 않습니다.")
        if len({candidate.day for candidate in selected}) != len(selected):
            raise ValueError("같은 공휴일을 중복 선택할 수 없습니다.")
        if len({candidate.day for candidate in calendar}) != len(calendar):
            raise ValueError("공휴일 달력에 같은 날짜가 중복되었습니다.")
        if self.action is ServiceAdjustmentAction.MANUAL:
            if selected or calendar:
                raise ValueError("직접 휴무 추가에는 공휴일 후보가 없어야 합니다.")
        else:
            if not selected:
                raise ValueError("검토할 공휴일을 하나 이상 선택해야 합니다.")
            calendar_days = {candidate.day for candidate in calendar}
            if not calendar or any(
                candidate.day not in calendar_days for candidate in selected
            ):
                raise ValueError("선택한 공휴일이 표시된 달력에 없습니다.")
        object.__setattr__(
            self,
            "public_holidays",
            tuple(sorted(selected, key=lambda candidate: candidate.day)),
        )
        object.__setattr__(
            self,
            "calendar_holidays",
            tuple(sorted(calendar, key=lambda candidate: candidate.day)),
        )


class ServiceAdjustmentHubDialog(QDialog):
    """Choose N current-DB holiday candidates or a manual closure entry."""

    def __init__(
        self,
        schedule: ServiceAdjustmentSchedule,
        anchor_day: date,
        holiday_provider: Callable[
            [int], tuple[PublicHolidayCandidate, ...]
        ] = south_korea_public_holiday_candidates,
        parent: QWidget | None = None,
    ) -> None:
        if not isinstance(schedule, ServiceAdjustmentSchedule):
            raise TypeError("schedule은 ServiceAdjustmentSchedule이어야 합니다.")
        if isinstance(anchor_day, datetime) or not isinstance(anchor_day, date):
            raise TypeError("anchor day는 date여야 합니다.")
        if not callable(holiday_provider):
            raise TypeError("holiday provider는 callable이어야 합니다.")
        super().__init__(parent)
        self._schedule = schedule
        self._anchor_day = anchor_day
        self._holiday_provider = holiday_provider
        self._visible_candidates: tuple[PublicHolidayCandidate, ...] = ()
        self._request: ServiceAdjustmentRequest | None = None
        register_application_fonts()
        self.setObjectName("serviceAdjustmentHubDialog")
        self.setWindowTitle("휴무·공휴일 관리")
        self.resize(860, 650)
        self.setMinimumSize(720, 560)
        self.setStyleSheet(application_stylesheet())
        self._build_ui()
        self._load_candidates()

    def selected_request(self) -> ServiceAdjustmentRequest:
        if self._request is None:
            raise RuntimeError("선택한 휴무·공휴일 작업이 없습니다.")
        return self._request

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 18, 18, 16)
        root.setSpacing(10)

        header = QFrame()
        header.setObjectName("serviceAdjustmentHeader")
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(16, 14, 16, 14)
        header_layout.setSpacing(3)
        eyebrow = QLabel("현재 저장된 데이터 · 공휴일 후보와 업체 자체 휴무")
        eyebrow.setObjectName("editEyebrow")
        title = QLabel("휴무·공휴일 관리")
        title.setObjectName("workspaceTitle")
        guide = QLabel(
            "공휴일은 자동으로 후보만 표시됩니다. 선택한 뒤 정상 운영, "
            "미제공 또는 대체식으로 확정해야 일정과 청구에 반영됩니다."
        )
        guide.setObjectName("workspaceSubtitle")
        guide.setWordWrap(True)
        header_layout.addWidget(eyebrow)
        header_layout.addWidget(title)
        header_layout.addWidget(guide)
        root.addWidget(header)

        candidate_frame = QFrame()
        candidate_frame.setObjectName("holidayCandidatePanel")
        candidate_layout = QVBoxLayout(candidate_frame)
        candidate_layout.setContentsMargins(14, 12, 14, 12)
        candidate_layout.setSpacing(7)
        tools = QHBoxLayout()
        candidate_title = QLabel("검토할 공휴일")
        candidate_title.setObjectName("sectionLabel")
        tools.addWidget(candidate_title)
        tools.addStretch(1)
        year_label = QLabel("연도")
        year_label.setObjectName("fieldLabel")
        tools.addWidget(year_label)
        self._year = QSpinBox()
        self._year.setObjectName("holidayReviewYear")
        self._year.setRange(1948, 2100)
        self._year.setValue(self._anchor_day.year)
        self._year.setSuffix("년")
        self._year.valueChanged.connect(self._load_candidates)
        tools.addWidget(self._year)
        candidate_layout.addLayout(tools)

        notice = QLabel(
            "목록에 나타난 것만으로는 저장된 내용이 바뀌지 않습니다. "
            "인터넷 연결 없이 앱에 포함된 한국 공휴일 기준으로 표시합니다."
        )
        notice.setObjectName("holidayCandidateNotice")
        notice.setWordWrap(True)
        candidate_layout.addWidget(notice)

        self._candidate_summary = QLabel()
        self._candidate_summary.setObjectName("holidayCandidateSummary")
        candidate_layout.addWidget(self._candidate_summary)
        selection_tools = QHBoxLayout()
        self._select_unreviewed_button = QPushButton("미검토 전체 선택")
        self._select_unreviewed_button.setObjectName("quietAction")
        self._select_unreviewed_button.clicked.connect(
            self._select_all_unreviewed
        )
        selection_tools.addWidget(self._select_unreviewed_button)
        self._clear_selection_button = QPushButton("선택 해제")
        self._clear_selection_button.setObjectName("quietAction")
        self._clear_selection_button.clicked.connect(self._clear_selection)
        selection_tools.addWidget(self._clear_selection_button)
        selection_tools.addStretch(1)
        candidate_layout.addLayout(selection_tools)
        self._candidate_error = QLabel()
        self._candidate_error.setObjectName("errorMessage")
        self._candidate_error.setWordWrap(True)
        self._candidate_error.hide()
        candidate_layout.addWidget(self._candidate_error)

        self._candidates = QTableWidget()
        self._candidates.setObjectName("holidayCandidateTable")
        self._candidates.setColumnCount(4)
        self._candidates.setHorizontalHeaderLabels(
            ("선택", "날짜", "공휴일", "현재 저장된 데이터 검토 상태")
        )
        self._candidates.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self._candidates.setSelectionMode(
            QAbstractItemView.SelectionMode.NoSelection
        )
        self._candidates.verticalHeader().setVisible(False)
        header_view = self._candidates.horizontalHeader()
        header_view.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header_view.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self._candidates.itemChanged.connect(self._update_review_button)
        self._candidates.cellDoubleClicked.connect(
            self._candidate_double_clicked
        )
        candidate_layout.addWidget(self._candidates, 1)
        root.addWidget(candidate_frame, 1)

        actions = QHBoxLayout()
        self._manual_button = QPushButton("공휴일 아닌 휴무 직접 추가")
        self._manual_button.setObjectName("quietAction")
        self._manual_button.clicked.connect(self._select_manual)
        actions.addWidget(self._manual_button)
        actions.addStretch(1)
        close = QPushButton("닫기")
        close.setObjectName("quietAction")
        close.clicked.connect(self.reject)
        actions.addWidget(close)
        self._review_button = QPushButton("공휴일을 선택해주세요")
        self._review_button.setObjectName("primaryAction")
        self._review_button.clicked.connect(self._review_selected)
        self._review_button.setEnabled(False)
        actions.addWidget(self._review_button)
        root.addLayout(actions)

    def _load_candidates(self, *_args) -> None:
        self._candidate_error.hide()
        try:
            candidates = tuple(self._holiday_provider(self._year.value()))
            if any(
                not isinstance(candidate, PublicHolidayCandidate)
                for candidate in candidates
            ):
                raise TypeError("공휴일 제공 결과가 올바르지 않습니다.")
            self._visible_candidates = tuple(
                sorted(candidates, key=lambda candidate: candidate.day)
            )
        except Exception as error:
            self._visible_candidates = ()
            self._candidate_error.setText(
                "공휴일 후보를 불러오지 못했습니다. 직접 휴무 추가는 계속 "
                f"사용할 수 있습니다.\n\n{error}"
            )
            self._candidate_error.show()
        self._fill_candidate_table()

    def _fill_candidate_table(self) -> None:
        self._candidates.blockSignals(True)
        self._candidates.clearContents()
        self._candidates.setRowCount(len(self._visible_candidates))
        unreviewed = 0
        for row, candidate in enumerate(self._visible_candidates):
            status, missing = _holiday_review_text(self._schedule, candidate)
            unreviewed += int(missing > 0)
            check = QTableWidgetItem()
            check.setFlags(
                Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable
            )
            check.setCheckState(Qt.CheckState.Unchecked)
            check.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._candidates.setItem(row, 0, check)
            values = (
                f"{candidate.day:%m.%d} ({_WEEKDAY_LABELS[candidate.day.weekday()]})",
                candidate.name,
                status,
            )
            for column, value in enumerate(values, start=1):
                item = QTableWidgetItem(value)
                if column == 1:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if missing:
                    item.setData(Qt.ItemDataRole.UserRole, "needs_review")
                self._candidates.setItem(row, column, item)
        self._candidates.blockSignals(False)
        provider = (
            ""
            if not self._visible_candidates
            else f" · 기준 v{self._visible_candidates[0].provider_version}"
        )
        self._candidate_summary.setText(
            f"{self._year.value()}년 {len(self._visible_candidates)}일{provider} · "
            f"검토 필요 {unreviewed}일"
        )
        self._scroll_to_next_unreviewed()
        self._update_review_button()

    def _scroll_to_next_unreviewed(self) -> None:
        if not self._visible_candidates:
            return
        preferred: int | None = None
        for row, candidate in enumerate(self._visible_candidates):
            _, missing = _holiday_review_text(self._schedule, candidate)
            if missing and candidate.day >= self._anchor_day:
                preferred = row
                break
        if preferred is None:
            for row, candidate in enumerate(self._visible_candidates):
                _, missing = _holiday_review_text(self._schedule, candidate)
                if missing:
                    preferred = row
                    break
        if preferred is None:
            preferred = 0
        self._candidates.scrollToItem(self._candidates.item(preferred, 1))

    def _selected_candidates(self) -> tuple[PublicHolidayCandidate, ...]:
        return tuple(
            candidate
            for row, candidate in enumerate(self._visible_candidates)
            if self._candidates.item(row, 0).checkState()
            is Qt.CheckState.Checked
        )

    def _update_review_button(self, *_args) -> None:
        selected_count = len(self._selected_candidates())
        self._review_button.setEnabled(selected_count > 0)
        self._review_button.setText(
            f"선택한 공휴일 {selected_count}일 검토"
            if selected_count
            else "공휴일을 선택해주세요"
        )
        self._clear_selection_button.setEnabled(selected_count > 0)

    def _select_all_unreviewed(self) -> None:
        self._candidates.blockSignals(True)
        for row, candidate in enumerate(self._visible_candidates):
            _, missing = _holiday_review_text(self._schedule, candidate)
            self._candidates.item(row, 0).setCheckState(
                Qt.CheckState.Checked if missing else Qt.CheckState.Unchecked
            )
        self._candidates.blockSignals(False)
        self._update_review_button()

    def _clear_selection(self) -> None:
        self._candidates.blockSignals(True)
        for row in range(self._candidates.rowCount()):
            self._candidates.item(row, 0).setCheckState(Qt.CheckState.Unchecked)
        self._candidates.blockSignals(False)
        self._update_review_button()

    def _candidate_double_clicked(self, row: int, column: int) -> None:
        if column == 0 or not 0 <= row < self._candidates.rowCount():
            return
        item = self._candidates.item(row, 0)
        item.setCheckState(
            Qt.CheckState.Unchecked
            if item.checkState() is Qt.CheckState.Checked
            else Qt.CheckState.Checked
        )

    def _select_manual(self) -> None:
        self._request = ServiceAdjustmentRequest(ServiceAdjustmentAction.MANUAL)
        self.accept()

    def _review_selected(self) -> None:
        selected = self._selected_candidates()
        if not selected:
            return
        self._request = ServiceAdjustmentRequest(
            ServiceAdjustmentAction.PUBLIC_HOLIDAY,
            selected,
            self._visible_candidates,
        )
        self.accept()


class PublicHolidayBatchDialog(QDialog):
    """Apply one meal/treatment choice to N holiday candidates."""

    def __init__(
        self,
        schedule: ServiceAdjustmentSchedule,
        public_holidays: tuple[PublicHolidayCandidate, ...],
        calendar_holidays: tuple[PublicHolidayCandidate, ...],
        parent: QWidget | None = None,
        *,
        roster: RosterSnapshot | None = None,
    ) -> None:
        if not isinstance(schedule, ServiceAdjustmentSchedule):
            raise TypeError("schedule은 ServiceAdjustmentSchedule이어야 합니다.")
        selected = tuple(public_holidays)
        calendar = tuple(calendar_holidays)
        if not selected:
            raise ValueError("검토할 공휴일을 하나 이상 선택해야 합니다.")
        if any(
            not isinstance(candidate, PublicHolidayCandidate)
            for candidate in (*selected, *calendar)
        ):
            raise TypeError("공휴일 후보가 올바르지 않습니다.")
        if len({candidate.day for candidate in selected}) != len(selected):
            raise ValueError("같은 공휴일을 중복 선택할 수 없습니다.")
        calendar_days = {candidate.day for candidate in calendar}
        if any(candidate.day not in calendar_days for candidate in selected):
            raise ValueError("선택한 공휴일이 표시된 달력에 없습니다.")
        if roster is not None and not isinstance(roster, RosterSnapshot):
            raise TypeError("roster는 RosterSnapshot이어야 합니다.")
        super().__init__(parent)
        self._schedule = schedule
        self._roster = roster
        self._public_holidays = tuple(
            sorted(selected, key=lambda candidate: candidate.day)
        )
        self._calendar_days = frozenset(calendar_days)
        self._meal_checks: dict[Meal, QCheckBox] = {}
        self._meal_cards: dict[Meal, QFrame] = {}
        self._treatment_cards: dict[ServiceTreatment, QFrame] = {}
        self._delivery_values: dict[tuple[date, Meal], date] = {}
        self._delivery_editors: dict[tuple[date, Meal], WheelSafeDateEdit] = {}
        self._rebuilding_deliveries = False
        register_application_fonts()
        self.setObjectName("publicHolidayBatchDialog")
        self.setWindowTitle("공휴일 일괄 검토")
        self.resize(900, 820)
        self.setMinimumSize(760, 700)
        self.setStyleSheet(application_stylesheet())
        self._build_ui()
        self._refresh_state()

    def selected_public_holidays(
        self,
    ) -> tuple[PublicHolidayCandidate, ...]:
        return self._public_holidays

    def selected_meals(self) -> tuple[Meal, ...]:
        return tuple(
            meal for meal in Meal if self._meal_checks[meal].isChecked()
        )

    def selected_treatment(self) -> ServiceTreatment:
        treatment = self._selected_treatment_or_none()
        if treatment is None:
            raise RuntimeError("식사 처리 방법을 아직 선택하지 않았습니다.")
        return treatment

    def selected_replacement_deliveries(
        self,
    ) -> tuple[ReplacementDeliveryOverride, ...]:
        if self._selected_treatment_or_none() is not ServiceTreatment.REPLACEMENT:
            return ()
        self._harvest_delivery_values()
        return tuple(
            ReplacementDeliveryOverride(
                meal,
                service_on,
                self._delivery_values[(service_on, meal)],
            )
            for service_on, meal in self.replacement_service_keys()
        )

    def replacement_service_keys(self) -> tuple[tuple[date, Meal], ...]:
        meals = self.selected_meals()
        return tuple(
            (candidate.day, meal)
            for candidate in self._public_holidays
            for meal in meals
            if self._roster is None
            or _meal_is_scheduled(self._roster, meal, candidate.day)
        )

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 18, 18, 16)
        root.setSpacing(10)

        header = QFrame()
        header.setObjectName("serviceAdjustmentHeader")
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(16, 14, 16, 14)
        header_layout.setSpacing(3)
        eyebrow = QLabel("현재 저장된 데이터 · 선택한 공휴일에 같은 운영 방식 적용")
        eyebrow.setObjectName("editEyebrow")
        title = QLabel("공휴일 일괄 검토")
        title.setObjectName("workspaceTitle")
        guide = QLabel(
            "선택한 모든 날짜에 같은 식종과 처리 방법을 적용합니다. "
            "대체식의 실제 제공일은 아래 표에서 날짜·식종별로 바꿀 수 있습니다."
        )
        guide.setObjectName("workspaceSubtitle")
        guide.setWordWrap(True)
        header_layout.addWidget(eyebrow)
        header_layout.addWidget(title)
        header_layout.addWidget(guide)
        root.addWidget(header)

        ticket = QFrame()
        ticket.setObjectName("holidayBatchTicket")
        ticket_layout = QHBoxLayout(ticket)
        ticket_layout.setContentsMargins(14, 11, 14, 11)
        ticket_layout.setSpacing(12)
        stamp = QLabel(f"{len(self._public_holidays)}일")
        stamp.setObjectName("holidayBatchStamp")
        ticket_layout.addWidget(stamp)
        ticket_copy = QVBoxLayout()
        ticket_title = QLabel("선택한 공휴일")
        ticket_title.setObjectName("sectionLabel")
        ticket_copy.addWidget(ticket_title)
        selected_text = QLabel(_holiday_batch_preview(self._public_holidays))
        selected_text.setObjectName("holidayBatchSelection")
        selected_text.setWordWrap(True)
        ticket_copy.addWidget(selected_text)
        ticket_layout.addLayout(ticket_copy, 1)
        root.addWidget(ticket)

        root.addWidget(_section_label("적용할 식종 · 기본 전체"))
        meal_row = QHBoxLayout()
        meal_row.setSpacing(8)
        for meal in Meal:
            card = QFrame()
            card.setObjectName("mealChoiceCard")
            card.setProperty("selected", True)
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(12, 9, 12, 9)
            check = QCheckBox(meal.value)
            check.setObjectName("mealChoiceCheck")
            check.setChecked(True)
            check.toggled.connect(self._selection_changed)
            helper = QLabel("선택한 모든 날짜에 적용")
            helper.setObjectName("mealCurrentState")
            card_layout.addWidget(check)
            card_layout.addWidget(helper)
            meal_row.addWidget(card, 1)
            self._meal_checks[meal] = check
            self._meal_cards[meal] = card
        root.addLayout(meal_row)

        self._overwrite_notice = QLabel()
        self._overwrite_notice.setObjectName("clearNotice")
        self._overwrite_notice.setWordWrap(True)
        root.addWidget(self._overwrite_notice)

        root.addWidget(_section_label("선택한 공휴일의 식사 처리"))
        treatment_row = QHBoxLayout()
        treatment_row.setSpacing(8)
        self._treatment_group = QButtonGroup(self)
        self._treatment_group.setExclusive(True)
        self._normal_radio = self._add_treatment_card(
            treatment_row,
            ServiceTreatment.NORMAL,
            "정상 운영",
            "평소대로 배송 · 청구 유지",
        )
        self._not_provided_radio = self._add_treatment_card(
            treatment_row,
            ServiceTreatment.NOT_PROVIDED,
            "미제공",
            "배송 없음 · 청구 식수 제외",
        )
        self._replacement_radio = self._add_treatment_card(
            treatment_row,
            ServiceTreatment.REPLACEMENT,
            "대체식 제공",
            "실제 제공일 변경 · 원래 식사일 청구",
        )
        root.addLayout(treatment_row)

        self._delivery_frame = QFrame()
        self._delivery_frame.setObjectName("replacementManifest")
        delivery_layout = QVBoxLayout(self._delivery_frame)
        delivery_layout.setContentsMargins(12, 10, 12, 11)
        delivery_layout.setSpacing(6)
        delivery_title = QLabel("원래 공휴일 → 실제 대체식 제공일")
        delivery_title.setObjectName("sectionLabel")
        delivery_layout.addWidget(delivery_title)
        delivery_help = QLabel(
            "공휴일과 주말을 건너뛴 직전 운영일을 제안합니다. "
            "실제 제공일이 다른 칸만 바꾸세요."
        )
        delivery_help.setObjectName("muted")
        delivery_help.setWordWrap(True)
        delivery_layout.addWidget(delivery_help)
        self._delivery_table = QTableWidget()
        self._delivery_table.setObjectName("holidayBatchDeliveryTable")
        self._delivery_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self._delivery_table.setSelectionMode(
            QAbstractItemView.SelectionMode.NoSelection
        )
        self._delivery_table.verticalHeader().setVisible(False)
        self._delivery_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self._delivery_table.setMinimumHeight(220)
        delivery_layout.addWidget(self._delivery_table)
        root.addWidget(self._delivery_frame, 1)

        self._summary = QLabel()
        self._summary.setObjectName("serviceAdjustmentSummary")
        self._summary.setWordWrap(True)
        root.addWidget(self._summary)
        self._error = QLabel()
        self._error.setObjectName("errorMessage")
        self._error.setWordWrap(True)
        root.addWidget(self._error)

        actions = QHBoxLayout()
        actions.addStretch(1)
        close = QPushButton("뒤로")
        close.setObjectName("quietAction")
        close.clicked.connect(self.reject)
        actions.addWidget(close)
        self._save_button = QPushButton(
            f"선택한 공휴일 {len(self._public_holidays)}일 저장"
        )
        self._save_button.setObjectName("primaryAction")
        self._save_button.clicked.connect(self._save)
        actions.addWidget(self._save_button)
        root.addLayout(actions)

    def _add_treatment_card(
        self,
        row: QHBoxLayout,
        treatment: ServiceTreatment,
        title: str,
        helper: str,
    ) -> QRadioButton:
        card = QFrame()
        card.setObjectName("serviceTreatmentCard")
        card.setProperty("selected", False)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(12, 9, 12, 9)
        radio = QRadioButton(title)
        radio.setObjectName("serviceTreatmentRadio")
        radio.toggled.connect(self._selection_changed)
        description = QLabel(helper)
        description.setObjectName("mealCurrentState")
        description.setWordWrap(True)
        layout.addWidget(radio)
        layout.addWidget(description)
        self._treatment_group.addButton(radio)
        self._treatment_cards[treatment] = card
        row.addWidget(card, 1)
        return radio

    def _selected_treatment_or_none(self) -> ServiceTreatment | None:
        if self._normal_radio.isChecked():
            return ServiceTreatment.NORMAL
        if self._not_provided_radio.isChecked():
            return ServiceTreatment.NOT_PROVIDED
        if self._replacement_radio.isChecked():
            return ServiceTreatment.REPLACEMENT
        return None

    def _selection_changed(self, *_args) -> None:
        if self._selected_treatment_or_none() is ServiceTreatment.REPLACEMENT:
            self._rebuild_delivery_table()
        self._refresh_state()

    def _harvest_delivery_values(self) -> None:
        if self._rebuilding_deliveries:
            return
        for key, editor in self._delivery_editors.items():
            self._delivery_values[key] = _python_date(editor)

    def _rebuild_delivery_table(self) -> None:
        if not hasattr(self, "_delivery_table"):
            return
        self._harvest_delivery_values()
        self._rebuilding_deliveries = True
        try:
            meals = self.selected_meals()
            self._delivery_table.clearContents()
            self._delivery_table.setColumnCount(2 + len(tuple(Meal)))
            self._delivery_table.setHorizontalHeaderLabels(
                (
                    "원래 식사일",
                    "공휴일",
                    *(f"{meal.value} 제공일" for meal in Meal),
                )
            )
            self._delivery_table.setRowCount(len(self._public_holidays))
            self._delivery_editors.clear()
            service_keys = frozenset(self.replacement_service_keys())
            for row, candidate in enumerate(self._public_holidays):
                day_item = QTableWidgetItem(
                    f"{candidate.day:%m.%d} ({_WEEKDAY_LABELS[candidate.day.weekday()]})"
                )
                day_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self._delivery_table.setItem(row, 0, day_item)
                self._delivery_table.setItem(
                    row,
                    1,
                    QTableWidgetItem(candidate.name),
                )
                scheduled_meals = tuple(
                    meal
                    for meal in meals
                    if (candidate.day, meal) in service_keys
                )
                suggestion = previous_common_operating_day(
                    self._schedule,
                    scheduled_meals or meals or tuple(Meal),
                    candidate.day,
                    excluded_days=tuple(self._calendar_days),
                )
                for column, meal in enumerate(Meal, start=2):
                    if (candidate.day, meal) not in service_keys:
                        empty = QTableWidgetItem("—")
                        empty.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                        self._delivery_table.setItem(row, column, empty)
                        continue
                    key = (candidate.day, meal)
                    value = self._delivery_values.setdefault(key, suggestion)
                    editor = _date_edit(
                        QDate(value.year, value.month, value.day),
                        "holidayBatchDeliveryDate",
                    )
                    editor.setDisplayFormat("yyyy.MM.dd")
                    editor.dateChanged.connect(self._refresh_state)
                    self._delivery_table.setCellWidget(row, column, editor)
                    self._delivery_editors[key] = editor
        finally:
            self._rebuilding_deliveries = False

    def _refresh_state(self, *_args) -> None:
        if not hasattr(self, "_delivery_frame"):
            return
        replacement = (
            self._selected_treatment_or_none() is ServiceTreatment.REPLACEMENT
        )
        self._delivery_frame.setVisible(replacement)
        for meal, card in self._meal_cards.items():
            _set_selected(card, self._meal_checks[meal].isChecked())
        selected_treatment = self._selected_treatment_or_none()
        for treatment, card in self._treatment_cards.items():
            _set_selected(card, treatment is selected_treatment)
        error = self._validation_error()
        self._error.setText(error or "")
        self._error.setVisible(error is not None)
        self._save_button.setEnabled(error is None)
        self._summary.setText(self._summary_text())
        reviewed_count = self._reviewed_cell_count()
        self._overwrite_notice.setText(
            f"선택한 범위에 이미 검토한 식종 {reviewed_count}칸이 있습니다. "
            "저장하면 이번 선택으로 다시 기록되며 기존 이력은 지워지지 않습니다."
        )
        self._overwrite_notice.setVisible(reviewed_count > 0)

    def _reviewed_cell_count(self) -> int:
        meals = self.selected_meals()
        return sum(
            1
            for candidate in self._public_holidays
            for meal in meals
            if meal
            in self._schedule.public_holiday_treatments_for(candidate.day)
        )

    def _validation_error(self) -> str | None:
        if not self.selected_meals():
            return "적용할 식종을 한 개 이상 선택해주세요."
        if self._selected_treatment_or_none() is None:
            return "선택한 공휴일의 식사 처리 방법을 선택해주세요."
        if (
            self._selected_treatment_or_none() is ServiceTreatment.REPLACEMENT
            and not self.replacement_service_keys()
        ):
            return "선택한 범위에 대체식을 제공할 예정 식사가 없습니다."
        return None

    def _summary_text(self) -> str:
        meals = self.selected_meals()
        prefix = (
            f"공휴일 {len(self._public_holidays)}일 · "
            f"{' · '.join(meal.value for meal in meals) if meals else '식종 미선택'}"
        )
        treatment = self._selected_treatment_or_none()
        if treatment is None:
            return f"{prefix}\n정상 운영, 미제공 또는 대체식 중 하나를 선택해주세요."
        if treatment is ServiceTreatment.NORMAL:
            return f"{prefix}\n선택한 모든 날짜를 평소대로 배송하고 청구합니다."
        if treatment is ServiceTreatment.NOT_PROVIDED:
            return (
                f"{prefix}\n선택한 모든 날짜에 배송하지 않고 청구 식수에서 제외합니다."
            )
        deliveries = self.selected_replacement_deliveries()
        return (
            f"{prefix}\n원래 식사일은 청구에 유지하고 "
            f"실제 제공일 {len(deliveries)}칸을 표대로 저장합니다."
        )

    def _save(self) -> None:
        error = self._validation_error()
        if error is not None:
            self._error.setText(error)
            self._error.show()
            return
        self.accept()


class ServiceAdjustmentDialog(QDialog):
    """Record a shared default replacement date and optional exact mappings."""

    def __init__(
        self,
        schedule: ServiceAdjustmentSchedule,
        anchor_day: date,
        parent: QWidget | None = None,
        *,
        public_holiday: PublicHolidayCandidate | None = None,
    ) -> None:
        if not isinstance(schedule, ServiceAdjustmentSchedule):
            raise TypeError("schedule은 ServiceAdjustmentSchedule이어야 합니다.")
        if isinstance(anchor_day, datetime) or not isinstance(anchor_day, date):
            raise TypeError("anchor day는 date여야 합니다.")
        if public_holiday is not None:
            if not isinstance(public_holiday, PublicHolidayCandidate):
                raise TypeError("public holiday는 PublicHolidayCandidate여야 합니다.")
            if public_holiday.day != anchor_day:
                raise ValueError("공휴일 후보 날짜와 편집 기준일이 다릅니다.")
        super().__init__(parent)
        self._schedule = schedule
        self._public_holiday = public_holiday
        self._meal_checks: dict[Meal, QCheckBox] = {}
        self._meal_cards: dict[Meal, QFrame] = {}
        self._treatment_cards: dict[ServiceTreatment, QFrame] = {}
        self._mapping_editors: dict[tuple[date, Meal], WheelSafeDateEdit] = {}
        self._mapping_values: dict[tuple[date, Meal], date] = {}
        self._rebuilding_mapping = False
        register_application_fonts()
        self.setObjectName("serviceAdjustmentDialog")
        self.setWindowTitle("휴무·대체식 관리")
        self.resize(840, 820)
        self.setMinimumSize(740, 700)
        self.setStyleSheet(application_stylesheet())
        self._build_ui(anchor_day)
        self._refresh_state()

    def selected_period(self) -> InclusiveDateRange:
        return InclusiveDateRange(
            _python_date(self._starts_on),
            _python_date(self._ends_on),
        )

    def selected_meals(self) -> tuple[Meal, ...]:
        return tuple(
            meal for meal in Meal if self._meal_checks[meal].isChecked()
        )

    def selected_treatment(self) -> ServiceTreatment:
        if self._normal_radio is not None and self._normal_radio.isChecked():
            return ServiceTreatment.NORMAL
        if self._replacement_radio.isChecked():
            return ServiceTreatment.REPLACEMENT
        if self._not_provided_radio.isChecked():
            return ServiceTreatment.NOT_PROVIDED
        raise RuntimeError("식사 처리 방법을 아직 선택하지 않았습니다.")

    def selected_default_delivery_on(self) -> date | None:
        if self.selected_treatment() is not ServiceTreatment.REPLACEMENT:
            return None
        return _python_date(self._default_delivery_on)

    def selected_delivery_overrides(
        self,
    ) -> tuple[ReplacementDeliveryOverride, ...]:
        if (
            self.selected_treatment() is not ServiceTreatment.REPLACEMENT
            or not self._custom_mapping.isChecked()
        ):
            return ()
        self._harvest_mapping_values()
        default = self.selected_default_delivery_on()
        assert default is not None
        return tuple(
            ReplacementDeliveryOverride(meal, service_on, delivery_on)
            for (service_on, meal), delivery_on in sorted(
                self._mapping_values.items(),
                key=lambda item: (
                    item[0][0],
                    tuple(Meal).index(item[0][1]),
                ),
            )
            if meal in self.selected_meals()
            and self.selected_period().contains(service_on)
            and delivery_on != default
        )

    def selected_note(self) -> str:
        return " ".join(self._note.text().split())

    def _build_ui(self, anchor_day: date) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 18, 18, 16)
        root.setSpacing(10)

        header = QFrame()
        header.setObjectName("serviceAdjustmentHeader")
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(16, 14, 16, 14)
        header_layout.setSpacing(3)
        eyebrow = QLabel("업체 일정 · 청구와 실제 제공일")
        eyebrow.setObjectName("editEyebrow")
        title = QLabel(
            "공휴일 운영 검토"
            if self._public_holiday is not None
            else "휴무·대체식 관리"
        )
        title.setObjectName("workspaceTitle")
        guide = QLabel(
            (
                "전체 식종이 기본 선택됩니다. 이 운영 데이터에서 실제로 어떻게 "
                "운영할지 하나를 확정해주세요."
                if self._public_holiday is not None
                else "전체 식종이 기본 선택됩니다. 미제공은 청구에서 빼고, "
                "대체식은 원래 식사일로 청구하면서 실제 제공일만 바꿉니다."
            )
        )
        guide.setObjectName("workspaceSubtitle")
        guide.setWordWrap(True)
        header_layout.addWidget(eyebrow)
        header_layout.addWidget(title)
        header_layout.addWidget(guide)
        root.addWidget(header)

        if self._public_holiday is not None:
            holiday_ticket = QFrame()
            holiday_ticket.setObjectName("holidayReviewTicket")
            ticket_layout = QHBoxLayout(holiday_ticket)
            ticket_layout.setContentsMargins(14, 10, 14, 10)
            stamp = QLabel(self._public_holiday.day.strftime("%m.%d"))
            stamp.setObjectName("holidayReviewStamp")
            ticket_layout.addWidget(stamp)
            ticket_copy = QVBoxLayout()
            holiday_name = QLabel(self._public_holiday.name)
            holiday_name.setObjectName("holidayReviewName")
            holiday_source = QLabel(
                "자동 후보 · "
                f"{self._public_holiday.provider_id} "
                f"v{self._public_holiday.provider_version}"
            )
            holiday_source.setObjectName("muted")
            ticket_copy.addWidget(holiday_name)
            ticket_copy.addWidget(holiday_source)
            ticket_layout.addLayout(ticket_copy, 1)
            root.addWidget(holiday_ticket)

        period_frame = QFrame()
        period_frame.setObjectName("editSection")
        period_layout = QGridLayout(period_frame)
        period_layout.setContentsMargins(14, 11, 14, 12)
        period_layout.setHorizontalSpacing(12)
        period_layout.setVerticalSpacing(6)
        period_layout.addWidget(_field_label("휴무 시작일"), 0, 0)
        period_layout.addWidget(_field_label("휴무 종료일"), 0, 1)
        period_layout.addWidget(_field_label("사유"), 0, 2)
        anchor = QDate(anchor_day.year, anchor_day.month, anchor_day.day)
        self._starts_on = _date_edit(anchor, "serviceAdjustmentStart")
        self._ends_on = _date_edit(anchor, "serviceAdjustmentEnd")
        self._note = QLineEdit(
            self._public_holiday.name
            if self._public_holiday is not None
            else "업체 휴무"
        )
        self._note.setObjectName("serviceAdjustmentNote")
        self._note.setPlaceholderText("예: 업체 휴무, 명절 휴무")
        if self._public_holiday is not None:
            self._starts_on.setEnabled(False)
            self._ends_on.setEnabled(False)
            self._note.setReadOnly(True)
        period_layout.addWidget(self._starts_on, 1, 0)
        period_layout.addWidget(self._ends_on, 1, 1)
        period_layout.addWidget(self._note, 1, 2)
        root.addWidget(period_frame)

        root.addWidget(_section_label("적용할 식종 · 기본 전체"))
        meal_row = QHBoxLayout()
        meal_row.setSpacing(8)
        for meal in Meal:
            card = QFrame()
            card.setObjectName("mealChoiceCard")
            card.setProperty("selected", True)
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(12, 9, 12, 9)
            check = QCheckBox(meal.value)
            check.setObjectName("mealChoiceCheck")
            check.setChecked(True)
            check.toggled.connect(self._selection_changed)
            helper = QLabel("포함")
            helper.setObjectName("mealCurrentState")
            card_layout.addWidget(check)
            card_layout.addWidget(helper)
            meal_row.addWidget(card, 1)
            self._meal_checks[meal] = check
            self._meal_cards[meal] = card
        root.addLayout(meal_row)

        root.addWidget(_section_label("휴무일의 식사 처리"))
        treatment_row = QHBoxLayout()
        treatment_row.setSpacing(8)
        self._treatment_group = QButtonGroup(self)
        self._treatment_group.setExclusive(True)
        self._normal_radio: QRadioButton | None = None
        if self._public_holiday is not None:
            self._normal_radio = self._add_treatment_card(
                treatment_row,
                ServiceTreatment.NORMAL,
                "정상 운영",
                "평소대로 배송 · 청구 유지",
            )
        self._not_provided_radio = self._add_treatment_card(
            treatment_row,
            ServiceTreatment.NOT_PROVIDED,
            "미제공",
            "배송 없음 · 청구 식수 제외",
        )
        self._replacement_radio = self._add_treatment_card(
            treatment_row,
            ServiceTreatment.REPLACEMENT,
            "대체식 제공",
            "실제 제공일 변경 · 원래 식사일 청구",
        )
        if self._public_holiday is None:
            self._not_provided_radio.blockSignals(True)
            self._not_provided_radio.setChecked(True)
            self._not_provided_radio.blockSignals(False)
        root.addLayout(treatment_row)

        self._replacement_frame = QFrame()
        self._replacement_frame.setObjectName("replacementDeliveryPanel")
        replacement_layout = QGridLayout(self._replacement_frame)
        replacement_layout.setContentsMargins(14, 11, 14, 11)
        replacement_layout.setHorizontalSpacing(12)
        replacement_layout.setVerticalSpacing(6)
        replacement_layout.addWidget(_field_label("기본 대체식 제공일"), 0, 0)
        replacement_hint = QLabel(
            "기본은 선택 기간 전체를 이 날짜에 함께 제공합니다."
        )
        replacement_hint.setObjectName("muted")
        replacement_layout.addWidget(replacement_hint, 0, 1)
        initial_delivery = previous_common_operating_day(
            self._schedule,
            tuple(Meal),
            anchor_day,
        )
        self._default_delivery_on = _date_edit(
            QDate(
                initial_delivery.year,
                initial_delivery.month,
                initial_delivery.day,
            ),
            "replacementDefaultDate",
        )
        self._last_default_delivery_on = initial_delivery
        replacement_layout.addWidget(self._default_delivery_on, 1, 0)
        self._custom_mapping = QCheckBox("날짜별로 다르게 지정")
        self._custom_mapping.setObjectName("customReplacementToggle")
        replacement_layout.addWidget(self._custom_mapping, 1, 1)
        root.addWidget(self._replacement_frame)

        self._custom_frame = QFrame()
        self._custom_frame.setObjectName("replacementManifest")
        custom_layout = QVBoxLayout(self._custom_frame)
        custom_layout.setContentsMargins(12, 10, 12, 11)
        custom_layout.setSpacing(6)
        custom_heading = QHBoxLayout()
        manifest_title = QLabel("원래 식사일 → 실제 제공일")
        manifest_title.setObjectName("sectionLabel")
        custom_heading.addWidget(manifest_title)
        custom_heading.addStretch(1)
        reset_mapping = QPushButton("모두 기본 제공일로")
        reset_mapping.setObjectName("quietAction")
        reset_mapping.clicked.connect(self._reset_mapping_dates)
        custom_heading.addWidget(reset_mapping)
        custom_layout.addLayout(custom_heading)
        manifest_help = QLabel(
            "바꿀 칸만 수정하세요. 수정하지 않은 칸은 위 기본 제공일을 사용합니다."
        )
        manifest_help.setObjectName("muted")
        custom_layout.addWidget(manifest_help)
        self._mapping_table = QTableWidget()
        self._mapping_table.setObjectName("replacementMappingTable")
        self._mapping_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self._mapping_table.setSelectionMode(
            QAbstractItemView.SelectionMode.NoSelection
        )
        self._mapping_table.verticalHeader().setVisible(False)
        self._mapping_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self._mapping_table.setMinimumHeight(150)
        custom_layout.addWidget(self._mapping_table)
        root.addWidget(self._custom_frame, 1)

        self._summary = QLabel()
        self._summary.setObjectName("serviceAdjustmentSummary")
        self._summary.setWordWrap(True)
        root.addWidget(self._summary)
        self._error = QLabel()
        self._error.setObjectName("errorMessage")
        self._error.setWordWrap(True)
        root.addWidget(self._error)

        self._history_heading = _section_label("최근 휴무·대체식 저장")
        root.addWidget(self._history_heading)
        self._history = QTableWidget()
        self._history.setObjectName("serviceAdjustmentHistory")
        self._history.setColumnCount(4)
        self._history.setHorizontalHeaderLabels(
            ("휴무 기간", "식종", "처리", "사유")
        )
        self._history.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self._history.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self._history.verticalHeader().setVisible(False)
        self._history.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self._history.setMaximumHeight(132)
        self._fill_history()
        root.addWidget(self._history)

        actions = QHBoxLayout()
        actions.addStretch(1)
        close = QPushButton("닫기")
        close.setObjectName("quietAction")
        close.clicked.connect(self.reject)
        actions.addWidget(close)
        self._save_button = QPushButton(
            "공휴일 운영 저장"
            if self._public_holiday is not None
            else "휴무·대체식 저장"
        )
        self._save_button.setObjectName("primaryAction")
        self._save_button.clicked.connect(self._save)
        actions.addWidget(self._save_button)
        root.addLayout(actions)

        self._starts_on.dateChanged.connect(self._period_changed)
        self._ends_on.dateChanged.connect(self._period_changed)
        self._note.textChanged.connect(self._refresh_state)
        self._default_delivery_on.dateChanged.connect(
            self._default_delivery_changed
        )
        self._custom_mapping.toggled.connect(self._custom_mapping_toggled)

    def _add_treatment_card(
        self,
        row: QHBoxLayout,
        treatment: ServiceTreatment,
        title: str,
        helper: str,
    ) -> QRadioButton:
        card = QFrame()
        card.setObjectName("serviceTreatmentCard")
        card.setProperty("selected", False)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(12, 9, 12, 9)
        radio = QRadioButton(title)
        radio.setObjectName("serviceTreatmentRadio")
        radio.toggled.connect(self._selection_changed)
        description = QLabel(helper)
        description.setObjectName("mealCurrentState")
        description.setWordWrap(True)
        layout.addWidget(radio)
        layout.addWidget(description)
        self._treatment_group.addButton(radio)
        self._treatment_cards[treatment] = card
        row.addWidget(card, 1)
        return radio

    def _fill_history(self) -> None:
        changes = tuple(reversed(self._schedule.changes[-5:]))
        self._history.setRowCount(max(1, len(changes)))
        if not changes:
            empty = QTableWidgetItem("아직 저장한 휴무·대체식 일정이 없습니다")
            self._history.setItem(0, 0, empty)
            self._history.setSpan(0, 0, 1, 4)
            return
        for row, change in enumerate(changes):
            assert change.period.starts_on is not None
            assert change.period.ends_on is not None
            values = (
                _period_text(change.period.starts_on, change.period.ends_on),
                " · ".join(meal.value for meal in change.meals),
                change.treatment.label,
                change.note,
            )
            for column, value in enumerate(values):
                self._history.setItem(row, column, QTableWidgetItem(value))

    def _selection_changed(self, *_args) -> None:
        self._update_default_delivery_suggestion()
        self._rebuild_mapping_table()
        self._refresh_state()

    def _period_changed(self, *_args) -> None:
        start = self._starts_on.date()
        if self._ends_on.date() < start:
            self._ends_on.setDate(start)
        self._update_default_delivery_suggestion()
        self._rebuild_mapping_table()
        self._refresh_state()

    def _update_default_delivery_suggestion(self) -> None:
        meals = self.selected_meals()
        if not meals:
            return
        suggestion = previous_common_operating_day(
            self._schedule,
            meals,
            _python_date(self._starts_on),
        )
        old_default = self._last_default_delivery_on
        self._default_delivery_on.blockSignals(True)
        self._default_delivery_on.setDate(
            QDate(suggestion.year, suggestion.month, suggestion.day)
        )
        self._default_delivery_on.blockSignals(False)
        self._shift_unchanged_mapping_dates(old_default, suggestion)
        self._last_default_delivery_on = suggestion

    def _default_delivery_changed(self, *_args) -> None:
        new_default = _python_date(self._default_delivery_on)
        self._shift_unchanged_mapping_dates(
            self._last_default_delivery_on,
            new_default,
        )
        self._last_default_delivery_on = new_default
        self._refresh_state()

    def _shift_unchanged_mapping_dates(
        self,
        old_default: date,
        new_default: date,
    ) -> None:
        for key, value in tuple(self._mapping_values.items()):
            if value == old_default:
                self._mapping_values[key] = new_default
        for editor in self._mapping_editors.values():
            if _python_date(editor) != old_default:
                continue
            editor.blockSignals(True)
            editor.setDate(
                QDate(new_default.year, new_default.month, new_default.day)
            )
            editor.blockSignals(False)

    def _custom_mapping_toggled(self, checked: bool) -> None:
        if checked:
            self._rebuild_mapping_table()
        self._refresh_state()

    def _harvest_mapping_values(self) -> None:
        if self._rebuilding_mapping:
            return
        for key, editor in self._mapping_editors.items():
            self._mapping_values[key] = _python_date(editor)

    def _rebuild_mapping_table(self) -> None:
        if not hasattr(self, "_mapping_table"):
            return
        self._harvest_mapping_values()
        self._rebuilding_mapping = True
        try:
            meals = self.selected_meals()
            period = self.selected_period()
            assert period.starts_on is not None and period.ends_on is not None
            days = tuple(_days(period.starts_on, period.ends_on))
            self._mapping_table.clearContents()
            self._mapping_table.setColumnCount(1 + len(tuple(Meal)))
            self._mapping_table.setHorizontalHeaderLabels(
                ("원래 식사일", *(f"{meal.value} 제공일" for meal in Meal))
            )
            self._mapping_table.setRowCount(len(days))
            self._mapping_editors.clear()
            default = _python_date(self._default_delivery_on)
            for row, service_on in enumerate(days):
                label = QTableWidgetItem(
                    f"{service_on:%m.%d} ({_WEEKDAY_LABELS[service_on.weekday()]})"
                )
                label.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self._mapping_table.setItem(row, 0, label)
                for column, meal in enumerate(Meal, start=1):
                    if meal not in meals:
                        empty = QTableWidgetItem("—")
                        empty.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                        self._mapping_table.setItem(row, column, empty)
                        continue
                    value = self._mapping_values.get((service_on, meal), default)
                    editor = _date_edit(
                        QDate(value.year, value.month, value.day),
                        "replacementMappingDate",
                    )
                    editor.setDisplayFormat("MM.dd")
                    editor.dateChanged.connect(self._refresh_state)
                    self._mapping_table.setCellWidget(row, column, editor)
                    self._mapping_editors[(service_on, meal)] = editor
        finally:
            self._rebuilding_mapping = False

    def _reset_mapping_dates(self) -> None:
        default = _python_date(self._default_delivery_on)
        self._mapping_values.clear()
        for editor in self._mapping_editors.values():
            editor.setDate(QDate(default.year, default.month, default.day))
        self._refresh_state()

    def _refresh_state(self, *_args) -> None:
        if not hasattr(self, "_replacement_frame"):
            return
        treatment = self._selected_treatment_or_none()
        replacement = treatment is ServiceTreatment.REPLACEMENT
        custom = replacement and self._custom_mapping.isChecked()
        self._replacement_frame.setVisible(replacement)
        self._custom_frame.setVisible(custom)
        self._history_heading.setVisible(not custom)
        self._history.setVisible(not custom)
        for meal, card in self._meal_cards.items():
            _set_selected(card, self._meal_checks[meal].isChecked())
        for treatment, card in self._treatment_cards.items():
            _set_selected(card, treatment is self._selected_treatment_or_none())
        error = self._validation_error()
        self._error.setText(error or "")
        self._error.setVisible(error is not None)
        self._save_button.setEnabled(error is None)
        self._summary.setText(self._summary_text())

    def _validation_error(self) -> str | None:
        if not self.selected_meals():
            return "휴무·대체식을 적용할 식종을 한 개 이상 선택해주세요."
        if self._selected_treatment_or_none() is None:
            return "이 공휴일의 식사 처리 방법을 선택해주세요."
        if not self.selected_note():
            return "휴무 사유를 입력해주세요."
        return None

    def _summary_text(self) -> str:
        meals = self.selected_meals()
        if not meals:
            return "식종을 한 개 이상 선택하면 저장 결과가 표시됩니다."
        period = self.selected_period()
        assert period.starts_on is not None and period.ends_on is not None
        prefix = (
            f"{_period_text(period.starts_on, period.ends_on)} · "
            f"{' · '.join(meal.value for meal in meals)}"
        )
        treatment = self._selected_treatment_or_none()
        if treatment is None:
            return f"{prefix}\n정상 운영, 미제공 또는 대체식 중 하나를 선택해주세요."
        if treatment is ServiceTreatment.NORMAL:
            return (
                f"{prefix}\n평소대로 배송하고 청구합니다. "
                "공휴일 후보를 정상 운영으로 확정합니다."
            )
        if treatment is ServiceTreatment.NOT_PROVIDED:
            return f"{prefix}\n배송하지 않고 해당 원래 식사일을 청구 식수에서 제외합니다."
        default = self.selected_default_delivery_on()
        assert default is not None
        override_count = len(self.selected_delivery_overrides())
        detail = (
            f"기본 {default:%Y.%m.%d} 제공"
            if not override_count
            else f"기본 {default:%Y.%m.%d} 제공 · 개별 변경 {override_count}건"
        )
        return (
            f"{prefix}\n원래 식사일은 청구에 유지하고 실제 제공일만 바꿉니다. {detail}"
        )

    def _selected_treatment_or_none(self) -> ServiceTreatment | None:
        if self._normal_radio is not None and self._normal_radio.isChecked():
            return ServiceTreatment.NORMAL
        if self._not_provided_radio.isChecked():
            return ServiceTreatment.NOT_PROVIDED
        if self._replacement_radio.isChecked():
            return ServiceTreatment.REPLACEMENT
        return None

    def _save(self) -> None:
        error = self._validation_error()
        if error is not None:
            self._error.setText(error)
            self._error.show()
            return
        self.accept()


class ServiceAdjustmentStatus(StrEnum):
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ServiceAdjustmentOutcome:
    status: ServiceAdjustmentStatus
    roster: RosterSnapshot | None = None
    schedule: ServiceAdjustmentSchedule | None = None
    backup_result: AutomaticBackupResult | None = None

    @property
    def completed(self) -> bool:
        return self.status is ServiceAdjustmentStatus.COMPLETED


class ServiceAdjustmentController:
    """Commit one current-DB holiday/manual instruction and back it up."""

    def __init__(
        self,
        session: DatabaseSession,
        *,
        actor_provider: Callable[[], RevisionActor] | None = None,
        hub_dialog_type=ServiceAdjustmentHubDialog,
        dialog_type=ServiceAdjustmentDialog,
        holiday_dialog_type=PublicHolidayBatchDialog,
        holiday_provider: Callable[
            [int], tuple[PublicHolidayCandidate, ...]
        ] = south_korea_public_holiday_candidates,
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
        self._hub_dialog_type = hub_dialog_type
        self._dialog_type = dialog_type
        self._holiday_dialog_type = holiday_dialog_type
        self._holiday_provider = holiday_provider
        self._message_box = message_box
        self._today_provider = today_provider or date.today
        self._recorded_at_provider = recorded_at_provider or _utc_now
        self._revision_id_factory = revision_id_factory
        self._change_id_factory = change_id_factory

    @dialog_workflow
    def run(self, parent: QWidget | None = None) -> ServiceAdjustmentOutcome:
        roster = self._session.database.current_roster()
        if roster is None:
            self._message_box.critical(
                parent,
                "휴무 일정을 저장하지 못했습니다",
                "현재 명단이 없습니다. 기존 명단 가져오기를 먼저 완료해주세요.",
            )
            return ServiceAdjustmentOutcome(ServiceAdjustmentStatus.FAILED)
        source = self._session.database.snapshot(roster.revision_id)
        stored_state = source.entity(
            SERVICE_ADJUSTMENT_SCHEDULE_KIND,
            SERVICE_ADJUSTMENT_SCHEDULE_ID,
        )
        try:
            schedule = service_adjustment_schedule_from_state(stored_state)
        except Exception as error:
            self._message_box.critical(
                parent,
                "휴무·대체식 이력을 읽지 못했습니다",
                str(error),
            )
            return ServiceAdjustmentOutcome(ServiceAdjustmentStatus.FAILED)
        anchor_day = self._today_provider()
        hub = self._hub_dialog_type(
            schedule,
            anchor_day,
            self._holiday_provider,
            parent,
        )
        if (yield DialogRequest(hub, self._session)) != QDialog.DialogCode.Accepted:
            return ServiceAdjustmentOutcome(ServiceAdjustmentStatus.CANCELLED)
        request = hub.selected_request()
        if request.action is ServiceAdjustmentAction.PUBLIC_HOLIDAY:
            dialog = self._holiday_dialog_type(
                schedule,
                request.public_holidays,
                request.calendar_holidays,
                parent,
                roster=roster,
            )
        else:
            dialog = self._dialog_type(schedule, anchor_day, parent)
        if (yield DialogRequest(dialog, self._session)) != QDialog.DialogCode.Accepted:
            return ServiceAdjustmentOutcome(ServiceAdjustmentStatus.CANCELLED)
        try:
            treatment = dialog.selected_treatment()
            if request.action is ServiceAdjustmentAction.PUBLIC_HOLIDAY:
                prepared = build_public_holiday_review_revision(
                    roster.revision_id,
                    stored_state,
                    request.public_holidays,
                    dialog.selected_meals(),
                    treatment,
                    dialog.selected_replacement_deliveries(),
                    actor=self._actor_provider(),
                    recorded_at=self._recorded_at_provider(),
                    revision_id=self._revision_id_factory(),
                    change_ids=tuple(
                        self._change_id_factory()
                        for _candidate in request.public_holidays
                    ),
                    replacement_targets=(
                        dialog.replacement_service_keys()
                        if treatment is ServiceTreatment.REPLACEMENT
                        else None
                    ),
                )
            else:
                prepared = build_service_adjustment_revision(
                    roster.revision_id,
                    stored_state,
                    dialog.selected_period(),
                    dialog.selected_meals(),
                    treatment,
                    dialog.selected_default_delivery_on(),
                    dialog.selected_delivery_overrides(),
                    dialog.selected_note(),
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
                "휴무·대체식을 저장하지 못했습니다",
                "일정과 변경 이력은 저장하지 않았습니다. 내용을 확인한 뒤 다시 시도해주세요.\n\n"
                + str(error),
            )
            return ServiceAdjustmentOutcome(ServiceAdjustmentStatus.FAILED)
        if updated_roster is None:
            self._message_box.critical(
                parent,
                "저장한 휴무 일정을 표시하지 못했습니다",
                "일정은 저장됐지만 현재 명단을 다시 읽지 못했습니다. 프로그램을 다시 열어주세요.",
            )
            return ServiceAdjustmentOutcome(ServiceAdjustmentStatus.FAILED)
        if backup_result.error is not None:
            self._message_box.warning(
                parent,
                "일정은 저장됐지만 자동 백업에 실패했습니다",
                "휴무·대체식 일정과 이력은 운영 데이터에 정상 저장되었습니다. "
                "백업 폴더 연결을 확인한 뒤 백업을 다시 실행해주세요.\n\n"
                + str(backup_result.error),
            )
        return ServiceAdjustmentOutcome(
            ServiceAdjustmentStatus.COMPLETED,
            updated_roster,
            prepared.schedule,
            backup_result,
        )


def _date_edit(value: QDate, object_name: str) -> WheelSafeDateEdit:
    editor = WheelSafeDateEdit(value)
    editor.setObjectName(object_name)
    editor.setCalendarPopup(True)
    editor.setDisplayFormat("yyyy.MM.dd")
    return editor


def _python_date(editor: WheelSafeDateEdit) -> date:
    value = editor.date()
    return date(value.year(), value.month(), value.day())


def _days(starts_on: date, ends_on: date):
    current = starts_on
    while current <= ends_on:
        yield current
        current += timedelta(days=1)


def _period_text(starts_on: date, ends_on: date) -> str:
    if starts_on == ends_on:
        return starts_on.strftime("%Y.%m.%d")
    return f"{starts_on:%Y.%m.%d}~{ends_on:%m.%d}"


def _meal_is_scheduled(
    roster: RosterSnapshot,
    meal: Meal,
    service_on: date,
) -> bool:
    for person in roster.persons:
        if meal not in roster.meals_for(person.person_id, on=service_on):
            continue
        setting = next(
            (
                item
                for item in roster.delivery_settings_for(
                    person.person_id,
                    on=service_on,
                )
                if item.meal is meal
            ),
            None,
        )
        weekdays = (
            setting.weekdays
            if setting is not None and setting.weekdays is not None
            else default_service_weekdays(meal, roster)
        )
        if service_on.weekday() in weekdays:
            return True
    return False


def _holiday_batch_preview(
    candidates: tuple[PublicHolidayCandidate, ...],
) -> str:
    preview_limit = 6
    visible = candidates[:preview_limit]
    text = " · ".join(
        f"{candidate.day:%m.%d} {candidate.name}" for candidate in visible
    )
    remaining = len(candidates) - len(visible)
    return text if not remaining else f"{text} · 외 {remaining}일"


def _holiday_review_text(
    schedule: ServiceAdjustmentSchedule,
    candidate: PublicHolidayCandidate,
) -> tuple[str, int]:
    treatments = schedule.public_holiday_treatments_for(candidate.day)
    missing = tuple(meal for meal in Meal if meal not in treatments)
    if not treatments:
        return "검토 필요 · 전체 식종", len(missing)
    if missing:
        reviewed = " · ".join(
            f"{meal.value} {_short_treatment(treatments[meal])}"
            for meal in Meal
            if meal in treatments
        )
        return f"일부 검토 · {reviewed} · 미검토 {len(missing)}개", len(missing)
    values = tuple(treatments[meal] for meal in Meal)
    if len(set(values)) == 1:
        return values[0].label, 0
    return (
        " · ".join(
            f"{meal.value} {_short_treatment(treatments[meal])}"
            for meal in Meal
        ),
        0,
    )


def _short_treatment(treatment: ServiceTreatment) -> str:
    return {
        ServiceTreatment.NORMAL: "정상",
        ServiceTreatment.NOT_PROVIDED: "미제공",
        ServiceTreatment.REPLACEMENT: "대체식",
    }[treatment]


def _set_selected(frame: QFrame, selected: bool) -> None:
    frame.setProperty("selected", selected)
    frame.style().unpolish(frame)
    frame.style().polish(frame)


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
