"""Computer-local preference for coloring effective stopped meal periods."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .theme import application_stylesheet, register_application_fonts


class StoppedHighlightMode(StrEnum):
    MONTH = "month"
    DAY = "day"
    DAYS = "days"


@dataclass(frozen=True, slots=True)
class StoppedHighlightPreference:
    mode: StoppedHighlightMode = StoppedHighlightMode.MONTH
    days: int = 30

    def __post_init__(self) -> None:
        if not isinstance(self.mode, StoppedHighlightMode):
            raise TypeError("중지 표시 기간 방식이 올바르지 않습니다.")
        if isinstance(self.days, bool) or not isinstance(self.days, int) or not 1 <= self.days <= 36500:
            raise ValueError("중지 표시 일수는 1일 이상 36500일 이하여야 합니다.")

    def is_red(self, start: date | None, day: date) -> bool:
        if start is None or day < start:
            return False
        if self.mode is StoppedHighlightMode.MONTH:
            return (day.year, day.month) == (start.year, start.month)
        if self.mode is StoppedHighlightMode.DAY:
            return day == start
        return (day - start).days < self.days


def load_preference(settings: QSettings | None, key: str | None) -> StoppedHighlightPreference:
    if settings is None or key is None:
        return StoppedHighlightPreference()
    try:
        raw = json.loads(settings.value(key, ""))
        return StoppedHighlightPreference(StoppedHighlightMode(raw["mode"]), raw["days"])
    except (TypeError, ValueError, KeyError, json.JSONDecodeError):
        return StoppedHighlightPreference()


def save_preference(settings: QSettings, key: str, preference: StoppedHighlightPreference) -> None:
    settings.setValue(key, json.dumps({"mode": preference.mode.value, "days": preference.days}))
    settings.sync()
    if settings.status() != QSettings.Status.NoError:
        raise OSError("중지 표시 기간 설정을 저장하지 못했습니다.")


class StoppedHighlightDialog(QDialog):
    def __init__(self, current: StoppedHighlightPreference, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        register_application_fonts()
        self.setStyleSheet(application_stylesheet())
        self.setWindowTitle("중지 표시 기간")
        self.setMinimumWidth(440)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(12)
        title = QLabel("중지 표시 기간")
        title.setObjectName("workspaceTitle")
        layout.addWidget(title)
        note = QLabel(
            "중지 시작일과 명단의 조회일을 기준으로 표시 색을 정합니다. "
            "설정한 기간에는 빨간색, 이후에는 회색으로 표시합니다."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        row = QHBoxLayout()
        self.mode = QComboBox(self)
        self.mode.setAccessibleName("중지 표시 기간")
        self.mode.addItem("당월", StoppedHighlightMode.MONTH)
        self.mode.addItem("당일", StoppedHighlightMode.DAY)
        self.mode.addItem("N일", StoppedHighlightMode.DAYS)
        self.mode.setCurrentIndex(self.mode.findData(current.mode))
        self.days = QSpinBox(self)
        self.days.setAccessibleName("중지 표시 일수")
        self.days.setRange(1, 36500)
        self.days.setSuffix("일")
        self.days.setValue(current.days)
        row.addWidget(self.mode, 1)
        row.addWidget(self.days)
        layout.addLayout(row)
        self.mode.currentIndexChanged.connect(self._update_days_enabled)
        self._update_days_enabled()
        detail = QLabel("당월은 시작일이 속한 달 말일까지, 당일은 시작일 하루만 표시합니다. N일은 시작일을 1일째로 셉니다.")
        detail.setWordWrap(True)
        layout.addWidget(detail)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("저장")
        buttons.button(QDialogButtonBox.StandardButton.Save).setObjectName("primaryAction")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("취소")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setObjectName("quietAction")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _update_days_enabled(self) -> None:
        self.days.setEnabled(StoppedHighlightMode(self.mode.currentData()) is StoppedHighlightMode.DAYS)

    def selected_preference(self) -> StoppedHighlightPreference:
        return StoppedHighlightPreference(StoppedHighlightMode(self.mode.currentData()), self.days.value())
