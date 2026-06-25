"""주말·휴무일 선택 제한 + 시각적 표시 캘린더 위젯.

holidays 라이브러리 의존 제거. DbReader.get_holiday_set()의 결과를 받아 사용.
"""

from __future__ import annotations

from datetime import date

from PyQt6.QtCore import QDate, QLocale, Qt
from PyQt6.QtGui import QColor, QPainter
from PyQt6.QtWidgets import QCalendarWidget


class MealCalendarWidget(QCalendarWidget):
    """주말·휴무일 선택 불가 + 색상 표시."""

    def __init__(self, holiday_set: set[date] | None = None, parent=None):
        super().__init__(parent)
        self._holiday_set: set[date] = holiday_set or set()
        self.setFirstDayOfWeek(Qt.DayOfWeek.Sunday)
        self.setVerticalHeaderFormat(
            QCalendarWidget.VerticalHeaderFormat.NoVerticalHeader
        )
        self.setLocale(QLocale(QLocale.Language.Korean, QLocale.Country.SouthKorea))

    def set_holidays(self, holiday_set: set[date]) -> None:
        """런타임에 휴무일 목록 갱신."""
        self._holiday_set = set(holiday_set)
        self.updateCells()

    def _is_invalid_date(self, qdate: QDate) -> bool:
        dow = qdate.dayOfWeek()
        if dow in (6, 7):
            return True
        d = date(qdate.year(), qdate.month(), qdate.day())
        return d in self._holiday_set

    def mouseReleaseEvent(self, event):
        """주말·휴무일 클릭 시 선택 취소."""
        super().mouseReleaseEvent(event)
        sel = self.selectedDate()
        if sel.isValid() and self._is_invalid_date(sel):
            # 오늘로 되돌림 (또는 이전 유효일)
            self.setSelectedDate(QDate.currentDate())

    def paintCell(self, painter: QPainter, rect, qdate) -> None:
        dow = qdate.dayOfWeek()
        in_month = (
            qdate.month() == self.monthShown() and qdate.year() == self.yearShown()
        )
        d = date(qdate.year(), qdate.month(), qdate.day())
        is_holiday = d in self._holiday_set
        restricted = in_month and (dow in (6, 7) or is_holiday)

        if restricted:
            painter.fillRect(rect, QColor(240, 240, 240))

        super().paintCell(painter, rect, qdate)

        if restricted:
            if dow == 7 or is_holiday:
                painter.setPen(QColor(220, 40, 40))
            else:
                painter.setPen(QColor(60, 130, 255))
            fnt = painter.font()
            fnt.setBold(True)
            painter.setFont(fnt)
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, str(qdate.day()))
