"""Document-specific period defaults and a plain-language period editor."""

from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from src.domain.revisions import RevisionSummary, format_revision_timestamp
from src.domain.roster import Meal

from .date_edit import WheelSafeDateEdit
from .document_hub import DocumentAction, DocumentSetupRequest
from .theme import application_stylesheet, register_application_fonts

_MONTHLY_ACTIONS = frozenset(
    (DocumentAction.INVOICE, DocumentAction.CONFIRMATION)
)


def suggest_document_period(
    action: DocumentAction,
    anchor_day: date,
) -> tuple[date, date]:
    """Derive the initial period from the date visible in the roster window."""

    if not isinstance(action, DocumentAction):
        raise TypeError("문서 작업은 DocumentAction이어야 합니다.")
    _validate_date(anchor_day, "문서 기본 날짜")
    if action in _MONTHLY_ACTIONS:
        return _month_bounds(anchor_day.year, anchor_day.month)
    return anchor_day, anchor_day


@dataclass(frozen=True, slots=True)
class DocumentPeriodSelection:
    """A document job with its source revision and validated date period."""

    action: DocumentAction
    source: RevisionSummary
    anchor_day: date
    anchor_meal: Meal
    starts_on: date
    ends_on: date

    def __post_init__(self) -> None:
        if not isinstance(self.action, DocumentAction):
            raise TypeError("문서 작업은 DocumentAction이어야 합니다.")
        if not isinstance(self.source, RevisionSummary):
            raise TypeError("문서 원본은 RevisionSummary여야 합니다.")
        _validate_date(self.anchor_day, "문서 기본 날짜")
        if not isinstance(self.anchor_meal, Meal):
            raise TypeError("문서 기본 식종은 Meal이어야 합니다.")
        _validate_date(self.starts_on, "문서 시작일")
        _validate_date(self.ends_on, "문서 종료일")
        if self.starts_on > self.ends_on:
            raise ValueError("문서 시작일은 종료일보다 늦을 수 없습니다.")
        if self.action in _MONTHLY_ACTIONS:
            expected = _month_bounds(self.starts_on.year, self.starts_on.month)
            if (self.starts_on, self.ends_on) != expected:
                raise ValueError(
                    f"{self.action.label} 기간은 한 달 전체여야 합니다."
                )
        if (
            self.action is DocumentAction.DELIVERY_SUBSTITUTE
            and self.starts_on != self.ends_on
        ):
            raise ValueError("배송대타는 하루 날짜만 선택할 수 있습니다.")


class DocumentPeriodDialog(QDialog):
    """Confirm or edit a job-specific period suggested from the roster date."""

    def __init__(
        self,
        request: DocumentSetupRequest,
        parent: QWidget | None = None,
    ) -> None:
        if not isinstance(request, DocumentSetupRequest):
            raise TypeError("문서 설정 요청이 올바르지 않습니다.")
        super().__init__(parent)
        self._request = request
        self._suggested_start, self._suggested_end = suggest_document_period(
            request.action,
            request.anchor_day,
        )
        self._year: QSpinBox | None = None
        self._month: QComboBox | None = None
        self._starts_on: WheelSafeDateEdit | None = None
        self._ends_on: WheelSafeDateEdit | None = None
        self._day: WheelSafeDateEdit | None = None

        register_application_fonts()
        self.setObjectName("documentPeriodDialog")
        self.setWindowTitle(f"{request.action.label} 기간 확인")
        self.setModal(True)
        self.resize(700, 510)
        self.setMinimumSize(620, 480)
        self.setStyleSheet(application_stylesheet())
        self._build_ui()
        self._refresh_state()

    @property
    def request(self) -> DocumentSetupRequest:
        return self._request

    def selected_period(self) -> DocumentPeriodSelection:
        starts_on, ends_on = self._current_period()
        return DocumentPeriodSelection(
            self._request.action,
            self._request.source,
            self._request.anchor_day,
            self._request.anchor_meal,
            starts_on,
            ends_on,
        )

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 22, 22, 19)
        root.setSpacing(13)
        root.addWidget(self._build_header())
        root.addWidget(self._build_anchor_ticket())
        root.addWidget(self._build_period_editor(), 1)

        self._summary = QLabel()
        self._summary.setObjectName("documentPeriodSummary")
        self._summary.setWordWrap(True)
        root.addWidget(self._summary)

        self._error = QLabel()
        self._error.setObjectName("errorMessage")
        self._error.setWordWrap(True)
        self._error.hide()
        root.addWidget(self._error)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        self._save_button = self._buttons.button(
            QDialogButtonBox.StandardButton.Ok
        )
        self._save_button.setText("이 기간 사용")
        self._save_button.setObjectName("primaryAction")
        self._buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(
            "뒤로"
        )
        self._buttons.accepted.connect(self._accept_if_valid)
        self._buttons.rejected.connect(self.reject)
        root.addWidget(self._buttons)

    def _build_header(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("documentPeriodHeader")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(18, 15, 18, 16)
        layout.setSpacing(4)
        eyebrow = QLabel("기간 선택 · 현재 화면 기준")
        eyebrow.setObjectName("editEyebrow")
        title = QLabel(f"{self._request.action.label} 기간 확인")
        title.setObjectName("workspaceTitle")
        subtitle = QLabel(_period_instruction(self._request.action))
        subtitle.setObjectName("workspaceSubtitle")
        subtitle.setWordWrap(True)
        layout.addWidget(eyebrow)
        layout.addWidget(title)
        layout.addWidget(subtitle)
        return frame

    def _build_anchor_ticket(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("documentPeriodAnchor")
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(14, 11, 14, 11)
        layout.setSpacing(13)
        stamp = QLabel(self._request.anchor_day.strftime("%m.%d"))
        stamp.setObjectName("documentPeriodAnchorStamp")
        stamp.setAlignment(Qt.AlignmentFlag.AlignCenter)
        stamp.setFixedWidth(76)
        layout.addWidget(stamp)

        copy = QVBoxLayout()
        copy.setSpacing(3)
        title = QLabel("명단 화면에서 보고 있던 날짜")
        title.setObjectName("sectionLabel")
        detail = QLabel(
            f"{self._request.anchor_day:%Y년 %m월 %d일}  ·  "
            f"변경번호 {format_revision_timestamp(self._request.source.recorded_at)}"
        )
        detail.setObjectName("documentPeriodAnchorDetail")
        copy.addWidget(title)
        copy.addWidget(detail)
        layout.addLayout(copy, 1)
        return frame

    def _build_period_editor(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("editSection")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(16, 14, 16, 15)
        layout.setSpacing(10)
        title = QLabel("만들 문서의 기간")
        title.setObjectName("sectionLabel")
        layout.addWidget(title)

        if self._request.action in _MONTHLY_ACTIONS:
            controls = QHBoxLayout()
            controls.setSpacing(8)
            self._year = QSpinBox()
            self._year.setObjectName("documentPeriodYear")
            self._year.setRange(1900, 2200)
            self._year.setSuffix("년")
            self._year.setValue(self._request.anchor_day.year)
            self._month = QComboBox()
            self._month.setObjectName("documentPeriodMonth")
            for month in range(1, 13):
                self._month.addItem(f"{month}월", month)
            self._month.setCurrentIndex(self._request.anchor_day.month - 1)
            self._year.valueChanged.connect(self._refresh_state)
            self._month.currentIndexChanged.connect(self._refresh_state)
            controls.addWidget(
                QLabel(
                    "청구할 월"
                    if self._request.action is DocumentAction.INVOICE
                    else "확인할 월"
                )
            )
            controls.addWidget(self._year)
            controls.addWidget(self._month)
            controls.addStretch(1)
            layout.addLayout(controls)
            help_text = QLabel("선택한 달의 1일부터 말일까지를 사용합니다.")
        elif self._request.action is DocumentAction.DELIVERY_REPORT:
            controls = QHBoxLayout()
            controls.setSpacing(8)
            self._starts_on = _date_edit(self._suggested_start)
            self._ends_on = _date_edit(self._suggested_end)
            self._starts_on.dateChanged.connect(self._refresh_state)
            self._ends_on.dateChanged.connect(self._refresh_state)
            controls.addWidget(QLabel("시작일"))
            controls.addWidget(self._starts_on)
            controls.addSpacing(8)
            controls.addWidget(QLabel("종료일"))
            controls.addWidget(self._ends_on)
            controls.addStretch(1)
            layout.addLayout(controls)
            help_text = QLabel("여러 날을 고르면 날짜별 리포트를 한 파일로 만듭니다.")
        else:
            controls = QHBoxLayout()
            controls.setSpacing(8)
            self._day = _date_edit(self._suggested_start)
            self._day.dateChanged.connect(self._refresh_state)
            controls.addWidget(QLabel("배송 날짜"))
            controls.addWidget(self._day)
            controls.addStretch(1)
            layout.addLayout(controls)
            help_text = QLabel("배송대타는 실제 인수인계가 필요한 하루를 선택합니다.")
        help_text.setObjectName("muted")
        help_text.setWordWrap(True)
        layout.addWidget(help_text)
        layout.addStretch(1)
        return frame

    def _current_period(self) -> tuple[date, date]:
        if self._request.action in _MONTHLY_ACTIONS:
            assert self._year is not None and self._month is not None
            month = self._month.currentData()
            if isinstance(month, bool) or not isinstance(month, int):
                raise ValueError("문서 월을 다시 선택해주세요.")
            return _month_bounds(self._year.value(), month)
        if self._request.action is DocumentAction.DELIVERY_REPORT:
            assert self._starts_on is not None and self._ends_on is not None
            return _python_date(self._starts_on), _python_date(self._ends_on)
        assert self._day is not None
        day = _python_date(self._day)
        return day, day

    def _refresh_state(self, _value=None) -> None:
        try:
            selection = self.selected_period()
        except (TypeError, ValueError) as error:
            self._summary.clear()
            self._error.setText(str(error))
            self._error.show()
            if hasattr(self, "_save_button"):
                self._save_button.setEnabled(False)
            return
        self._error.hide()
        self._summary.setText(_period_summary(selection))
        if hasattr(self, "_save_button"):
            self._save_button.setEnabled(True)

    def _accept_if_valid(self) -> None:
        try:
            self.selected_period()
        except (TypeError, ValueError):
            self._refresh_state()
            return
        self.accept()


def _month_bounds(year: int, month: int) -> tuple[date, date]:
    return date(year, month, 1), date(year, month, monthrange(year, month)[1])


def _date_edit(day: date) -> WheelSafeDateEdit:
    editor = WheelSafeDateEdit(QDate(day.year, day.month, day.day))
    editor.setObjectName("documentPeriodDate")
    editor.setCalendarPopup(True)
    editor.setDisplayFormat("yyyy.MM.dd")
    return editor


def _python_date(editor: WheelSafeDateEdit) -> date:
    value = editor.date()
    return date(value.year(), value.month(), value.day())


def _period_instruction(action: DocumentAction) -> str:
    if action in _MONTHLY_ACTIONS:
        return (
            "화면에서 보고 있던 날짜의 월 전체를 먼저 제안했습니다. 다른 월로 바꿀 수 있습니다. "
            "담당 기사·주소는 선택한 원본의 해당 월 말일 기준이며, "
            "급식 일수·금액은 월 전체의 날짜별 기록으로 계산합니다."
        )
    if action is DocumentAction.DELIVERY_REPORT:
        return "화면에서 보고 있던 날짜 하루를 먼저 제안했습니다. 필요한 기간으로 넓힐 수 있습니다."
    return "화면에서 보고 있던 날짜를 먼저 제안했습니다. 다른 배송 날짜로 바꿀 수 있습니다."


def _period_summary(selection: DocumentPeriodSelection) -> str:
    if selection.action in _MONTHLY_ACTIONS:
        return (
            f"{selection.action.label} · {selection.starts_on:%Y년 %m월} 전체  "
            f"({selection.starts_on:%m월 %d일} ~ {selection.ends_on:%m월 %d일})"
        )
    if selection.starts_on == selection.ends_on:
        return f"{selection.action.label} · {selection.starts_on:%Y년 %m월 %d일} 하루"
    return (
        f"{selection.action.label} · {selection.starts_on:%Y년 %m월 %d일} ~ "
        f"{selection.ends_on:%Y년 %m월 %d일}"
    )


def _validate_date(value: object, label: str) -> None:
    if isinstance(value, datetime) or not isinstance(value, date):
        raise TypeError(f"{label}는 date여야 합니다.")
