"""Standalone editor for school schedules already stored in SQLite."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
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
    school_lunch_calendar_from_snapshot,
)
from src.domain.school_calendar_edits import (
    SchoolLunchRuleSegment,
    apply_school_lunch_rule_period,
    build_school_calendar_edit_revision,
    replace_school_lunch_rule_segment,
    school_lunch_rule_segments,
)
from src.storage.database_session import (
    AutomaticBackupResult,
    DatabaseSession,
)

from .date_edit import WheelSafeDateEdit
from .import_controller import current_windows_actor
from .theme import application_stylesheet, register_application_fonts


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
        self._import = QPushButton("Excel에서 새로 가져오기")
        self._import.setObjectName("quietAction")
        self._import.setToolTip("현재 편집 화면을 닫고 학교 일정 Excel 가져오기를 시작합니다")
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
        title = QLabel("저장된 학교 급식 운행표")
        title.setObjectName("workspaceTitle")
        subtitle = QLabel(
            "Excel 파일 없이 운영 데이터의 최신 일정을 엽니다. 표에서 기간을 고쳐도 저장 전까지 원본은 바뀌지 않습니다."
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
        self._coverage_text.setObjectName("fieldHelp")
        school_text.addWidget(self._school_title)
        school_text.addWidget(self._coverage_text)
        heading.addLayout(school_text, 1)
        self._new_period = QPushButton("선택 범위에 새 기간 지정")
        self._new_period.setObjectName("quietAction")
        self._new_period.clicked.connect(self._start_new_period)
        heading.addWidget(self._new_period)
        layout.addLayout(heading)

        self._ledger = QTableWidget(0, 7)
        self._ledger.setObjectName("schoolCalendarStoredLedger")
        self._ledger.setHorizontalHeaderLabels(
            ("시작일", "종료일", "O/X", "중식 처리", "학년 예외", "평일", "상태")
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
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.Fixed)
        header.resizeSection(6, 66)
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
        editor_layout.addWidget(_field_label("시작일"), 1, 0)
        editor_layout.addWidget(_field_label("종료일"), 1, 1)
        editor_layout.addWidget(_field_label("O/X"), 1, 2)
        editor_layout.addWidget(_field_label("학교 급식 없는 학년"), 1, 3, 1, 2)
        self._starts_on = _date_edit(date.today())
        self._ends_on = _date_edit(date.today())
        self._mark = _mark_combo(SchoolLunchMark.SCHOOL_PROVIDES)
        self._grades = QLineEdit()
        self._grades.setObjectName("schoolCalendarGrades")
        self._grades.setPlaceholderText("예: 6학년, 특수학급 (없으면 비움)")
        editor_layout.addWidget(self._starts_on, 2, 0)
        editor_layout.addWidget(self._ends_on, 2, 1)
        editor_layout.addWidget(self._mark, 2, 2)
        editor_layout.addWidget(self._grades, 2, 3, 1, 2)
        self._apply = QPushButton("표에 반영")
        self._apply.setObjectName("primaryAction")
        self._apply.clicked.connect(self._apply_edit)
        editor_layout.addWidget(self._apply, 2, 5)
        help_text = QLabel(
            "O = 학교가 중식 제공(센터 배송 없음) · X = 학교 미급식(센터 중식 배송). "
            "학년을 적으면 O인 날에도 해당 학년만 센터에서 배송합니다."
        )
        help_text.setObjectName("schoolCalendarRuleHelp")
        help_text.setWordWrap(True)
        editor_layout.addWidget(help_text, 3, 0, 1, 6)
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
                + "\n"
                + _coverage_caption(schedule)
            )
            item.setData(Qt.ItemDataRole.UserRole, key)
            if changed:
                item.setForeground(QColor("#0F6F7E"))
            self._schools.addItem(item)
        self._schools.blockSignals(False)
        if current_key in self._school_order:
            self._schools.setCurrentRow(self._school_order.index(current_key))
        self._refresh_summary()

    def _school_changed(self, row: int) -> None:
        if row < 0 or row >= len(self._school_order):
            return
        self._selected_segment = None
        self._edit_coverage = None
        self._fill_schedule()

    def _fill_schedule(self, select_row: int = 0) -> None:
        schedule = self._current_schedule()
        if schedule is None:
            return
        self._school_title.setText(schedule.school_name)
        self._coverage_text.setText("저장 검토 범위  " + _coverage_caption(schedule))
        self._visible_segments = school_lunch_rule_segments(schedule)
        original = self._original[schedule.normalized_school_name]
        original_rules = {item.service_on: item.rule for item in original.days}
        current_rules = {item.service_on: item.rule for item in schedule.days}
        self._ledger.blockSignals(True)
        self._ledger.setRowCount(len(self._visible_segments))
        for row, segment in enumerate(self._visible_segments):
            grades = ", ".join(sorted(segment.rule.grades_without_school_meal)) or "—"
            values = (
                segment.starts_on.strftime("%Y.%m.%d"),
                segment.ends_on.strftime("%Y.%m.%d"),
                segment.rule.mark.value,
                (
                    "학교 급식"
                    if segment.rule.mark is SchoolLunchMark.SCHOOL_PROVIDES
                    else "센터 중식 배송"
                ),
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
            self._ledger.setItem(row, 6, status)
        self._ledger.blockSignals(False)
        if self._visible_segments:
            target = min(max(select_row, 0), len(self._visible_segments) - 1)
            self._ledger.selectRow(target)
            self._segment_selected(target, 0, -1, -1)
        else:
            self._edit_mode.setText("이 검토 범위에는 평일이 없습니다")
            self._apply.setEnabled(False)
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
        self._selected_segment = segment
        self._edit_coverage = coverage
        self._configure_dates(coverage, segment.starts_on, segment.ends_on)
        self._set_rule(segment.rule)
        self._edit_mode.setText("선택 구간 수정 · 앞뒤 날짜는 자동으로 이어집니다")
        self._apply.setText("수정 내용을 표에 반영")
        self._apply.setEnabled(True)
        self._hide_error()

    def _start_new_period(self) -> None:
        schedule = self._current_schedule()
        row = self._ledger.currentRow()
        if schedule is None or row < 0 or row >= len(self._visible_segments):
            self._show_error("먼저 기준으로 삼을 저장 구간을 선택해주세요.")
            return
        segment = self._visible_segments[row]
        coverage = _coverage_for(schedule, segment.starts_on, segment.ends_on)
        self._selected_segment = None
        self._edit_coverage = coverage
        self._configure_dates(coverage, segment.starts_on, segment.ends_on)
        self._set_rule(segment.rule)
        self._ledger.clearSelection()
        self._edit_mode.setText("새 기간 지정 · 입력한 날짜만 선택한 O/X로 덮어씁니다")
        self._apply.setText("새 기간을 표에 반영")
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

    def _set_rule(self, rule: LunchDayRule) -> None:
        self._mark.setCurrentIndex(self._mark.findData(rule.mark.value))
        self._grades.setText(", ".join(sorted(rule.grades_without_school_meal)))

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
            rule = LunchDayRule(
                SchoolLunchMark(str(self._mark.currentData())),
                frozenset(_grades(self._grades.text())),
            )
            if self._selected_segment is None:
                updated = apply_school_lunch_rule_period(schedule, period, rule)
            else:
                updated = replace_school_lunch_rule_segment(
                    schedule,
                    self._selected_segment,
                    period,
                    rule,
                )
        except (TypeError, ValueError) as error:
            self._show_error(str(error))
            return
        key = schedule.normalized_school_name
        self._edited[key] = updated
        self._hide_error()
        self._populate_schools()
        self._fill_schedule(_segment_row_for(updated, period, rule))

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
                "학교와 구간을 선택해 시작일·종료일·O/X를 고칠 수 있습니다."
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

    def run(self, parent: QWidget | None = None) -> SchoolCalendarEditOutcome:
        roster = self._session.database.current_roster()
        latest = self._session.database.latest_revision()
        if roster is None or latest is None:
            self._message_box.critical(
                parent,
                "학교 일정을 열 수 없습니다",
                "현재 명단과 기준 변경 이력이 없습니다. Excel 명단 가져오기를 먼저 완료해주세요.",
            )
            return SchoolCalendarEditOutcome(SchoolCalendarEditStatus.FAILED)
        try:
            snapshot = self._session.database.snapshot(latest.metadata.revision_id)
            calendar = school_lunch_calendar_from_snapshot(snapshot)
        except Exception as error:
            self._message_box.critical(
                parent,
                "저장된 학교 일정을 읽지 못했습니다",
                str(error),
            )
            return SchoolCalendarEditOutcome(SchoolCalendarEditStatus.FAILED)
        if not calendar.schedules:
            return SchoolCalendarEditOutcome(
                SchoolCalendarEditStatus.IMPORT_REQUESTED,
                roster,
                calendar,
            )

        dialog = self._dialog_type(calendar, parent=parent)
        result = dialog.exec()
        if dialog.import_requested:
            return SchoolCalendarEditOutcome(
                SchoolCalendarEditStatus.IMPORT_REQUESTED,
                roster,
                calendar,
            )
        if result != QDialog.DialogCode.Accepted:
            return SchoolCalendarEditOutcome(SchoolCalendarEditStatus.CANCELLED)
        try:
            prepared = build_school_calendar_edit_revision(
                snapshot,
                dialog.edited_schedules(),
                actor=self._actor_provider(),
                recorded_at=self._recorded_at_provider(),
                revision_id=self._revision_id_factory(),
            )
            backup_result = self._session.append_revision(prepared.record)
            updated_roster = self._session.database.current_roster()
        except Exception as error:
            self._message_box.critical(
                parent,
                "학교 일정을 저장하지 못했습니다",
                "운영 데이터에는 반영하지 않았습니다. 수정 날짜와 O/X를 확인한 뒤 다시 시도해주세요.\n\n"
                + str(error),
            )
            return SchoolCalendarEditOutcome(SchoolCalendarEditStatus.FAILED)
        if updated_roster is None:
            self._message_box.critical(
                parent,
                "저장한 학교 일정을 표시하지 못했습니다",
                "일정은 저장됐지만 현재 화면을 다시 읽지 못했습니다. 프로그램을 다시 열어주세요.",
            )
            return SchoolCalendarEditOutcome(SchoolCalendarEditStatus.FAILED)
        if backup_result.error is not None:
            self._message_box.warning(
                parent,
                "학교 일정은 저장됐지만 자동 백업에 실패했습니다",
                "학교 일정과 변경 이력은 운영 데이터에 정상 저장되었습니다. "
                "백업 폴더 연결을 확인한 뒤 백업을 다시 실행해주세요.\n\n"
                + str(backup_result.error),
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


def _mark_combo(selected: SchoolLunchMark) -> QComboBox:
    combo = QComboBox()
    combo.setObjectName("schoolCalendarMark")
    combo.addItem("O · 학교 급식", SchoolLunchMark.SCHOOL_PROVIDES.value)
    combo.addItem("X · 센터 배송", SchoolLunchMark.VENDOR_REQUIRED.value)
    combo.setCurrentIndex(combo.findData(selected.value))
    return combo


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
