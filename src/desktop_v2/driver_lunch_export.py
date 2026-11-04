"""Period selection and Excel output for driver lunch dates."""

from calendar import monthrange
from datetime import date, timedelta

from PySide6.QtCore import QDate
from PySide6.QtWidgets import QDialog, QFormLayout, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from .current_sheet_export import CurrentSheetSnapshot, TextWorkbookSnapshot
from .date_edit import WheelSafeDateEdit
from .theme import application_stylesheet, register_application_fonts


def suggested_lunch_period(calendar, day):
    """Choose the nearest reviewed season, merging overlapping school ranges."""
    periods = sorted((period.starts_on, period.ends_on)
                     for schedule in calendar.schedules for period in schedule.covered_periods)
    merged = []
    for start, end in periods:
        if merged and start <= merged[-1][1] + timedelta(days=1):
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    if not merged:
        return date(day.year, day.month, 1), date(day.year, day.month, monthrange(day.year, day.month)[1])
    return min(merged, key=lambda period: (0 if period[0] <= day <= period[1] else
               min(abs((day - period[0]).days), abs((day - period[1]).days)), period[0]))


class DriverLunchPeriodDialog(QDialog):
    def __init__(self, starts_on, ends_on, parent=None):
        super().__init__(parent)
        register_application_fonts()
        self.setStyleSheet(application_stylesheet())
        self.setWindowTitle("중식 기간 Excel 내보내기")
        self.resize(540, 260)
        layout = QVBoxLayout(self)
        note = QLabel("조회 범위 안에서 기사별 첫 중식 배송일과 마지막 배송일을 내보냅니다. 학교명은 출력하지 않습니다.")
        note.setWordWrap(True)
        layout.addWidget(note)
        form = QFormLayout()
        self.start = self._date_edit(starts_on)
        self.end = self._date_edit(ends_on)
        form.addRow("조회 시작일", self.start)
        form.addRow("조회 종료일", self.end)
        layout.addLayout(form)
        hint = QLabel("저장된 학교 일정이 있는 날짜만 계산합니다. 일정이 없거나 검토 범위 밖인 날짜는 제외합니다. 계산된 배송이 없는 기사는 날짜를 ‘-’로 표시합니다.")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.error = QLabel()
        self.error.setObjectName("errorMessage")
        self.error.setWordWrap(True)
        self.error.hide()
        layout.addWidget(self.error)
        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel = QPushButton("취소")
        cancel.setObjectName("quietAction")
        cancel.clicked.connect(self.reject)
        self.export_button = QPushButton("Excel 내보내기")
        self.export_button.setObjectName("primaryAction")
        self.export_button.clicked.connect(self._confirm)
        buttons.addWidget(cancel)
        buttons.addWidget(self.export_button)
        layout.addLayout(buttons)

    @staticmethod
    def _date_edit(day):
        editor = WheelSafeDateEdit(QDate(day.year, day.month, day.day))
        editor.setCalendarPopup(True)
        editor.setDisplayFormat("yyyy-MM-dd")
        return editor

    def period(self):
        return self.start.date().toPython(), self.end.date().toPython()

    def _confirm(self):
        start, end = self.period()
        if start > end:
            self.error.setText("조회 종료일은 시작일 이후로 선택해주세요.")
            self.error.show()
            return
        self.accept()


def lunch_period_snapshot(rows, starts_on, ends_on):
    sheet = CurrentSheetSnapshot(
        "기사별 중식 기간", ("배송기사", "중식 시작일", "중식 종료일"),
        tuple((row.driver_name,
               row.starts_on.isoformat() if row.starts_on else "-",
               row.ends_on.isoformat() if row.ends_on else "-") for row in rows),
    )
    return TextWorkbookSnapshot(f"기사별 중식 기간_{starts_on:%Y%m%d}-{ends_on:%Y%m%d}", (sheet,))
