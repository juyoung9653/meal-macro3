"""Standalone editor for school schedules already stored in SQLite."""

from __future__ import annotations

from .user_messages import user_error

from .dialog_workflow import DialogRequest, dialog_workflow

from .startup_loading import save_revision

import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from PySide6.QtCore import QDate, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.domain.roster import (
    InclusiveDateRange,
    RosterSnapshot,
)
from src.domain.entitlements import (
    LunchDayRule,
    SchoolLunchMark,
)
from src.domain.revisions import RevisionActor
from src.domain.school_calendar import (
    SchoolLunchCalendar,
    SchoolLunchSchoolSchedule,
    normalize_grade,
    normalize_school_name,
    school_lunch_calendar_from_snapshot,
)
from src.domain.school_calendar_edits import (
    SchoolLunchRuleSegment,
    apply_school_lunch_rule_period,
    build_school_calendar_edit_revision,
    school_lunch_rule_segments,
)
from src.storage.database_session import (
    AutomaticBackupResult,
    DatabaseSession,
)

from .date_edit import WheelSafeDateEdit
from .import_controller import current_windows_actor
from .theme import application_stylesheet, register_application_fonts
from .school_grade_checks import SchoolGradeChecks


class SchoolCalendarEditDialog(QDialog):
    """Edit stored weekday facts without requiring the source workbook."""

    def __init__(
        self,
        calendar: SchoolLunchCalendar,
        parent: QWidget | None = None,
        *,
        message_box=QMessageBox,
    ) -> None:
        if not isinstance(calendar, SchoolLunchCalendar):
            raise TypeError("calendar는 SchoolLunchCalendar여야 합니다.")
        if not calendar.schedules:
            raise ValueError("편집할 저장 학교 일정이 없습니다.")
        super().__init__(parent)
        self._message_box = message_box
        self._original = {
            schedule.normalized_school_name: schedule
            for schedule in calendar.schedules
        }
        self._edited = dict(self._original)
        self._school_order = tuple(self._original)
        self._visible_segments: tuple[SchoolLunchRuleSegment, ...] = ()
        self._selected_segment: SchoolLunchRuleSegment | None = None
        self._edit_coverage: InclusiveDateRange | None = None
        self._import_requested = False
        self.delete_import_requested = None
        self._import_history = ()
        register_application_fonts()
        self.setObjectName("schoolCalendarEditDialog")
        self.setWindowTitle("저장된 학교 일정 관리")
        self.setMinimumSize(1020, 720)
        self.resize(1160, 800)
        self.setStyleSheet(application_stylesheet())
        self._build_ui()
        self._populate_schools()
        self._schools.setCurrentRow(0)

    @property
    def import_requested(self) -> bool:
        return self._import_requested

    def edited_schedules(self) -> tuple[SchoolLunchSchoolSchedule, ...]:
        if self.result() != QDialog.DialogCode.Accepted:
            raise RuntimeError("확정된 학교 일정 수정 내용이 없습니다.")
        return tuple(self._edited[key] for key in self._school_order)

    def changed_school_count(self) -> int:
        return sum(
            self._edited[key] != self._original[key]
            for key in self._school_order
        )

    def changed_weekday_count(self) -> int:
        return sum(
            _changed_weekday_count(self._original[key], self._edited[key])
            for key in self._school_order
        )

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 16)
        root.setSpacing(11)
        root.addWidget(self._header())

        periods = QHBoxLayout()
        periods.addWidget(QLabel("불러온 방학"))
        self._period_choice = QComboBox()
        self._period_choice.setObjectName("schoolCalendarImportPeriod")
        self._period_choice.addItem("전체 저장 기간", None)
        self._period_choice.currentIndexChanged.connect(self._filter_schools)
        periods.addWidget(self._period_choice, 1)
        self._history_button = QPushButton("불러온 원본 보기")
        self._history_button.setEnabled(False)
        self._history_button.clicked.connect(self._show_import_rows)
        periods.addWidget(self._history_button)
        self._delete_import = QPushButton("선택한 불러오기 전체 삭제")
        self._delete_import.setEnabled(False)
        self._delete_import.clicked.connect(self._request_delete_import)
        periods.addWidget(self._delete_import)
        root.addLayout(periods)

        self._summary = QLabel()
        self._summary.setObjectName("schoolCalendarEditSummary")
        self._summary.setWordWrap(True)
        root.addWidget(self._summary)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._school_rail())
        splitter.addWidget(self._schedule_workspace())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([255, 820])
        root.addWidget(splitter, 1)

        self._error = QLabel()
        self._error.setObjectName("errorMessage")
        self._error.setWordWrap(True)
        self._error.hide()
        root.addWidget(self._error)

        actions = QHBoxLayout()
        self._import = QPushButton("다음 방학 일정 추가")
        self._import.setObjectName("quietAction")
        self._import.setToolTip("다른 방학은 누적 보관하고 겹치는 날짜만 새 일정으로 반영합니다")
        self._import.clicked.connect(self._request_import)
        actions.addWidget(self._import)
        actions.addStretch(1)
        close = QPushButton("닫기")
        close.setObjectName("quietAction")
        close.clicked.connect(self.reject)
        actions.addWidget(close)
        self._save = QPushButton("변경 일정 저장")
        self._save.setObjectName("primaryAction")
        self._save.clicked.connect(self._save_changes)
        actions.addWidget(self._save)
        root.addLayout(actions)

    def _header(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("schoolCalendarEditHeader")
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(18, 14, 18, 14)
        text = QVBoxLayout()
        text.setSpacing(2)
        eyebrow = QLabel("저장된 학교별 중식 일정")
        eyebrow.setObjectName("editEyebrow")
        title = QLabel("방학별 중식 일정")
        title.setObjectName("workspaceTitle")
        subtitle = QLabel(
            "불러온 방학을 골라 학교별 센터 중식 기간을 확인하세요. 다음 방학도 이어서 추가할 수 있습니다."
        )
        subtitle.setObjectName("workspaceSubtitle")
        subtitle.setWordWrap(True)
        text.addWidget(eyebrow)
        text.addWidget(title)
        text.addWidget(subtitle)
        layout.addLayout(text, 1)
        stamp = QLabel("저장")
        stamp.setObjectName("schoolCalendarEditStamp")
        stamp.setAlignment(Qt.AlignmentFlag.AlignCenter)
        stamp.setFixedSize(58, 58)
        layout.addWidget(stamp)
        return frame

    def _school_rail(self) -> QFrame:
        rail = QFrame()
        rail.setObjectName("schoolCalendarSchoolRail")
        rail.setMinimumWidth(225)
        layout = QVBoxLayout(rail)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        label = QLabel("학교")
        label.setObjectName("sectionLabel")
        layout.addWidget(label)
        guide = QLabel("수정된 학교에는 청록색 점이 표시됩니다.")
        guide.setObjectName("fieldHelp")
        guide.setWordWrap(True)
        layout.addWidget(guide)
        self._school_search = QLineEdit()
        self._school_search.setPlaceholderText("학교 이름 검색")
        self._school_search.setClearButtonEnabled(True)
        self._school_search.textChanged.connect(self._filter_schools)
        layout.addWidget(self._school_search)
        self._schools = QListWidget()
        self._schools.setObjectName("schoolCalendarSchoolList")
        self._schools.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self._schools.currentRowChanged.connect(self._school_changed)
        layout.addWidget(self._schools, 1)
        self._reset_school = QPushButton("이 학교 원래대로")
        self._reset_school.setObjectName("quietAction")
        self._reset_school.clicked.connect(self._restore_school)
        layout.addWidget(self._reset_school)
        return rail

    def _schedule_workspace(self) -> QFrame:
        workspace = QFrame()
        workspace.setObjectName("schoolCalendarEditWorkspace")
        layout = QVBoxLayout(workspace)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)
        heading = QHBoxLayout()
        school_text = QVBoxLayout()
        self._school_title = QLabel()
        self._school_title.setObjectName("schoolCalendarSelectedSchool")
        self._coverage_text = QLabel()
        self._coverage_text.setWordWrap(True)
        self._coverage_text.setObjectName("fieldHelp")
        school_text.addWidget(self._school_title)
        school_text.addWidget(self._coverage_text)
        heading.addLayout(school_text, 1)
        self._new_period = QPushButton("중식 기간 추가")
        self._new_period.setObjectName("quietAction")
        self._new_period.clicked.connect(self._start_new_period)
        heading.addWidget(self._new_period)
        layout.addLayout(heading)

        self._ledger = QTableWidget(0, 5)
        self._ledger.setObjectName("schoolCalendarStoredLedger")
        self._ledger.setHorizontalHeaderLabels(
            ("중식 시작일", "중식 종료일", "날짜별 학년 예외", "중식 일수", "상태")
        )
        self._ledger.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._ledger.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self._ledger.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._ledger.setAlternatingRowColors(True)
        self._ledger.verticalHeader().setVisible(False)
        header = self._ledger.horizontalHeader()
        header.setMinimumSectionSize(54)
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        header.resizeSection(4, 66)
        self._ledger.currentCellChanged.connect(self._segment_selected)
        layout.addWidget(self._ledger, 1)

        editor = QFrame()
        editor.setObjectName("schoolCalendarSegmentTicket")
        editor_layout = QGridLayout(editor)
        editor_layout.setContentsMargins(13, 11, 13, 11)
        editor_layout.setHorizontalSpacing(9)
        editor_layout.setVerticalSpacing(6)
        self._edit_mode = QLabel("구간을 선택해주세요")
        self._edit_mode.setObjectName("schoolCalendarEditMode")
        editor_layout.addWidget(self._edit_mode, 0, 0, 1, 6)
        editor_layout.addWidget(_field_label("중식 시작일"), 1, 0)
        editor_layout.addWidget(_field_label("중식 종료일"), 1, 1)
        self._starts_on = _date_edit(date.today())
        self._ends_on = _date_edit(date.today())
        self._grades = SchoolGradeChecks()
        self._grades.setObjectName("schoolCalendarGrades")
        editor_layout.addWidget(self._starts_on, 2, 0)
        editor_layout.addWidget(self._ends_on, 2, 1)
        editor_layout.addWidget(_field_label("학년 예외 날짜"), 3, 0)
        self._grade_day = _date_edit(date.today())
        self._grade_day.dateChanged.connect(self._load_grade_day)
        editor_layout.addWidget(self._grade_day, 4, 0)
        editor_layout.addWidget(_field_label("이 날짜에 중식 받을 학년"), 3, 1, 1, 4)
        editor_layout.addWidget(self._grades, 4, 1, 1, 4)
        self._apply_grades = QPushButton("선택 날짜에 학년 예외 반영")
        self._apply_grades.clicked.connect(self._apply_grade_day)
        editor_layout.addWidget(self._apply_grades, 4, 5)
        self._apply = QPushButton("표에 반영")
        self._apply.setObjectName("primaryAction")
        self._apply.clicked.connect(self._apply_edit)
        editor_layout.addWidget(self._apply, 2, 5)
        help_text = QLabel(
            "학년 예외는 선택한 날짜 하루에만 적용됩니다. 체크한 학년만 중식을 받고, 모두 해제하면 그날은 중식에서 제외됩니다. "
            "기간 수정은 다른 날짜의 학년 예외를 바꾸지 않습니다."
        )
        help_text.setObjectName("schoolCalendarRuleHelp")
        help_text.setWordWrap(True)
        editor_layout.addWidget(help_text, 5, 0, 1, 6)
        layout.addWidget(editor)
        return workspace

    def _populate_schools(self) -> None:
        current_key = self._current_key()
        self._schools.blockSignals(True)
        self._schools.clear()
        for key in self._school_order:
            schedule = self._edited[key]
            changed = schedule != self._original[key]
            item = QListWidgetItem(
                ("●  " if changed else "   ")
                + schedule.school_name
            )
            item.setData(Qt.ItemDataRole.UserRole, key)
            if changed:
                item.setForeground(QColor("#0F6F7E"))
            self._schools.addItem(item)
        self._schools.blockSignals(False)
        if current_key in self._school_order:
            self._schools.setCurrentRow(self._school_order.index(current_key))
        self._filter_schools()
        self._refresh_summary()

    def set_import_history(self, entries) -> None:
        self._import_history = tuple(entries)
        self._period_choice.blockSignals(True)
        self._period_choice.clear()
        for index, entry in enumerate(self._import_history):
            self._period_choice.addItem(entry.label, index)
        self._period_choice.addItem("전체 저장 기간", None)
        self._period_choice.setCurrentIndex(0)
        self._period_choice.blockSignals(False)
        self._filter_schools()

    def _selected_import(self):
        index = self._period_choice.currentData()
        return None if index is None else self._import_history[index]

    def _filter_schools(self, *_args):
        if not hasattr(self, "_schools"):
            return
        entry = self._selected_import()
        self._history_button.setEnabled(entry is not None)
        self._delete_import.setEnabled(entry is not None)
        query = "".join(self._school_search.text().casefold().split())
        visible = []
        for index, key in enumerate(self._school_order):
            school = self._edited[key].school_name
            item = self._schools.item(index)
            item.setText(("●  " if self._edited[key] != self._original[key] else "   ") + school)
            item.setToolTip(school)
            show = query in "".join(school.casefold().split()) and (
                entry is None or bool(entry.school_rows(school)))
            item.setHidden(not show)
            if show:
                visible.append(index)
        selected = self._schools.currentRow()
        if selected not in visible:
            selected = visible[0] if visible else -1
            self._schools.setCurrentRow(selected)
        self._school_changed(selected)

    def _school_periods(self, school_name):
        entry = self._selected_import()
        if entry is None:
            return ()
        merged = []
        for row in sorted(entry.school_rows(school_name), key=lambda item: (item.starts_on, item.ends_on)):
            if merged and row.starts_on.toordinal() <= merged[-1].ends_on.toordinal() + 1:
                merged[-1] = InclusiveDateRange(merged[-1].starts_on, max(merged[-1].ends_on, row.ends_on))
            else:
                merged.append(InclusiveDateRange(row.starts_on, row.ends_on))
        return tuple(merged)

    def _filtered_segments(self, schedule):
        # Show only actual center-lunch days, grouped by imported vacation.
        # Monthly SCHOOL_PROVIDES defaults are calculation data, never rows.
        periods = list(self._school_periods(schedule.school_name))
        if self._selected_import() is None:
            periods = [InclusiveDateRange(row.starts_on, row.ends_on)
                       for entry in self._import_history for row in entry.school_rows(schedule.school_name)]
        active = [item for item in schedule.days if item.rule.mark is SchoolLunchMark.VENDOR_REQUIRED
                  or item.rule.grades_without_school_meal]
        # Include manually added periods, and support legacy schedules without import history.
        outside = [item.service_on for item in active if not any(p.contains(item.service_on) for p in periods)]
        if self._selected_import() is None:
            for day in outside:
                if periods and periods[-1].ends_on < day and all(
                    candidate.weekday() >= 5 for candidate in _days_between(periods[-1].ends_on, day)):
                    periods[-1] = InclusiveDateRange(periods[-1].starts_on, day)
                else:
                    periods.append(InclusiveDateRange(day, day))
        merged = []
        for period in sorted(set(periods), key=lambda p: (p.starts_on, p.ends_on)):
            if merged and period.starts_on <= merged[-1].ends_on:
                merged[-1] = InclusiveDateRange(merged[-1].starts_on, max(merged[-1].ends_on, period.ends_on))
            else:
                merged.append(period)
        result = []
        for period in merged:
            days = [item for item in active if period.contains(item.service_on)]
            if not days:
                continue
            grades = frozenset(grade for item in days for grade in item.rule.grades_without_school_meal)
            mark = SchoolLunchMark.VENDOR_REQUIRED if any(item.rule.mark is SchoolLunchMark.VENDOR_REQUIRED for item in days) else SchoolLunchMark.SCHOOL_PROVIDES
            result.append(SchoolLunchRuleSegment(days[0].service_on, days[-1].service_on, LunchDayRule(mark, grades), len(days)))
        return tuple(result)

    def _selected_coverage(self, schedule, coverage, service_on):
        periods = self._school_periods(schedule.school_name)
        period = next((item for item in periods if item.contains(service_on)), None)
        if period is None:
            return coverage
        return InclusiveDateRange(max(coverage.starts_on, period.starts_on),
                                  min(coverage.ends_on, period.ends_on))

    def _school_summary(self, schedule):
        return "센터 중식이 있는 방학 기간만 표시합니다. 학년 예외는 아래에서 날짜를 골라 하루씩 지정하세요."

    def _show_import_rows(self):
        entry = self._selected_import()
        if entry is None:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("불러온 원본 기록")
        dialog.resize(1050, 620)
        root = QVBoxLayout(dialog)
        label = QLabel(f"{entry.filename} · {entry.recorded_at.astimezone():%Y.%m.%d %H:%M} · {entry.display_code}\n"
                       "불러올 당시의 원본입니다. 이후 수정한 일정은 이전 화면에서 확인하세요.")
        label.setWordWrap(True)
        root.addWidget(label)
        table = QTableWidget(len(entry.rows), 6)
        table.setHorizontalHeaderLabels(("학교명", "방학식", "방학식 급식 제공 여부", "개학식", "개학식 급식 제공 여부", "비고"))
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        for index, row in enumerate(entry.rows):
            values = (row.school_name, row.starts_on.isoformat(),
                      "○" if row.starts_on_mark is SchoolLunchMark.SCHOOL_PROVIDES else "×",
                      row.ends_on.isoformat(),
                      "○" if row.ends_on_mark is SchoolLunchMark.SCHOOL_PROVIDES else "×", row.note or "")
            for column, value in enumerate(values):
                table.setItem(index, column, QTableWidgetItem(value))
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setStretchLastSection(True)
        root.addWidget(table)
        close = QPushButton("닫기")
        close.clicked.connect(dialog.close)
        root.addWidget(close)
        self._history_dialog = dialog
        dialog.open()

    def _school_changed(self, row: int) -> None:
        if row < 0 or row >= len(self._school_order):
            self._ledger.setRowCount(0)
            self._school_title.setText("검색 결과가 없습니다")
            self._coverage_text.clear()
            self._apply.setEnabled(False)
            self._new_period.setEnabled(False)
            self._grades.setEnabled(False)
            self._apply_grades.setEnabled(False)
            return
        self._new_period.setEnabled(True)
        self._grades.setEnabled(True)
        self._apply_grades.setEnabled(True)
        self._selected_segment = None
        self._edit_coverage = None
        self._fill_schedule()

    def _fill_schedule(self, select_row: int = 0) -> None:
        schedule = self._current_schedule()
        if schedule is None:
            return
        self._school_title.setText(schedule.school_name)
        self._grades.set_school(schedule.school_name)
        self._visible_segments = self._filtered_segments(schedule)
        self._coverage_text.setText(self._school_summary(schedule))
        original = self._original[schedule.normalized_school_name]
        original_rules = {item.service_on: item.rule for item in original.days}
        current_rules = {item.service_on: item.rule for item in schedule.days}
        self._ledger.blockSignals(True)
        self._ledger.setRowCount(len(self._visible_segments))
        for row, segment in enumerate(self._visible_segments):
            grades = "\n".join(f"{item.service_on:%m.%d}: {', '.join(sorted(item.rule.grades_without_school_meal))}"
                for item in schedule.days if segment.starts_on <= item.service_on <= segment.ends_on
                and item.rule.grades_without_school_meal) or "—"
            values = (
                segment.starts_on.strftime("%Y.%m.%d"),
                segment.ends_on.strftime("%Y.%m.%d"),
                grades,
                f"{segment.weekday_count}일",
            )
            changed = any(
                original_rules[service_on] != current_rules[service_on]
                for service_on in _weekdays(segment.starts_on, segment.ends_on)
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if changed:
                    item.setBackground(QColor("#E6F4F5"))
                    item.setForeground(QColor("#155E69"))
                self._ledger.setItem(row, column, item)
            status = QTableWidgetItem("수정됨" if changed else "저장값")
            if changed:
                status.setForeground(QColor("#0F6F7E"))
                status.setBackground(QColor("#E6F4F5"))
            else:
                status.setForeground(QColor("#71838A"))
            self._ledger.setItem(row, 4, status)
        self._ledger.blockSignals(False)
        self._ledger.resizeRowsToContents()
        if self._visible_segments:
            target = min(max(select_row, 0), len(self._visible_segments) - 1)
            self._ledger.selectRow(target)
            self._segment_selected(target, 0, -1, -1)
        else:
            self._edit_mode.setText("이 범위에 센터 중식 기간이 없습니다")
            self._apply.setEnabled(False)
            periods = self._school_periods(schedule.school_name)
            self._configure_grade_dates(periods[0] if periods else schedule.covered_periods[0])
        self._reset_school.setEnabled(schedule != original)

    def _segment_selected(
        self,
        current_row: int,
        _current_column: int,
        _previous_row: int,
        _previous_column: int,
    ) -> None:
        if current_row < 0 or current_row >= len(self._visible_segments):
            return
        schedule = self._current_schedule()
        if schedule is None:
            return
        segment = self._visible_segments[current_row]
        coverage = _coverage_for(schedule, segment.starts_on, segment.ends_on)
        coverage = self._selected_coverage(schedule, coverage, segment.starts_on)
        self._selected_segment = segment
        self._edit_coverage = coverage
        self._configure_dates(coverage, segment.starts_on, segment.ends_on)
        self._configure_grade_dates(coverage)
        self._edit_mode.setText("선택한 방학의 센터 중식 기간을 수정합니다")
        self._apply.setText("수정 내용을 표에 반영")
        self._apply.setEnabled(True)
        self._hide_error()

    def _start_new_period(self) -> None:
        schedule = self._current_schedule()
        row = self._ledger.currentRow()
        if schedule is None:
            self._show_error("먼저 학교를 선택해주세요.")
            return
        if row < 0 or row >= len(self._visible_segments):
            periods = self._school_periods(schedule.school_name)
            coverage = periods[0] if periods else schedule.covered_periods[0]
            first, last = coverage.starts_on, coverage.ends_on
        else:
            segment = self._visible_segments[row]
            coverage = _coverage_for(schedule, segment.starts_on, segment.ends_on)
            coverage = self._selected_coverage(schedule, coverage, segment.starts_on)
            first, last = segment.starts_on, segment.ends_on
        self._selected_segment = None
        self._edit_coverage = coverage
        self._configure_dates(coverage, first, last)
        self._configure_grade_dates(coverage)
        self._ledger.clearSelection()
        self._edit_mode.setText("새 기간 지정 · 입력한 날짜에 센터 중식 기간을 추가합니다")
        self._apply.setText("새 기간을 표에 반영")
        self._apply.setEnabled(True)
        self._hide_error()

    def _configure_dates(
        self,
        coverage: InclusiveDateRange,
        starts_on: date,
        ends_on: date,
    ) -> None:
        assert coverage.starts_on is not None and coverage.ends_on is not None
        minimum = QDate(
            coverage.starts_on.year,
            coverage.starts_on.month,
            coverage.starts_on.day,
        )
        maximum = QDate(
            coverage.ends_on.year,
            coverage.ends_on.month,
            coverage.ends_on.day,
        )
        for editor in (self._starts_on, self._ends_on):
            editor.setMinimumDate(minimum)
            editor.setMaximumDate(maximum)
        self._starts_on.setDate(QDate(starts_on.year, starts_on.month, starts_on.day))
        self._ends_on.setDate(QDate(ends_on.year, ends_on.month, ends_on.day))

    def _configure_grade_dates(self, coverage):
        self._grade_day.setMinimumDate(QDate(coverage.starts_on.year, coverage.starts_on.month, coverage.starts_on.day))
        self._grade_day.setMaximumDate(QDate(coverage.ends_on.year, coverage.ends_on.month, coverage.ends_on.day))
        self._load_grade_day()

    def _load_grade_day(self, *_args):
        schedule = self._current_schedule()
        if schedule is None:
            return
        day = _python_date(self._grade_day)
        item = next((item for item in schedule.days if item.service_on == day), None)
        self._grades.set_grades(item.rule.grades_without_school_meal if item else ())

    def _apply_grade_day(self):
        schedule = self._current_schedule()
        if schedule is None:
            return
        day = _python_date(self._grade_day)
        if day.weekday() >= 5:
            self._show_error("학년 예외를 지정할 평일을 선택해주세요.")
            return
        try:
            updated = apply_school_lunch_rule_period(schedule, InclusiveDateRange(day, day),
                LunchDayRule(SchoolLunchMark.SCHOOL_PROVIDES, self._grades.selected_grades()))
        except (TypeError, ValueError) as error:
            self._show_error(user_error(error))
            return
        self._edited[schedule.normalized_school_name] = updated
        self._hide_error()
        self._populate_schools()
        self._fill_schedule()

    def _apply_edit(self) -> None:
        schedule = self._current_schedule()
        if schedule is None or self._edit_coverage is None:
            self._show_error("수정할 학교 구간을 먼저 선택해주세요.")
            return
        try:
            period = InclusiveDateRange(
                _python_date(self._starts_on),
                _python_date(self._ends_on),
            )
            if period.starts_on < self._edit_coverage.starts_on or period.ends_on > self._edit_coverage.ends_on:
                raise ValueError("선택한 방학 범위 안에서 날짜를 입력해주세요.")
            days = []
            for item in schedule.days:
                rule = item.rule
                selected = self._selected_segment
                was_inside = selected is not None and selected.starts_on <= item.service_on <= selected.ends_on
                if was_inside and not period.contains(item.service_on) and not rule.grades_without_school_meal:
                    rule = LunchDayRule(SchoolLunchMark.SCHOOL_PROVIDES)
                elif period.contains(item.service_on) and not was_inside:
                    # Keep independently configured single-day grade exceptions.
                    if not rule.grades_without_school_meal:
                        rule = LunchDayRule(SchoolLunchMark.VENDOR_REQUIRED)
                days.append(replace(item, rule=rule))
            updated = replace(schedule, days=tuple(days))
        except (TypeError, ValueError) as error:
            self._show_error(user_error(error))
            return
        key = schedule.normalized_school_name
        self._edited[key] = updated
        self._hide_error()
        self._populate_schools()
        self._fill_schedule(next((index for index, item in enumerate(self._filtered_segments(updated))
                                  if item.starts_on <= period.starts_on <= item.ends_on), 0))

    def _restore_school(self) -> None:
        key = self._current_key()
        if key is None:
            return
        self._edited[key] = self._original[key]
        self._hide_error()
        self._populate_schools()
        self._fill_schedule()

    def _refresh_summary(self) -> None:
        school_count = self.changed_school_count()
        weekday_count = self.changed_weekday_count()
        if school_count:
            self._summary.setText(
                f"저장 대기 · {school_count}개 학교, {weekday_count}개 평일이 달라졌습니다. "
                "아래 ‘변경 일정 저장’을 눌러야 저장 내용과 자동 백업에 반영됩니다."
            )
            self._summary.setProperty("changed", True)
        else:
            self._summary.setText(
                f"현재 저장된 데이터의 저장 일정 · {len(self._school_order)}개 학교. "
                "학교와 구간을 선택해 시작일·종료일·급식 제공 여부를 고칠 수 있습니다."
            )
            self._summary.setProperty("changed", False)
        self._save.setEnabled(bool(school_count))
        _repolish(self._summary)

    def _request_import(self) -> None:
        if self.changed_school_count():
            answer = self._message_box.question(
                self,
                "저장하지 않은 수정을 버릴까요?",
                "지금 표에서 바꾼 내용은 저장되지 않습니다. Excel에서 다시 가져오시겠습니까?",
                self._message_box.StandardButton.Yes
                | self._message_box.StandardButton.No,
                self._message_box.StandardButton.No,
            )
            if answer != self._message_box.StandardButton.Yes:
                return
        self._import_requested = True
        self.reject()

    def _request_delete_import(self) -> None:
        entry = self._selected_import()
        if entry is None:
            return
        if self.changed_school_count():
            self._show_error("수정 중인 학교 일정이 있습니다. 먼저 변경 일정을 저장하거나 창을 닫고 다시 열어 삭제해주세요.")
            return
        school_count = len({normalize_school_name(row.school_name) for row in entry.rows})
        answer = self._message_box.question(self, "불러온 학교 일정 전체를 삭제할까요?",
            f"{entry.label}\n\n이 파일에서 불러온 {school_count}개 학교의 일정을 한 번에 삭제합니다. "
            "검색 결과나 선택한 학교만 삭제하는 것이 아닙니다.\n"
            "다른 불러오기 기록은 유지하며, 겹치는 날짜는 남은 기록의 일정으로 돌아갑니다. "
            "이 기록으로 자동 입력한 중식 날짜도 이후 수정하지 않았다면 되돌립니다.\n"
            "삭제는 변경 이력에 남으며 이전 자료로 되돌리기에서 복구할 수 있습니다.",
            self._message_box.StandardButton.Yes | self._message_box.StandardButton.No,
            self._message_box.StandardButton.No)
        if answer == self._message_box.StandardButton.Yes:
            self.delete_import_requested = entry.revision_id
            self.accept()

    def _save_changes(self) -> None:
        if not self.changed_school_count():
            self._show_error("저장할 학교 일정 변경이 없습니다.")
            return
        self.accept()

    def _current_key(self) -> str | None:
        row = self._schools.currentRow() if hasattr(self, "_schools") else -1
        if row < 0 or row >= len(self._school_order):
            return None
        return self._school_order[row]

    def _current_schedule(self) -> SchoolLunchSchoolSchedule | None:
        key = self._current_key()
        return None if key is None else self._edited[key]

    def _show_error(self, message: str) -> None:
        self._error.setText(message)
        self._error.show()

    def _hide_error(self) -> None:
        self._error.clear()
        self._error.hide()


class SchoolCalendarEditStatus(StrEnum):
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"
    IMPORT_REQUESTED = "import_requested"


@dataclass(frozen=True, slots=True)
class SchoolCalendarEditOutcome:
    status: SchoolCalendarEditStatus
    roster: RosterSnapshot | None = None
    calendar: SchoolLunchCalendar | None = None
    backup_result: AutomaticBackupResult | None = None

    @property
    def completed(self) -> bool:
        return self.status is SchoolCalendarEditStatus.COMPLETED


class SchoolCalendarEditController:
    """Load the latest stored calendar and commit one reviewed UI revision."""

    def __init__(
        self,
        session: DatabaseSession,
        *,
        actor_provider: Callable[[], RevisionActor] | None = None,
        dialog_type=SchoolCalendarEditDialog,
        message_box=QMessageBox,
        recorded_at_provider: Callable[[], datetime] | None = None,
        revision_id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        if not isinstance(session, DatabaseSession):
            raise TypeError("session은 DatabaseSession이어야 합니다.")
        self._session = session
        self._actor_provider = actor_provider or current_windows_actor
        self._dialog_type = dialog_type
        self._message_box = message_box
        self._recorded_at_provider = recorded_at_provider or (
            lambda: datetime.now(UTC)
        )
        self._revision_id_factory = revision_id_factory

    @dialog_workflow
    def run(self, parent: QWidget | None = None) -> SchoolCalendarEditOutcome:
        roster = self._session.database.current_roster()
        latest = self._session.database.latest_revision()
        if roster is None or latest is None:
            self._message_box.critical(
                parent,
                "학교 일정을 열 수 없습니다",
                "현재 명단과 기준 변경 이력이 없습니다. Excel 명단 불러오기를 먼저 완료해주세요.",
            )
            return SchoolCalendarEditOutcome(SchoolCalendarEditStatus.FAILED)
        try:
            snapshot = self._session.database.snapshot(latest.metadata.revision_id)
            calendar = school_lunch_calendar_from_snapshot(snapshot)
        except Exception as error:
            self._message_box.critical(
                parent,
                "저장된 학교 일정을 읽지 못했습니다",
                user_error(error),
            )
            return SchoolCalendarEditOutcome(SchoolCalendarEditStatus.FAILED)
        if not calendar.schedules:
            return SchoolCalendarEditOutcome(
                SchoolCalendarEditStatus.IMPORT_REQUESTED,
                roster,
                calendar,
            )

        dialog = self._dialog_type(calendar, parent=parent)
        setter = getattr(dialog, "set_import_history", None)
        if callable(setter):
            from src.services.school_calendar_history import school_calendar_import_history
            try:
                setter(school_calendar_import_history(self._session.database, snapshot.revision_id))
            except Exception as error:
                self._message_box.critical(parent, "불러온 방학 기록을 읽지 못했습니다", user_error(error))
                return SchoolCalendarEditOutcome(SchoolCalendarEditStatus.FAILED)
        result = (yield DialogRequest(dialog, self._session))
        if dialog.import_requested:
            return SchoolCalendarEditOutcome(
                SchoolCalendarEditStatus.IMPORT_REQUESTED,
                roster,
                calendar,
            )
        if result != QDialog.DialogCode.Accepted:
            return SchoolCalendarEditOutcome(SchoolCalendarEditStatus.CANCELLED)
        saved = False
        try:
            requested_deletion = getattr(dialog, "delete_import_requested", None)
            if requested_deletion is not None:
                from src.services.school_calendar_delete import build_import_deletion
                prepared = build_import_deletion(snapshot,
                    self._session.database.branch_revision_chain(snapshot.revision_id), requested_deletion,
                    actor=self._actor_provider(), recorded_at=self._recorded_at_provider(),
                    revision_id=self._revision_id_factory())
            else:
                prepared = build_school_calendar_edit_revision(
                    snapshot, dialog.edited_schedules(), actor=self._actor_provider(),
                    recorded_at=self._recorded_at_provider(), revision_id=self._revision_id_factory())
            backup_result = save_revision(self._session, prepared.record, parent=parent)
            saved = True
            updated_roster = self._session.database.current_roster()
        except Exception as error:
            self._message_box.critical(
                parent,
                "학교 일정을 저장하지 못했습니다",
                ("일정은 저장됐지만 화면을 갱신하지 못했습니다. 프로그램을 다시 열어 확인해주세요.\n\n" if saved else "")
                + user_error(error),
            )
            return SchoolCalendarEditOutcome(SchoolCalendarEditStatus.FAILED)
        if updated_roster is None:
            self._message_box.critical(
                parent,
                "저장한 학교 일정을 표시하지 못했습니다",
                "일정은 저장됐지만 현재 화면을 다시 읽지 못했습니다. 프로그램을 다시 열어주세요.",
            )
            return SchoolCalendarEditOutcome(SchoolCalendarEditStatus.FAILED)
        if backup_result.warning_required:
            self._message_box.warning(
                parent,
                "학교 일정은 저장됐지만 자동 백업에 실패했습니다",
                "학교 일정과 변경 이력은 운영 데이터에 정상 저장되었습니다. "
                "백업 폴더 연결을 확인한 뒤 백업을 다시 실행해주세요.\n\n"
                + user_error(backup_result.error),
            )
        return SchoolCalendarEditOutcome(
            SchoolCalendarEditStatus.COMPLETED,
            updated_roster,
            prepared.calendar,
            backup_result,
        )


def _field_label(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("fieldLabel")
    return label


def _date_edit(value: date) -> WheelSafeDateEdit:
    editor = WheelSafeDateEdit(QDate(value.year, value.month, value.day))
    editor.setCalendarPopup(True)
    editor.setDisplayFormat("yyyy.MM.dd")
    editor.setObjectName("schoolCalendarDate")
    return editor


def _python_date(editor: WheelSafeDateEdit) -> date:
    value = editor.date()
    return date(value.year(), value.month(), value.day())


def _days_between(first, last):
    return (date.fromordinal(value) for value in range(first.toordinal() + 1, last.toordinal()))


def _grades(value: str) -> tuple[str, ...]:
    parts = tuple(
        part
        for part in re.split(r"[,/+·;\n]+", value.strip())
        if part.strip()
    )
    return tuple(dict.fromkeys(normalize_grade(part) for part in parts))


def _coverage_caption(schedule: SchoolLunchSchoolSchedule) -> str:
    return " · ".join(
        f"{period.starts_on:%Y.%m.%d}–{period.ends_on:%Y.%m.%d}"
        for period in schedule.covered_periods
    )


def _coverage_for(
    schedule: SchoolLunchSchoolSchedule,
    starts_on: date,
    ends_on: date,
) -> InclusiveDateRange:
    for coverage in schedule.covered_periods:
        if coverage.contains(starts_on) and coverage.contains(ends_on):
            return coverage
    raise ValueError("선택한 구간의 저장 검토 범위를 찾을 수 없습니다.")


def _changed_weekday_count(
    original: SchoolLunchSchoolSchedule,
    edited: SchoolLunchSchoolSchedule,
) -> int:
    original_rules = {item.service_on: item.rule for item in original.days}
    edited_rules = {item.service_on: item.rule for item in edited.days}
    return sum(
        original_rules[service_on] != edited_rules[service_on]
        for service_on in original_rules
    )


def _segment_row_for(
    schedule: SchoolLunchSchoolSchedule,
    period: InclusiveDateRange,
    rule: LunchDayRule,
) -> int:
    assert period.starts_on is not None and period.ends_on is not None
    for row, segment in enumerate(school_lunch_rule_segments(schedule)):
        overlaps = not (
            segment.ends_on < period.starts_on or segment.starts_on > period.ends_on
        )
        if overlaps and segment.rule == rule:
            return row
    return 0


def _weekdays(starts_on: date, ends_on: date):
    current = starts_on
    while current <= ends_on:
        if current.weekday() < 5:
            yield current
        current = date.fromordinal(current.toordinal() + 1)


def _repolish(widget: QWidget) -> None:
    widget.style().unpolish(widget)
    widget.style().polish(widget)
