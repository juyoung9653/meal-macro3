"""Local archive threshold setting and its edit dialog."""

from __future__ import annotations

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QFormLayout, QLabel, QSpinBox, QVBoxLayout, QWidget


class InactiveRosterSettings:
    SETTINGS_KEY = "roster/inactive_days"
    LEGACY_KEY = "roster/inactive_months"
    DEFAULT_DAYS = 365

    def __init__(self, settings: QSettings | None = None):
        self.settings = settings if settings is not None else QSettings(
            "분당구", "급식 배송 운영 SQLite v2"
        )

    @property
    def days(self) -> int:
        if self.settings.contains(self.SETTINGS_KEY):
            return self._valid_days(self.settings.value(self.SETTINGS_KEY))
        if self.settings.contains(self.LEGACY_KEY):
            try:
                months = int(self.settings.value(self.LEGACY_KEY))
            except (TypeError, ValueError, OverflowError):
                return self.DEFAULT_DAYS
            if not 1 <= months <= 1200:
                return self.DEFAULT_DAYS
            converted = self.DEFAULT_DAYS if months == 12 else round(months * 365 / 12)
            days = self._valid_days(converted)
            self.settings.setValue(self.SETTINGS_KEY, days)
            return days
        return self.DEFAULT_DAYS

    @staticmethod
    def _valid_days(value: object) -> int:
        try:
            days = int(value)
        except (TypeError, ValueError, OverflowError):
            return InactiveRosterSettings.DEFAULT_DAYS
        return days if 1 <= days <= 36500 else InactiveRosterSettings.DEFAULT_DAYS

    def save_days(self, days: int) -> None:
        if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 36500:
            raise ValueError("보관 기준은 1~36500일이어야 합니다.")
        self.settings.setValue(self.SETTINGS_KEY, days)


class InactiveThresholdDialog(QDialog):
    def __init__(self, days: int, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("보관 기준 설정")
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.days = QSpinBox(self)
        self.days.setAccessibleName("보관 기준 일수")
        self.days.setRange(1, 36500)
        self.days.setSuffix("일")
        self.days.setValue(days)
        form.addRow("연속 미배송 기간", self.days)
        layout.addLayout(form)
        layout.addWidget(QLabel("조회 날짜를 포함한 최근 일수를 기준으로 바로 반영됩니다. 취소하면 이전 기준으로 돌아갑니다."))
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("저장")
        buttons.button(QDialogButtonBox.StandardButton.Save).setObjectName("primaryAction")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("취소")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setObjectName("quietAction")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
