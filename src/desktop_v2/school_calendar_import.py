"""Guided school-calendar Excel import, editable ledger review, and commit."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from uuid import UUID, uuid4

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDateEdit,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.domain.entitlements import SchoolLunchMark
from src.domain.revisions import RevisionActor
from src.domain.roster import InclusiveDateRange, RosterSnapshot
from src.domain.school_calendar import SchoolLunchCalendar, normalize_school_name
from src.importing.school_calendar import (
    SchoolCalendarColumnMapping,
    SchoolCalendarImportPreview,
    SchoolCalendarNoteAction,
    SchoolCalendarNoteCandidate,
    SchoolCalendarNoteDecision,
    SchoolCalendarWorkbookInspection,
    SchoolVacationRow,
    build_school_calendar_import_revision,
    build_school_lunch_schedules,
    inspect_school_calendar_workbook,
    parse_school_calendar_workbook,
)
from src.importing.workbook import (
    WorkbookImportPasswordError,
    WorkbookImportPasswordRequiredError,
)
from src.storage import AutomaticBackupResult, DatabaseSession

from .import_controller import current_windows_actor
from .theme import application_stylesheet, register_application_fonts


class SchoolCalendarImportDialog(QDialog):
    """Pick a workbook, auto-map its columns, and preview typed school rows."""

    def __init__(
        self,
        *,
        initial_directory: str | Path,
        calendar_year: int,
        parent: QWidget | None = None,
        inspection_loader=inspect_school_calendar_workbook,
        preview_loader=parse_school_calendar_workbook,
        file_dialog=QFileDialog,
    ) -> None:
        super().__init__(parent)
        self._initial_directory = Path(initial_directory)
        self._inspection_loader = inspection_loader
        self._preview_loader = preview_loader
        self._file_dialog = file_dialog
        self._source_path: Path | None = None
        self._inspection: SchoolCalendarWorkbookInspection | None = None
        self._preview: SchoolCalendarImportPreview | None = None
        self._mapping_combos: dict[str, QComboBox] = {}
        register_application_fonts()
        self.setObjectName("schoolCalendarImportDialog")
        self.setWindowTitle("학교 일정 Excel 가져오기")
        self.setMinimumSize(1020, 760)
        self.resize(1120, 820)
        self.setStyleSheet(application_stylesheet())

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 20)
        root.setSpacing(12)
        root.addWidget(self._header())
        root.addWidget(self._source_panel())
        root.addWidget(self._mapping_panel())

        summary_row = QHBoxLayout()
        self._mapping_status = QLabel("Excel 파일을 선택하면 열을 자동으로 찾습니다.")
        self._mapping_status.setObjectName("schoolCalendarMappingStatus")
        self._mapping_status.setWordWrap(True)
        summary_row.addWidget(self._mapping_status, 1)
        refresh = QPushButton("선택한 열로 다시 읽기")
        refresh.setObjectName("quietAction")
        refresh.clicked.connect(self._read_preview)
        summary_row.addWidget(refresh)
        root.addLayout(summary_row)

        self._preview_table = QTableWidget(0, 7)
        self._preview_table.setObjectName("schoolCalendarPreviewTable")
        self._preview_table.setHorizontalHeaderLabels(
            ("원본 행", "학교", "시작일", "O/X", "종료일", "O/X", "비고")
        )
        self._preview_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._preview_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._preview_table.setAlternatingRowColors(True)
        self._preview_table.verticalHeader().setVisible(False)
        header = self._preview_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        root.addWidget(self._preview_table, 1)

        self._error = QLabel()
        self._error.setObjectName("errorMessage")
        self._error.setWordWrap(True)
        self._error.hide()
        root.addWidget(self._error)

        actions = QHBoxLayout()
        actions.addStretch(1)
        cancel = QPushButton("취소")
        cancel.setObjectName("quietAction")
        cancel.clicked.connect(self.reject)
        actions.addWidget(cancel)
        self._continue = QPushButton("학교별 일정 검토")
        self._continue.setObjectName("primaryAction")
        self._continue.setEnabled(False)
        self._continue.clicked.connect(self._accept_preview)
        actions.addWidget(self._continue)
        root.addLayout(actions)

        self._year.setValue(calendar_year)

    def _header(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("schoolCalendarHeader")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(18, 15, 18, 15)
        eyebrow = QLabel("학교별 중식 일정")
        eyebrow.setObjectName("editEyebrow")
        title = QLabel("학교 급식 운행표 가져오기")
        title.setObjectName("workspaceTitle")
        subtitle = QLabel(
            "학교명·시작일·종료일·양 끝 O/X를 자동으로 읽습니다. "
            "자동 탐지가 애매할 때만 아래 열 선택을 바꾸세요."
        )
        subtitle.setObjectName("workspaceSubtitle")
        subtitle.setWordWrap(True)
        layout.addWidget(eyebrow)
        layout.addWidget(title)
        layout.addWidget(subtitle)
        return frame

    def _source_panel(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("schoolCalendarSource")
        layout = QGridLayout(frame)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setHorizontalSpacing(9)
        layout.setVerticalSpacing(8)
        layout.addWidget(_field_label("학교 일정 Excel"), 0, 0)
        self._path = QLineEdit()
        self._path.setReadOnly(True)
        self._path.setPlaceholderText(".xlsx 또는 .xlsm 파일을 선택하세요")
        layout.addWidget(self._path, 0, 1, 1, 5)
        browse = QPushButton("파일 선택")
        browse.setObjectName("primaryAction")
        browse.clicked.connect(self._browse)
        layout.addWidget(browse, 0, 6)

        layout.addWidget(_field_label("파일 비밀번호"), 1, 0)
        self._password = QLineEdit()
        self._password.setEchoMode(QLineEdit.EchoMode.Password)
        self._password.setPlaceholderText("암호가 없는 파일이면 비워두세요")
        layout.addWidget(self._password, 1, 1, 1, 2)
        retry = QPushButton("파일 다시 읽기")
        retry.setObjectName("quietAction")
        retry.clicked.connect(self._read_structure)
        layout.addWidget(retry, 1, 3)

        layout.addWidget(_field_label("일정 연도"), 1, 4)
        self._year = QSpinBox()
        self._year.setRange(2000, 2200)
        self._year.setSuffix("년")
        layout.addWidget(self._year, 1, 5)
        self._sheet = QComboBox()
        self._sheet.setEnabled(False)
        self._sheet.currentTextChanged.connect(self._sheet_changed)
        layout.addWidget(self._sheet, 1, 6)
        return frame

    def _mapping_panel(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("schoolCalendarMapping")
        layout = QGridLayout(frame)
        layout.setContentsMargins(14, 11, 14, 11)
        layout.setHorizontalSpacing(10)
        labels = (
            ("school", "학교명 열"),
            ("starts_on", "시작일 열"),
            ("starts_mark", "시작일 O/X"),
            ("ends_on", "종료일 열"),
            ("ends_mark", "종료일 O/X"),
            ("note", "비고 열(선택)"),
        )
        for index, (key, label) in enumerate(labels):
            column = index % 3
            row = (index // 3) * 2
            layout.addWidget(_field_label(label), row, column)
            combo = QComboBox()
            combo.setMinimumWidth(180)
            self._mapping_combos[key] = combo
            layout.addWidget(combo, row + 1, column)
        return frame

    def _browse(self) -> None:
        filename, _filter = self._file_dialog.getOpenFileName(
            self,
            "학교 일정 Excel 선택",
            str(self._initial_directory),
            "Excel 파일 (*.xlsx *.xlsm)",
        )
        if filename:
            self.load_file(filename)

    def load_file(self, path: str | Path) -> None:
        self._source_path = Path(path).resolve()
        self._path.setText(str(self._source_path))
        self._read_structure()

    def _read_structure(self) -> None:
        if self._source_path is None:
            self._show_error("학교 일정 Excel 파일을 먼저 선택해주세요.")
            return
        selected_sheet = self._sheet.currentText() or None
        try:
            inspection = self._inspection_loader(
                self._source_path,
                password=self._password.text(),
                sheet_name=selected_sheet,
                header_row=2,
                data_start_row=3,
                calendar_year=self._year.value(),
            )
        except WorkbookImportPasswordRequiredError:
            self._show_error("암호로 보호된 Excel입니다. 파일 비밀번호를 입력하고 다시 읽어주세요.")
            return
        except WorkbookImportPasswordError:
            self._show_error("파일 비밀번호가 맞지 않습니다. 다시 입력해주세요.")
            return
        except (OSError, TypeError, ValueError) as error:
            self._show_error(str(error))
            return
        self._inspection = inspection
        self._preview = None
        self._continue.setEnabled(False)
        self._populate_sheet_names(inspection)
        self._populate_column_choices(inspection)
        if inspection.proposed_mapping is None:
            self._mapping_status.setText(
                "자동으로 확정하지 못한 열이 있습니다. 각 역할의 열을 한 번 선택한 뒤 "
                "‘선택한 열로 다시 읽기’를 누르세요."
            )
            self._mapping_status.setProperty("outcome", "review")
            _repolish(self._mapping_status)
            self._error.hide()
            return
        self._apply_mapping(inspection.proposed_mapping)
        self._mapping_status.setText(
            "열을 자동으로 찾았습니다. 아래 미리보기에서 학교·날짜·O/X를 확인하세요."
        )
        self._mapping_status.setProperty("outcome", "ready")
        _repolish(self._mapping_status)
        self._read_preview()

    def _populate_sheet_names(self, inspection: SchoolCalendarWorkbookInspection) -> None:
        self._sheet.blockSignals(True)
        self._sheet.clear()
        self._sheet.addItems(inspection.sheet_names)
        self._sheet.setCurrentText(inspection.selected_sheet)
        self._sheet.setEnabled(True)
        self._sheet.blockSignals(False)

    def _populate_column_choices(self, inspection: SchoolCalendarWorkbookInspection) -> None:
        for key, combo in self._mapping_combos.items():
            combo.clear()
            if key == "note":
                combo.addItem("사용 안 함", None)
            else:
                combo.addItem("열 선택", None)
            for column in inspection.columns:
                combo.addItem(column.label, column.index)

    def _apply_mapping(self, mapping: SchoolCalendarColumnMapping) -> None:
        values = {
            "school": mapping.school_column,
            "starts_on": mapping.starts_on_column,
            "starts_mark": mapping.starts_on_mark_column,
            "ends_on": mapping.ends_on_column,
            "ends_mark": mapping.ends_on_mark_column,
            "note": mapping.note_column,
        }
        for key, value in values.items():
            combo = self._mapping_combos[key]
            index = combo.findData(value)
            combo.setCurrentIndex(max(0, index))

    def _mapping(self) -> SchoolCalendarColumnMapping:
        if self._inspection is None:
            raise ValueError("Excel 구조를 먼저 읽어야 합니다.")
        values = {key: combo.currentData() for key, combo in self._mapping_combos.items()}
        missing = [
            label
            for key, label in (
                ("school", "학교명"),
                ("starts_on", "시작일"),
                ("starts_mark", "시작일 O/X"),
                ("ends_on", "종료일"),
                ("ends_mark", "종료일 O/X"),
            )
            if values[key] is None
        ]
        if missing:
            raise ValueError("열을 선택해주세요: " + ", ".join(missing))
        return SchoolCalendarColumnMapping(
            self._inspection.selected_sheet,
            2,
            3,
            self._year.value(),
            int(values["school"]),
            int(values["starts_on"]),
            int(values["starts_mark"]),
            int(values["ends_on"]),
            int(values["ends_mark"]),
            None if values["note"] is None else int(values["note"]),
        )

    def _read_preview(self) -> None:
        if self._source_path is None:
            self._show_error("학교 일정 Excel 파일을 먼저 선택해주세요.")
            return
        try:
            preview = self._preview_loader(
                self._source_path,
                self._mapping(),
                password=self._password.text(),
            )
        except (OSError, TypeError, ValueError) as error:
            self._show_error(str(error))
            return
        self._preview = preview
        self._fill_preview(preview)
        if preview.issues:
            details = "\n".join(
                f"• {issue.location}: {issue.message}" for issue in preview.issues[:8]
            )
            self._show_error(details)
            return
        self._error.hide()
        self._continue.setEnabled(preview.can_continue)
        self._mapping_status.setText(
            f"학교 {preview.school_count}곳 · 원본 {len(preview.rows)}행 · "
            f"비고 검토 {len(preview.note_candidates)}건을 읽었습니다."
        )
        self._mapping_status.setProperty("outcome", "ready")
        _repolish(self._mapping_status)

    def _fill_preview(self, preview: SchoolCalendarImportPreview) -> None:
        self._preview_table.setRowCount(len(preview.rows))
        for row_index, row in enumerate(preview.rows):
            values = (
                str(row.row_number),
                row.school_name,
                row.starts_on.strftime("%Y.%m.%d"),
                row.starts_on_mark.value,
                row.ends_on.strftime("%Y.%m.%d"),
                row.ends_on_mark.value,
                row.note or "",
            )
            for column, value in enumerate(values):
                self._preview_table.setItem(row_index, column, QTableWidgetItem(value))

    def _sheet_changed(self, text: str) -> None:
        if self._inspection is not None and text != self._inspection.selected_sheet:
            self._read_structure()

    def _show_error(self, message: str) -> None:
        self._preview = None
        self._continue.setEnabled(False)
        self._error.setText(message)
        self._error.show()

    def _accept_preview(self) -> None:
        if self._preview is None or not self._preview.can_continue:
            self._show_error("오류가 없는 학교 일정 미리보기를 먼저 만들어주세요.")
            return
        self.accept()

    def preview(self) -> SchoolCalendarImportPreview:
        if self.result() != QDialog.DialogCode.Accepted or self._preview is None:
            raise RuntimeError("확정된 학교 일정 미리보기가 없습니다.")
        return self._preview


@dataclass(slots=True)
class _RowEditors:
    source_row: int
    school: QTableWidgetItem
    starts_on: QDateEdit
    starts_mark: QComboBox
    ends_on: QDateEdit
    ends_mark: QComboBox
    note: str | None


@dataclass(slots=True)
class _NoteEditors:
    candidate: SchoolCalendarNoteCandidate
    action: QComboBox
    starts_on: QDateEdit
    ends_on: QDateEdit
    grades: QLineEdit
    fields: QFrame


class SchoolCalendarReviewDialog(QDialog):
    """Editable per-school timetable and explicit natural-language review."""

    def __init__(
        self,
        preview: SchoolCalendarImportPreview,
        *,
        known_school_names: Sequence[str] = (),
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        if not isinstance(preview, SchoolCalendarImportPreview) or not preview.can_continue:
            raise ValueError("검토할 수 있는 학교 일정 미리보기가 필요합니다.")
        self._preview = preview
        self._known_school_names = tuple(
            name.strip() for name in known_school_names if isinstance(name, str) and name.strip()
        )
        self._row_editors: list[_RowEditors] = []
        self._note_editors: dict[UUID, _NoteEditors] = {}
        self._reviewed_preview: SchoolCalendarImportPreview | None = None
        self._reviewed_decisions: Mapping[UUID, SchoolCalendarNoteDecision] | None = None
        register_application_fonts()
        self.setObjectName("schoolCalendarReviewDialog")
        self.setWindowTitle("학교별 일정 검토")
        self.setMinimumSize(1120, 780)
        self.resize(1220, 880)
        self.setStyleSheet(application_stylesheet())

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 20)
        root.setSpacing(11)
        root.addWidget(self._header())
        root.addWidget(self._coverage_panel())

        self._table = QTableWidget(0, 7)
        self._table.setObjectName("schoolCalendarLedger")
        self._table.setHorizontalHeaderLabels(
            ("원본 행", "학교명", "시작일", "시작 O/X", "종료일", "종료 O/X", "원본 비고")
        )
        self._table.setAlternatingRowColors(True)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self._table.horizontalHeader().setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        self._fill_rows()
        root.addWidget(self._table, 3)

        note_header = QHBoxLayout()
        note_header.addWidget(_section_label("비고 검토 · 자동 확정하지 않음"))
        note_header.addStretch(1)
        if preview.note_candidates:
            ignore_all = QPushButton("비고 모두 반영 안 함")
            ignore_all.setObjectName("quietAction")
            ignore_all.clicked.connect(self._ignore_all_notes)
            note_header.addWidget(ignore_all)
        root.addLayout(note_header)
        root.addWidget(self._note_area(), 2)

        self._error = QLabel()
        self._error.setObjectName("errorMessage")
        self._error.setWordWrap(True)
        self._error.hide()
        root.addWidget(self._error)
        self._summary = QLabel()
        self._summary.setObjectName("schoolCalendarReviewSummary")
        self._summary.setWordWrap(True)
        root.addWidget(self._summary)

        actions = QHBoxLayout()
        actions.addStretch(1)
        cancel = QPushButton("취소")
        cancel.setObjectName("quietAction")
        cancel.clicked.connect(self.reject)
        actions.addWidget(cancel)
        self._save = QPushButton("학교 일정 운영 데이터에 저장")
        self._save.setObjectName("primaryAction")
        self._save.clicked.connect(self._accept_review)
        actions.addWidget(self._save)
        root.addLayout(actions)

        self._table.itemChanged.connect(self._update_validation)
        self._coverage_starts.dateChanged.connect(self._update_validation)
        self._coverage_ends.dateChanged.connect(self._update_validation)
        self._update_validation()

    def _header(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("schoolCalendarReviewHeader")
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(18, 14, 18, 14)
        stamp = QLabel("O / X")
        stamp.setObjectName("schoolCalendarStamp")
        stamp.setAlignment(Qt.AlignmentFlag.AlignCenter)
        stamp.setMinimumSize(74, 52)
        layout.addWidget(stamp)
        copy = QVBoxLayout()
        eyebrow = QLabel("확인한 급식 지원 일정")
        eyebrow.setObjectName("editEyebrow")
        title = QLabel("학교별 시작일·종료일을 확인하세요")
        title.setObjectName("workspaceTitle")
        subtitle = QLabel(
            "학교명·날짜·O/X는 표에서 바로 수정할 수 있습니다. "
            "O는 학교 급식, X는 업체 배송입니다."
        )
        subtitle.setObjectName("workspaceSubtitle")
        copy.addWidget(eyebrow)
        copy.addWidget(title)
        copy.addWidget(subtitle)
        layout.addLayout(copy, 1)
        return frame

    def _coverage_panel(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("schoolCalendarCoverage")
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.addWidget(_field_label("이번 파일로 검토 완료할 전체 기간"))
        self._coverage_starts = _date_edit(self._preview.coverage.starts_on)
        self._coverage_ends = _date_edit(self._preview.coverage.ends_on)
        layout.addWidget(self._coverage_starts)
        layout.addWidget(QLabel("부터"))
        layout.addWidget(self._coverage_ends)
        layout.addWidget(QLabel("까지"))
        layout.addStretch(1)
        self._school_match = QLabel(self._school_match_text())
        self._school_match.setObjectName("muted")
        layout.addWidget(self._school_match)
        return frame

    def _fill_rows(self) -> None:
        self._table.blockSignals(True)
        self._table.setRowCount(len(self._preview.rows))
        for row_index, row in enumerate(self._preview.rows):
            source = QTableWidgetItem(str(row.row_number))
            source.setFlags(source.flags() & ~Qt.ItemFlag.ItemIsEditable)
            school = QTableWidgetItem(row.school_name)
            starts_on = _date_edit(row.starts_on)
            starts_mark = _mark_combo(row.starts_on_mark)
            ends_on = _date_edit(row.ends_on)
            ends_mark = _mark_combo(row.ends_on_mark)
            note = QTableWidgetItem(row.note or "")
            note.setFlags(note.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self._table.setItem(row_index, 0, source)
            self._table.setItem(row_index, 1, school)
            self._table.setCellWidget(row_index, 2, starts_on)
            self._table.setCellWidget(row_index, 3, starts_mark)
            self._table.setCellWidget(row_index, 4, ends_on)
            self._table.setCellWidget(row_index, 5, ends_mark)
            self._table.setItem(row_index, 6, note)
            editors = _RowEditors(
                row.row_number,
                school,
                starts_on,
                starts_mark,
                ends_on,
                ends_mark,
                row.note,
            )
            self._row_editors.append(editors)
            starts_on.dateChanged.connect(self._update_validation)
            starts_mark.currentIndexChanged.connect(self._update_validation)
            ends_on.dateChanged.connect(self._update_validation)
            ends_mark.currentIndexChanged.connect(self._update_validation)
        self._table.blockSignals(False)

    def _note_area(self) -> QWidget:
        if not self._preview.note_candidates:
            label = QLabel("자연어 비고가 없습니다. 학교별 표만 확인하면 저장할 수 있습니다.")
            label.setObjectName("schoolCalendarNoNotes")
            return label
        scroll = QScrollArea()
        scroll.setObjectName("schoolCalendarNotes")
        scroll.setWidgetResizable(True)
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(8)
        row_by_source = {row.row_number: row for row in self._preview.rows}
        for candidate in self._preview.note_candidates:
            source_row = row_by_source[candidate.row_number]
            card = QFrame()
            card.setObjectName("schoolCalendarNoteCard")
            card_layout = QGridLayout(card)
            card_layout.setContentsMargins(12, 10, 12, 10)
            heading = QLabel(f"{candidate.row_number}행 · {candidate.school_name}")
            heading.setObjectName("schoolCalendarNoteHeading")
            card_layout.addWidget(heading, 0, 0)
            note = QLabel(candidate.note)
            note.setObjectName("schoolCalendarSourceNote")
            note.setWordWrap(True)
            card_layout.addWidget(note, 0, 1, 1, 4)
            action = QComboBox()
            action.addItem("처리 방법 선택", None)
            action.addItem("반영 안 함", SchoolCalendarNoteAction.IGNORE.value)
            action.addItem(
                "업체 배송으로 반영 (X)",
                SchoolCalendarNoteAction.VENDOR_REQUIRED.value,
            )
            action.addItem(
                "학교 급식으로 반영 (O)",
                SchoolCalendarNoteAction.SCHOOL_PROVIDES.value,
            )
            card_layout.addWidget(action, 1, 0)
            fields = QFrame()
            fields.setObjectName("schoolCalendarNoteFields")
            fields_layout = QHBoxLayout(fields)
            fields_layout.setContentsMargins(9, 4, 9, 4)
            suggested = candidate.suggested_period
            default_start = source_row.ends_on if suggested is None else suggested.starts_on
            default_end = source_row.ends_on if suggested is None else suggested.ends_on
            starts_on = _date_edit(default_start)
            ends_on = _date_edit(default_end)
            grades = QLineEdit(", ".join(candidate.suggested_grades))
            grades.setPlaceholderText("전체 대상이면 비움 · 예: 6학년")
            fields_layout.addWidget(QLabel("기간"))
            fields_layout.addWidget(starts_on)
            fields_layout.addWidget(QLabel("~"))
            fields_layout.addWidget(ends_on)
            fields_layout.addWidget(QLabel("학년"))
            fields_layout.addWidget(grades, 1)
            card_layout.addWidget(fields, 1, 1, 1, 4)
            if suggested is None:
                hint = QLabel("비고에서 날짜를 찾지 못했습니다. 적용할 날짜를 직접 확인하세요.")
                hint.setObjectName("schoolCalendarNoteHint")
                card_layout.addWidget(hint, 2, 1, 1, 4)
            editor = _NoteEditors(candidate, action, starts_on, ends_on, grades, fields)
            self._note_editors[candidate.candidate_id] = editor
            action.currentIndexChanged.connect(
                lambda _index, current=editor: self._note_action_changed(current)
            )
            starts_on.dateChanged.connect(self._update_validation)
            ends_on.dateChanged.connect(self._update_validation)
            grades.textChanged.connect(self._update_validation)
            self._note_action_changed(editor)
            layout.addWidget(card)
        layout.addStretch(1)
        scroll.setWidget(body)
        return scroll

    def _note_action_changed(self, editor: _NoteEditors) -> None:
        raw_action = editor.action.currentData()
        action = None if raw_action is None else SchoolCalendarNoteAction(str(raw_action))
        active = action in {
            SchoolCalendarNoteAction.VENDOR_REQUIRED,
            SchoolCalendarNoteAction.SCHOOL_PROVIDES,
        }
        editor.fields.setEnabled(active)
        editor.grades.setEnabled(action is SchoolCalendarNoteAction.VENDOR_REQUIRED)
        self._update_validation()

    def _ignore_all_notes(self) -> None:
        for editor in self._note_editors.values():
            index = editor.action.findData(SchoolCalendarNoteAction.IGNORE.value)
            editor.action.setCurrentIndex(index)
        self._update_validation()

    def _school_match_text(self) -> str:
        imported = {normalize_school_name(row.school_name) for row in self._preview.rows}
        known = {normalize_school_name(name) for name in self._known_school_names}
        missing = len(known - imported)
        return (
            f"현재 명단 학교 {len(known)}곳 모두 포함"
            if not missing
            else f"현재 명단 중 이번 파일에 없는 학교 {missing}곳"
        )

    def _read_rows(self) -> tuple[SchoolVacationRow, ...]:
        rows = []
        for editor in self._row_editors:
            rows.append(
                SchoolVacationRow(
                    editor.source_row,
                    editor.school.text(),
                    _python_date(editor.starts_on),
                    SchoolLunchMark(str(editor.starts_mark.currentData())),
                    _python_date(editor.ends_on),
                    SchoolLunchMark(str(editor.ends_mark.currentData())),
                    editor.note,
                )
            )
        return tuple(rows)

    def _coverage(self) -> InclusiveDateRange:
        return InclusiveDateRange(
            _python_date(self._coverage_starts),
            _python_date(self._coverage_ends),
        )

    def _decisions(self) -> Mapping[UUID, SchoolCalendarNoteDecision]:
        decisions = {}
        for candidate_id, editor in self._note_editors.items():
            raw_action = editor.action.currentData()
            if raw_action is None:
                raise ValueError("모든 비고에서 처리 방법을 선택해주세요.")
            action = SchoolCalendarNoteAction(str(raw_action))
            if action is SchoolCalendarNoteAction.IGNORE:
                decision = SchoolCalendarNoteDecision(candidate_id, action)
            else:
                grades = ()
                if action is SchoolCalendarNoteAction.VENDOR_REQUIRED:
                    grades = tuple(
                        value
                        for value in re.split(r"[,/+·\s]+", editor.grades.text().strip())
                        if value
                    )
                decision = SchoolCalendarNoteDecision(
                    candidate_id,
                    action,
                    InclusiveDateRange(
                        _python_date(editor.starts_on),
                        _python_date(editor.ends_on),
                    ),
                    grades,
                )
            decisions[candidate_id] = decision
        return MappingProxyType(decisions)

    def _validated_result(
        self,
    ) -> tuple[SchoolCalendarImportPreview, Mapping[UUID, SchoolCalendarNoteDecision]]:
        preview = self._preview.with_review_edits(self._read_rows(), self._coverage())
        decisions = self._decisions()
        build_school_lunch_schedules(preview, decisions)
        return preview, decisions

    def _update_validation(self, *_args) -> None:
        if not hasattr(self, "_save"):
            return
        try:
            preview, decisions = self._validated_result()
        except (TypeError, ValueError) as error:
            self._save.setEnabled(False)
            self._summary.setText(str(error))
            self._summary.setProperty("outcome", "review")
            _repolish(self._summary)
            return
        self._save.setEnabled(True)
        applied = sum(
            decision.action is not SchoolCalendarNoteAction.IGNORE
            for decision in decisions.values()
        )
        ignored = len(decisions) - applied
        self._summary.setText(
            f"학교 {preview.school_count}곳 · 원본 {len(preview.rows)}행 · "
            f"비고 반영 {applied}건 / 반영 안 함 {ignored}건을 한 변경번호로 저장합니다."
        )
        self._summary.setProperty("outcome", "ready")
        _repolish(self._summary)

    def _accept_review(self) -> None:
        try:
            preview, decisions = self._validated_result()
        except (TypeError, ValueError) as error:
            self._error.setText(str(error))
            self._error.show()
            return
        self._error.hide()
        self._reviewed_preview = preview
        self._reviewed_decisions = decisions
        self.accept()

    def reviewed_preview(self) -> SchoolCalendarImportPreview:
        if self.result() != QDialog.DialogCode.Accepted or self._reviewed_preview is None:
            raise RuntimeError("확정된 학교 일정 검토 결과가 없습니다.")
        return self._reviewed_preview

    def decisions(self) -> Mapping[UUID, SchoolCalendarNoteDecision]:
        if self.result() != QDialog.DialogCode.Accepted or self._reviewed_decisions is None:
            raise RuntimeError("확정된 비고 검토 결정이 없습니다.")
        return self._reviewed_decisions


class SchoolCalendarImportStatus(StrEnum):
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class SchoolCalendarImportOutcome:
    status: SchoolCalendarImportStatus
    roster: RosterSnapshot | None = None
    calendar: SchoolLunchCalendar | None = None
    backup_result: AutomaticBackupResult | None = None

    @property
    def completed(self) -> bool:
        return self.status is SchoolCalendarImportStatus.COMPLETED


class SchoolCalendarImportController:
    """Commit one reviewed school workbook as one revision and one backup."""

    def __init__(
        self,
        session: DatabaseSession,
        *,
        actor_provider: Callable[[], RevisionActor] | None = None,
        import_dialog_type=SchoolCalendarImportDialog,
        review_dialog_type=SchoolCalendarReviewDialog,
        message_box=QMessageBox,
        today_provider: Callable[[], date] = date.today,
        recorded_at_provider: Callable[[], datetime] | None = None,
        revision_id_factory: Callable[[], UUID] = uuid4,
        batch_id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        if not isinstance(session, DatabaseSession):
            raise TypeError("session은 DatabaseSession이어야 합니다.")
        self._session = session
        self._actor_provider = actor_provider or current_windows_actor
        self._import_dialog_type = import_dialog_type
        self._review_dialog_type = review_dialog_type
        self._message_box = message_box
        self._today_provider = today_provider
        self._recorded_at_provider = recorded_at_provider or (lambda: datetime.now(UTC))
        self._revision_id_factory = revision_id_factory
        self._batch_id_factory = batch_id_factory

    def run(self, parent: QWidget | None = None) -> SchoolCalendarImportOutcome:
        roster = self._session.database.current_roster()
        if roster is None:
            self._message_box.critical(
                parent,
                "학교 일정을 가져올 수 없습니다",
                "현재 명단이 없습니다. Excel 명단 가져오기를 먼저 완료해주세요.",
            )
            return SchoolCalendarImportOutcome(SchoolCalendarImportStatus.FAILED)
        import_dialog = self._import_dialog_type(
            initial_directory=self._session.path.parent,
            calendar_year=self._today_provider().year,
            parent=parent,
        )
        if import_dialog.exec() != QDialog.DialogCode.Accepted:
            return SchoolCalendarImportOutcome(SchoolCalendarImportStatus.CANCELLED)
        preview = import_dialog.preview()
        review = self._review_dialog_type(
            preview,
            known_school_names=tuple(
                person.profile.school_name
                for person in roster.persons
                if person.profile.school_name is not None
            ),
            parent=parent,
        )
        if review.exec() != QDialog.DialogCode.Accepted:
            return SchoolCalendarImportOutcome(SchoolCalendarImportStatus.CANCELLED)
        latest = self._session.database.latest_revision()
        if latest is None:
            self._message_box.critical(
                parent,
                "학교 일정을 저장하지 못했습니다",
                "기준 변경 이력이 없습니다. 프로그램을 다시 열어주세요.",
            )
            return SchoolCalendarImportOutcome(SchoolCalendarImportStatus.FAILED)
        try:
            snapshot = self._session.database.snapshot(latest.metadata.revision_id)
            prepared = build_school_calendar_import_revision(
                snapshot,
                review.reviewed_preview(),
                decisions=review.decisions(),
                actor=self._actor_provider(),
                recorded_at=self._recorded_at_provider(),
                revision_id=self._revision_id_factory(),
                batch_id=self._batch_id_factory(),
            )
            backup_result = self._session.append_revision(prepared.record)
            updated_roster = self._session.database.current_roster()
        except Exception as error:
            self._message_box.critical(
                parent,
                "학교 일정을 저장하지 못했습니다",
                "운영 데이터에는 반영하지 않았습니다. 표와 비고 검토 내용을 확인한 뒤 다시 시도해주세요.\n\n"
                + str(error),
            )
            return SchoolCalendarImportOutcome(SchoolCalendarImportStatus.FAILED)
        if updated_roster is None:
            self._message_box.critical(
                parent,
                "저장한 학교 일정을 표시하지 못했습니다",
                "일정은 저장됐지만 현재 화면을 다시 읽지 못했습니다. 프로그램을 다시 열어주세요.",
            )
            return SchoolCalendarImportOutcome(SchoolCalendarImportStatus.FAILED)
        if backup_result.error is not None:
            self._message_box.warning(
                parent,
                "학교 일정은 저장됐지만 자동 백업에 실패했습니다",
                "학교별 일정과 변경 이력은 운영 데이터에 정상 저장되었습니다. "
                "백업 폴더 연결을 확인한 뒤 백업을 다시 실행해주세요.\n\n"
                + str(backup_result.error),
            )
        return SchoolCalendarImportOutcome(
            SchoolCalendarImportStatus.COMPLETED,
            updated_roster,
            prepared.calendar,
            backup_result,
        )


def _field_label(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("fieldLabel")
    return label


def _section_label(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("sectionLabel")
    return label


def _date_edit(value: date | None) -> QDateEdit:
    if value is None:
        raise ValueError("날짜 editor에는 날짜가 필요합니다.")
    editor = QDateEdit(QDate(value.year, value.month, value.day))
    editor.setCalendarPopup(True)
    editor.setDisplayFormat("yyyy.MM.dd")
    editor.setObjectName("schoolCalendarDate")
    return editor


def _python_date(editor: QDateEdit) -> date:
    value = editor.date()
    return date(value.year(), value.month(), value.day())


def _mark_combo(selected: SchoolLunchMark) -> QComboBox:
    combo = QComboBox()
    combo.setObjectName("schoolCalendarMark")
    combo.addItem("O · 학교 급식", SchoolLunchMark.SCHOOL_PROVIDES.value)
    combo.addItem("X · 업체 배송", SchoolLunchMark.VENDOR_REQUIRED.value)
    combo.setCurrentIndex(combo.findData(selected.value))
    return combo


def _repolish(widget: QWidget) -> None:
    widget.style().unpolish(widget)
    widget.style().polish(widget)
