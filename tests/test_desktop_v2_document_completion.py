"""Verified document completion and explicit file-opening behavior."""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import UUID

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from src.desktop_v2.document_hub import DocumentAction
from src.desktop_v2.document_completion import (
    DocumentCompletionDialog,
    GeneratedDocumentResult,
)
from src.desktop_v2.document_meal import DocumentMealSelection
from src.desktop_v2.document_output import (
    DocumentOutputMode,
    next_available_document_output,
)
from src.desktop_v2.document_period import DocumentPeriodSelection
from src.domain.roster import Meal
from src.domain.revisions import (
    RevisionActor,
    RevisionSource,
    RevisionSummary,
)


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _document() -> DocumentMealSelection:
    source = RevisionSummary(
        sequence=5,
        revision_id=UUID("5b000000-0000-0000-0000-000000000001"),
        display_code="20260831-005",
        recorded_at=datetime(2026, 8, 31, 6, 0, tzinfo=UTC),
        effective_on=date(2026, 8, 17),
        actor=RevisionActor("tester", "S-1-5-21-test"),
        reason="완료 화면 테스트",
        source=RevisionSource.UI,
    )
    period = DocumentPeriodSelection(
        DocumentAction.INVOICE,
        source,
        date(2026, 8, 17),
        Meal.LUNCH,
        date(2026, 8, 1),
        date(2026, 8, 31),
    )
    return DocumentMealSelection(period, tuple(Meal))


def _selection(tmp_path: Path):
    return next_available_document_output(
        _document(),
        tmp_path,
        DocumentOutputMode.SAVED_DEFAULT,
    )


def _generated_result(
    tmp_path: Path,
    content: bytes = b"synthetic xlsx bytes",
) -> GeneratedDocumentResult:
    output = _selection(tmp_path)
    output.output_path.write_bytes(content)
    return GeneratedDocumentResult(
        output,
        datetime(2026, 8, 31, 15, 30, tzinfo=UTC),
        hashlib.sha256(content).hexdigest(),
        len(content),
    )


def test_completion_shows_missing_lunch_school_dates_without_blocking(app, tmp_path):
    from dataclasses import replace
    from PySide6.QtWidgets import QPlainTextEdit
    result = replace(_generated_result(tmp_path), lunch_exclusions=("누락학교 · 1명 · 2026-08-21 (1식 제외)",))
    dialog = DocumentCompletionDialog(result)
    detail = dialog.findChild(QPlainTextEdit, "missingLunchScheduleDetails")
    assert detail.isReadOnly()
    assert "누락학교" in detail.toPlainText() and "2026-08-21" in detail.toPlainText()
    assert dialog.result.verify_file().is_file()
    dialog.close()


def test_result_requires_existing_nonempty_bytes_matching_size_and_hash(
    tmp_path: Path,
):
    output = _selection(tmp_path)
    with pytest.raises(ValueError, match="찾을 수 없습니다"):
        GeneratedDocumentResult(
            output,
            datetime(2026, 8, 31, 15, 30, tzinfo=UTC),
            hashlib.sha256(b"missing").hexdigest(),
            len(b"missing"),
        )

    content = b"published workbook"
    output.output_path.write_bytes(content)
    with pytest.raises(ValueError, match="크기가"):
        GeneratedDocumentResult(
            output,
            datetime(2026, 8, 31, 15, 30, tzinfo=UTC),
            hashlib.sha256(content).hexdigest(),
            len(content) + 1,
        )
    with pytest.raises(ValueError, match="내용이"):
        GeneratedDocumentResult(
            output,
            datetime(2026, 8, 31, 15, 30, tzinfo=UTC),
            hashlib.sha256(b"other workbook").hexdigest(),
            len(content),
        )

    verified = GeneratedDocumentResult(
        output,
        datetime(2026, 8, 31, 15, 30, tzinfo=UTC),
        hashlib.sha256(content).hexdigest(),
        len(content),
    )
    assert verified.verify_file() == output.output_path


def test_result_rejects_empty_file_and_naive_generation_time(tmp_path: Path):
    output = _selection(tmp_path)
    output.output_path.write_bytes(b"")
    with pytest.raises(ValueError, match="빈 문서"):
        GeneratedDocumentResult(
            output,
            datetime(2026, 8, 31, 15, 30, tzinfo=UTC),
            hashlib.sha256(b"").hexdigest(),
            0,
        )

    output.output_path.write_bytes(b"xlsx")
    with pytest.raises(ValueError, match="시간대"):
        GeneratedDocumentResult(
            output,
            datetime(2026, 8, 31, 15, 30),
            hashlib.sha256(b"xlsx").hexdigest(),
            4,
        )


def test_dialog_never_opens_file_or_folder_automatically(
    app: QApplication,
    tmp_path: Path,
):
    opened: list[Path] = []
    result = _generated_result(tmp_path)
    dialog = DocumentCompletionDialog(
        result,
        path_opener=lambda path: opened.append(path) or True,
    )
    dialog.show()
    app.processEvents()

    assert opened == []
    assert dialog._open_file_button.text() == "파일 열기"
    assert dialog._open_folder_button.text() == "폴더 열기"
    assert result.output.filename in dialog._result.output_path.name
    assert not dialog._status.isVisible()
    dialog.close()


def test_file_and_folder_open_only_after_their_buttons_are_clicked(
    app: QApplication,
    tmp_path: Path,
):
    opened: list[Path] = []
    result = _generated_result(tmp_path)
    dialog = DocumentCompletionDialog(
        result,
        path_opener=lambda path: opened.append(path) or True,
    )
    dialog.show()
    app.processEvents()

    dialog._open_file_button.click()
    assert opened == [result.output_path]
    assert "파일 열기를 요청" in dialog._status.text()
    assert dialog.isVisible()

    dialog._open_folder_button.click()
    assert opened == [result.output_path, result.output.directory]
    assert "폴더 열기를 요청" in dialog._status.text()
    assert dialog.isVisible()
    dialog.close()


def test_open_failure_is_inline_and_keeps_completion_dialog_open(
    app: QApplication,
    tmp_path: Path,
):
    result = _generated_result(tmp_path)
    dialog = DocumentCompletionDialog(result, path_opener=lambda _path: False)
    dialog.show()
    app.processEvents()

    dialog._open_file_button.click()

    assert "처리하지 못했습니다" in dialog._status.text()
    assert dialog._status.property("outcome") == "error"
    assert dialog._status.isVisible()
    assert dialog.isVisible()
    dialog.close()


def test_file_changed_after_completion_is_not_opened(
    app: QApplication,
    tmp_path: Path,
):
    opened: list[Path] = []
    result = _generated_result(tmp_path)
    dialog = DocumentCompletionDialog(
        result,
        path_opener=lambda path: opened.append(path) or True,
    )
    dialog.show()
    app.processEvents()
    result.output_path.write_bytes(b"changed after generation")

    dialog._open_file_button.click()

    assert opened == []
    assert "완료 기록과 다릅니다" in dialog._status.text()
    assert dialog.isVisible()
    dialog.close()
