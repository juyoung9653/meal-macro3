"""Choose and publish the current database's roster and history workbook."""
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from time import monotonic

from PySide6.QtCore import QDate, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtWidgets import (
    QCheckBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QVBoxLayout,
)

from src.domain.export_security import require_export_password_setting
from src.excel.publication import publish_workbook
from src.services.database_export import build_database_workbook

from .ledger_reader import load_revision_chain
from .theme import application_stylesheet, register_application_fonts


@dataclass(frozen=True)
class DatabaseExportSelection:
    day: date
    sections: tuple[str, ...]


class DatabaseExportDialog(QDialog):
    def __init__(self, day, parent=None):
        super().__init__(parent)
        register_application_fonts()
        self.setStyleSheet(application_stylesheet())
        self.setWindowTitle("현재 DB Excel 내보내기")
        self.resize(560, 360)
        layout = QVBoxLayout(self)
        intro = QLabel("현재 DB의 전체 명단과 기록을 내보냅니다. 화면 검색·열 숨김과 관계없이 모든 항목을 포함합니다.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        form = QFormLayout()
        self.day = QDateEdit(QDate(day.year, day.month, day.day))
        self.day.setCalendarPopup(True)
        self.day.setDisplayFormat("yyyy-MM-dd")
        form.addRow("명단 조회 기준일", self.day)
        layout.addLayout(form)
        self.sections = {}
        for key, label in (
            ("roster", "전체 명단 (모든 칼럼·사용자 항목 포함)"),
            ("complaints", "민원 접수 기록"),
            ("history", "정보·식종 변경 이력"),
        ):
            checkbox = QCheckBox(label)
            checkbox.setChecked(True)
            self.sections[key] = checkbox
            layout.addWidget(checkbox)
        note = QLabel(
            "한 명이라도 가입 식종별 배송담당이 다르면 명단 전체를 식종별 행으로 나눕니다. "
            "모두 같으면 한 사람 한 행에 식종 3개 열을 사용합니다. "
            "이력은 기록 전체를 별도 시트로 내보내며, 설정된 Excel 공통 비밀번호를 적용합니다."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setText("파일 저장")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("취소")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        for checkbox in self.sections.values():
            checkbox.toggled.connect(self._refresh)
        layout.addWidget(self.buttons)

    def _refresh(self):
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setEnabled(
            any(box.isChecked() for box in self.sections.values())
        )

    def selection(self):
        day = self.day.date()
        sections = tuple(key for key, box in self.sections.items() if box.isChecked())
        if not sections:
            raise ValueError("내보낼 항목을 하나 이상 선택해주세요.")
        return DatabaseExportSelection(date(day.year(), day.month(), day.day()), sections)


class _ExportWorker(QThread):
    progress = Signal(str, int, int)

    def __init__(self, session, selection, target, builder, publisher):
        super().__init__()
        self.session, self.selection, self.target = session, selection, target
        self.builder, self.publisher = builder, publisher
        self.result = None
        self.error = None

    def run(self):
        try:
            self.progress.emit("현재 DB 읽는 중", 0, 0)
            database = self.session.database
            roster = database.current_roster()
            if roster is None:
                raise ValueError("현재 명단이 없습니다.")
            snapshot = database.snapshot(roster.revision_id)
            password = require_export_password_setting(snapshot).password
            self.progress.emit("변경 이력 읽는 중", 0, 0)
            revisions = load_revision_chain(database, roster.revision_id)
            self.progress.emit("Excel 시트 준비 중", 0, 0)
            workbook = self.builder(
                roster, snapshot, revisions, day=self.selection.day,
                sections=self.selection.sections, progress=self.progress.emit,
            )
            try:
                self.progress.emit("Excel 암호화·파일 저장 중", 0, 0)
                self.result = self.publisher(workbook, self.target, password=password)
            finally:
                workbook.close()
        except Exception as error:  # noqa: BLE001 - delivered to the UI thread
            self.error = error


class DatabaseExportProgressDialog(QDialog):
    """Keep the event loop alive and the database session open until work finishes."""

    def __init__(self, worker, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Excel 내보내기 진행 중")
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, False)
        self.setStyleSheet(application_stylesheet())
        self.setMinimumWidth(460)
        self._active = True
        self._started = monotonic()
        layout = QVBoxLayout(self)
        self.stage = QLabel("내보내기 준비 중")
        self.stage.setWordWrap(True)
        self.bar = QProgressBar()
        self.bar.setRange(0, 0)
        self.elapsed = QLabel()
        for widget in (self.stage, self.bar, self.elapsed):
            layout.addWidget(widget)
        self.timer = QTimer(self)
        self.timer.setInterval(200)
        self.timer.timeout.connect(self._tick)
        self.timer.start()
        self._tick()
        worker.progress.connect(self.update_progress, Qt.ConnectionType.QueuedConnection)
        worker.finished.connect(self._finished, Qt.ConnectionType.QueuedConnection)

    @Slot(str, int, int)
    def update_progress(self, stage, completed, total):
        self.stage.setText(stage)
        self.bar.setRange(0, total)
        self.bar.setValue(completed)
        if total:
            self.bar.setFormat(f"{completed:,} / {total:,}명 (%p%)")

    def _tick(self):
        seconds = int(monotonic() - self._started)
        self.elapsed.setText(f"경과 시간 {seconds // 60}분 {seconds % 60}초 · 작업이 끝나면 결과를 알려드립니다.")

    @Slot()
    def _finished(self):
        self._active = False
        self.timer.stop()
        self.accept()

    def reject(self):
        if not self._active:
            super().reject()

    def closeEvent(self, event):
        if self._active:
            event.ignore()
        else:
            super().closeEvent(event)


class DatabaseExportController:
    def __init__(
        self, session, *, dialog_type=DatabaseExportDialog,
        file_dialog=QFileDialog, message_box=QMessageBox,
        workbook_builder=build_database_workbook, publisher=publish_workbook,
    ):
        self._session = session
        self._dialog_type = dialog_type
        self._file_dialog = file_dialog
        self._messages = message_box
        self._builder = workbook_builder
        self._publisher = publisher

    def run(self, day, parent=None, *, directory=None):
        dialog = self._dialog_type(day, parent)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        try:
            selection = dialog.selection()
            filename = Path(directory or Path.home()) / f"급식_DB_{selection.day:%Y%m%d}.xlsx"
            chosen, _ = self._file_dialog.getSaveFileName(
                parent, "DB Excel 내보내기", str(filename), "Excel 통합 문서 (*.xlsx)",
                options=QFileDialog.Option.DontConfirmOverwrite,
            )
            if not chosen:
                return None
            target = Path(chosen)
            if target.suffix.lower() != ".xlsx":
                target = target.with_suffix(".xlsx")
            if target.exists() and self._messages.question(
                parent, "파일 덮어쓰기", f"{target.name} 파일을 덮어쓸까요?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            ) != QMessageBox.StandardButton.Yes:
                return None
            worker = _ExportWorker(self._session, selection, target, self._builder, self._publisher)
            progress = DatabaseExportProgressDialog(worker, parent)
            QTimer.singleShot(0, worker.start)
            progress.exec()
            worker.wait()
            if worker.error is not None:
                raise worker.error
            result = worker.result
        except Exception as error:  # noqa: BLE001 - report output failures at the UI boundary
            self._messages.critical(parent, "Excel을 내보내지 못했습니다", str(error))
            return None
        self._messages.information(parent, "Excel 내보내기 완료", f"저장했습니다: {result.output_path}")
        return result
