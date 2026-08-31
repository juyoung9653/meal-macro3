"""Status period editor used by the exact roster context-menu actions."""

from __future__ import annotations

from datetime import date

from PySide6.QtCore import QDate
from PySide6.QtWidgets import (
    QCheckBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from src.domain.roster import InclusiveDateRange
from src.domain.status import MealStatus

from .theme import application_stylesheet, register_application_fonts


class StatusPeriodDialog(QDialog):
    def __init__(
        self,
        status: MealStatus,
        anchor_day: date,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        if not isinstance(status, MealStatus):
            raise TypeError("status는 MealStatus여야 합니다.")
        if not isinstance(anchor_day, date):
            raise TypeError("anchor_day는 date여야 합니다.")
        self._status = status
        register_application_fonts()
        self.setStyleSheet(application_stylesheet())
        self.setWindowTitle(f"{status.value} 기간")
        self.setMinimumWidth(430)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        title = QLabel(f"{status.value} 적용 기간")
        title.setObjectName("workspaceTitle")
        layout.addWidget(title)
        guide = QLabel(
            "과거 날짜도 선택할 수 있습니다. 열린 기간은 이전부터 또는 계속으로 "
            "표시됩니다."
        )
        guide.setObjectName("workspaceSubtitle")
        guide.setWordWrap(True)
        layout.addWidget(guide)

        form = QFormLayout()
        self._starts_on = _date_edit(anchor_day)
        self._ends_on = _date_edit(anchor_day)
        form.addRow("시작일", self._starts_on)
        form.addRow("종료일", self._ends_on)
        self._open_start = QCheckBox("시작일 없음 · 이전부터")
        self._open_end = QCheckBox("종료일 없음 · 계속")
        self._open_start.toggled.connect(
            lambda checked: self._starts_on.setEnabled(not checked)
        )
        self._open_end.toggled.connect(
            lambda checked: self._ends_on.setEnabled(not checked)
        )
        form.addRow("", self._open_start)
        form.addRow("", self._open_end)
        layout.addLayout(form)

        if status is MealStatus.PAUSED:
            self._open_start.hide()
            self._open_end.hide()
        else:
            self._open_end.setChecked(True)
        self._error = QLabel()
        self._error.setStyleSheet("color: #B64A48; font-weight: 700;")
        self._error.setWordWrap(True)
        layout.addWidget(self._error)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.Ok
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(
            f"{status.value} 적용"
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setObjectName(
            "primaryAction"
        )
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("취소")
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self._validate_and_accept)
        layout.addWidget(buttons)

    def selected_period(self) -> InclusiveDateRange:
        starts_on = None if self._open_start.isChecked() else _python_date(self._starts_on)
        ends_on = None if self._open_end.isChecked() else _python_date(self._ends_on)
        if starts_on is None and ends_on is None:
            raise ValueError("시작일과 종료일을 동시에 열어둘 수 없습니다.")
        period = InclusiveDateRange(starts_on, ends_on)
        if self._status is MealStatus.PAUSED and (
            period.starts_on is None or period.ends_on is None
        ):
            raise ValueError("일시중지는 시작일과 종료일이 모두 필요합니다.")
        return period

    def _validate_and_accept(self) -> None:
        try:
            self.selected_period()
        except ValueError as error:
            self._error.setText(str(error))
            return
        self.accept()


def _date_edit(value: date) -> QDateEdit:
    editor = QDateEdit(QDate(value.year, value.month, value.day))
    editor.setCalendarPopup(True)
    editor.setDisplayFormat("yyyy.MM.dd")
    return editor


def _python_date(editor: QDateEdit) -> date:
    value = editor.date()
    return date(value.year(), value.month(), value.day())
