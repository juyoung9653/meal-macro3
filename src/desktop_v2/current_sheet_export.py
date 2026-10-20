"""Publish an immutable snapshot of the currently visible roster sheet."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from time import monotonic

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from PySide6.QtCore import Qt, QThread, QTimer, Signal, Slot
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QLabel,
    QMessageBox,
    QProgressBar,
    QVBoxLayout,
)

from src.excel.publication import publish_workbook

from .startup_loading import run_progress_task, run_worker_with_delayed_progress, take_worker_result
from .theme import application_stylesheet


@dataclass(frozen=True, slots=True)
class CurrentSheetSnapshot:
    """UI-thread capture containing only strings, in their visual order."""

    sheet_name: str
    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]

    def __post_init__(self) -> None:
        if not isinstance(self.sheet_name, str) or not self.sheet_name.strip():
            raise ValueError("현재 시트 이름이 없습니다.")
        if not self.headers:
            raise ValueError("현재 시트에 표시된 칼럼이 없어 내보낼 수 없습니다.")
        if not all(isinstance(value, str) for value in self.headers):
            raise TypeError("시트 머리글은 문자열이어야 합니다.")
        if any(
            len(row) != len(self.headers)
            or not all(isinstance(value, str) for value in row)
            for row in self.rows
        ):
            raise ValueError("현재 시트 행과 칼럼 수가 맞지 않습니다.")


@dataclass(frozen=True, slots=True)
class TextWorkbookSnapshot:
    """Immutable text-only worksheets sharing one suggested output filename."""

    sheet_name: str
    sheets: tuple[CurrentSheetSnapshot, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.sheet_name, str) or not self.sheet_name.strip():
            raise ValueError("Excel 파일 이름이 없습니다.")
        if not self.sheets or not all(
            isinstance(sheet, CurrentSheetSnapshot) for sheet in self.sheets
        ):
            raise ValueError("Excel에 저장할 시트가 없습니다.")
        titles = tuple(_excel_sheet_name(sheet.sheet_name).casefold() for sheet in self.sheets)
        if len(set(titles)) != len(titles):
            raise ValueError("Excel 시트 이름이 중복됩니다.")


def build_current_sheet_workbook(
    snapshot: CurrentSheetSnapshot | TextWorkbookSnapshot,
    *,
    progress: Callable[[str, int, int], None] | None = None,
) -> Workbook:
    """Build captured worksheets while preserving every value as literal text."""

    progress = progress or (lambda _stage, _completed, _total: None)
    workbook = Workbook()
    first_worksheet = workbook.active
    if first_worksheet is None:  # pragma: no cover - openpyxl contract guard
        workbook.close()
        raise RuntimeError("Excel 시트를 만들지 못했습니다.")
    sheets = snapshot.sheets if isinstance(snapshot, TextWorkbookSnapshot) else (snapshot,)
    total = sum(len(sheet.rows) for sheet in sheets)
    completed = 0
    stage = (
        "기사·식종별 Excel 준비 중"
        if isinstance(snapshot, TextWorkbookSnapshot)
        else "현재 시트 Excel 준비 중"
    )
    header_fill = PatternFill("solid", fgColor="17313D")
    try:
        for sheet_index, sheet in enumerate(sheets):
            worksheet = (
                first_worksheet
                if sheet_index == 0
                else workbook.create_sheet()
            )
            worksheet.title = _excel_sheet_name(sheet.sheet_name)
            for column, value in enumerate(sheet.headers, 1):
                cell = worksheet.cell(1, column)
                _set_literal_text(cell, value)
                cell.font = Font(bold=True, color="FFFFFF")
                cell.fill = header_fill
                cell.alignment = Alignment(
                    horizontal="center", vertical="center", wrap_text=True
                )
            for row_number, values in enumerate(sheet.rows, 2):
                for column, value in enumerate(values, 1):
                    cell = worksheet.cell(row_number, column)
                    _set_literal_text(cell, value)
                    cell.alignment = Alignment(vertical="top", wrap_text=True)
                completed += 1
                progress(stage, completed, total)
            for column, header in enumerate(sheet.headers, 1):
                longest = max(
                    (
                        len(line)
                        for value in (
                            header,
                            *(row[column - 1] for row in sheet.rows),
                        )
                        for line in value.splitlines() or ("",)
                    ),
                    default=0,
                )
                worksheet.column_dimensions[get_column_letter(column)].width = min(
                    max(longest + 2, 10), 50
                )
            worksheet.freeze_panes = "A2"
            worksheet.auto_filter.ref = worksheet.dimensions
        return workbook
    except BaseException:
        workbook.close()
        raise


def _set_literal_text(cell, value: str) -> None:
    cell.value = value
    # openpyxl treats leading '=' as a formula unless the data type is forced.
    cell.data_type = "s"


def _excel_sheet_name(name: str) -> str:
    cleaned = "".join("_" if character in "[]:*?/\\" else character for character in name).strip("'")
    return (cleaned or "현재 시트")[:31]


class _CurrentSheetExportWorker(QThread):
    progress = Signal(str, int, int)

    def __init__(self, snapshot, password, target, builder, publisher):
        super().__init__()
        self.snapshot = snapshot
        self.password = password
        self.target = target
        self.builder = builder
        self.publisher = publisher
        self.result = None
        self.error: Exception | None = None

    def run(self) -> None:
        workbook = None
        try:
            workbook = self.builder(
                self.snapshot,
                progress=self.progress.emit,
            )
            self.progress.emit("Excel 암호화·파일 저장 중", 0, 0)
            self.result = self.publisher(workbook, self.target, password=self.password)
        except Exception as error:  # noqa: BLE001 - delivered to the UI thread
            self.error = error
        finally:
            if workbook is not None:
                workbook.close()


class CurrentSheetExportProgressDialog(QDialog):
    def __init__(self, worker: _CurrentSheetExportWorker, parent=None) -> None:
        super().__init__(parent)
        self._active = True
        self._started = monotonic()
        self.setWindowTitle("현재 시트 Excel 내보내기")
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, False)
        self.setStyleSheet(application_stylesheet())
        self.setMinimumWidth(460)
        layout = QVBoxLayout(self)
        self.stage = QLabel("내보내기 준비 중")
        self.stage.setWordWrap(True)
        self.bar = QProgressBar()
        self.bar.setRange(0, 0)
        self.elapsed = QLabel()
        layout.addWidget(self.stage)
        layout.addWidget(self.bar)
        layout.addWidget(self.elapsed)
        self.timer = QTimer(self)
        self.timer.setInterval(200)
        self.timer.timeout.connect(self._tick)
        self.timer.start()
        self._tick()
        worker.progress.connect(self.update_progress, Qt.ConnectionType.QueuedConnection)
        worker.finished.connect(self._finished, Qt.ConnectionType.QueuedConnection)

    @Slot(str, int, int)
    def update_progress(self, stage: str, completed: int, total: int) -> None:
        self.stage.setText(stage)
        self.bar.setRange(0, total)
        self.bar.setValue(completed)
        if total:
            self.bar.setFormat(f"{completed:,} / {total:,}명 (%p%)")

    def _tick(self) -> None:
        seconds = int(monotonic() - self._started)
        self.elapsed.setText(f"경과 시간 {seconds // 60}분 {seconds % 60}초")

    @Slot()
    def _finished(self) -> None:
        self._active = False
        self.timer.stop()
        self.accept()

    def reject(self) -> None:
        if not self._active:
            super().reject()

    def closeEvent(self, event) -> None:
        if self._active:
            event.ignore()
        else:
            super().closeEvent(event)


class CurrentSheetExportController:
    def __init__(
        self,
        *,
        file_dialog=QFileDialog,
        message_box=QMessageBox,
        workbook_builder=build_current_sheet_workbook,
        publisher=publish_workbook,
        progress_dialog_type=CurrentSheetExportProgressDialog,
    ) -> None:
        self._file_dialog = file_dialog
        self._messages = message_box
        self._builder = workbook_builder
        self._publisher = publisher
        self._progress_dialog_type = progress_dialog_type

    def run(
        self,
        snapshot: CurrentSheetSnapshot | TextWorkbookSnapshot,
        password: str,
        parent=None,
        *,
        directory: str | Path | None = None,
    ):
        try:
            multi_sheet = isinstance(snapshot, TextWorkbookSnapshot)
            export_title = (
                f"{snapshot.sheet_name} Excel 내보내기"
                if multi_sheet
                else "현재 시트 Excel 내보내기"
            )
            if not isinstance(password, str) or not password or not password.strip():
                raise ValueError("설정에서 생성 Excel 공통 비밀번호를 먼저 지정해주세요.")
            if password != password.strip():
                raise ValueError("Excel 출력 비밀번호 앞뒤에는 공백을 사용할 수 없습니다.")
            filename = Path(directory or Path.home()) / f"{_safe_filename(snapshot.sheet_name)}.xlsx"
            chosen, _ = self._file_dialog.getSaveFileName(
                parent,
                export_title,
                str(filename),
                "Excel 통합 문서 (*.xlsx)",
                options=QFileDialog.Option.DontConfirmOverwrite,
            )
            if not chosen:
                return None
            target = Path(chosen)
            if target.suffix.lower() != ".xlsx":
                target = target.with_suffix(".xlsx")
            exists = run_progress_task(
                target.exists, title="Excel 저장 위치를 확인하는 중입니다",
                message="저장 폴더와 같은 이름의 파일을 확인하는 중입니다.", parent=parent,
            )
            if exists and self._messages.question(
                parent,
                "파일 덮어쓰기",
                f"{target.name} 파일을 덮어쓸까요?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            ) != QMessageBox.StandardButton.Yes:
                return None
            worker = _CurrentSheetExportWorker(
                snapshot, password, target, self._builder, self._publisher
            )
            progress = self._progress_dialog_type(worker, parent)
            if multi_sheet:
                progress.setWindowTitle(export_title)
            run_worker_with_delayed_progress(worker, progress)
            result = take_worker_result(worker)
        except Exception as error:  # noqa: BLE001 - output failure is reported at the UI boundary
            failure_title = (
                "기사·식종별 Excel을 내보내지 못했습니다"
                if isinstance(snapshot, TextWorkbookSnapshot)
                else "현재 시트를 내보내지 못했습니다"
            )
            self._messages.critical(parent, failure_title, str(error))
            return None
        self._messages.information(parent, "Excel 내보내기 완료", f"저장했습니다: {result.output_path}")
        return result


def _safe_filename(name: str) -> str:
    cleaned = "".join("_" if character in '<>:"/\\|?*' else character for character in name)
    cleaned = cleaned.strip().rstrip(".")
    return cleaned or "현재 시트"
