"""Remembered default and one-time document output-folder checks."""

from __future__ import annotations

import os
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import UUID

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from src.desktop_v2 import (
    DocumentAction,
    DocumentMealSelection,
    DocumentOutputDialog,
    DocumentOutputMode,
    DocumentOutputSelection,
    DocumentPeriodSelection,
    next_available_document_output,
    suggest_document_filename,
)
from src.domain import Meal, RevisionActor, RevisionSource, RevisionSummary


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _document(
    action: DocumentAction = DocumentAction.INVOICE,
    meals: tuple[Meal, ...] = tuple(Meal),
    *,
    starts_on: date | None = None,
    ends_on: date | None = None,
) -> DocumentMealSelection:
    source = RevisionSummary(
        sequence=5,
        revision_id=UUID("5a000000-0000-0000-0000-000000000001"),
        display_code="20260831-005",
        recorded_at=datetime(2026, 8, 31, 6, 0, tzinfo=UTC),
        effective_on=date(2026, 8, 17),
        actor=RevisionActor("tester", "S-1-5-21-test"),
        reason="문서 폴더 테스트",
        source=RevisionSource.UI,
    )
    if starts_on is None or ends_on is None:
        if action in (DocumentAction.INVOICE, DocumentAction.CONFIRMATION):
            starts_on, ends_on = date(2026, 8, 1), date(2026, 8, 31)
        else:
            starts_on = ends_on = date(2026, 8, 17)
    period = DocumentPeriodSelection(
        action,
        source,
        date(2026, 8, 17),
        Meal.LUNCH,
        starts_on,
        ends_on,
    )
    return DocumentMealSelection(period, meals)


def test_output_selection_requires_an_existing_directory(tmp_path: Path):
    document = _document()
    filename = suggest_document_filename(document)
    selected = DocumentOutputSelection(
        document,
        tmp_path,
        DocumentOutputMode.SAVED_DEFAULT,
        filename,
        1,
    )

    assert selected.directory == tmp_path.resolve()
    assert not selected.remembers_directory
    with pytest.raises(ValueError, match="자동 파일명 규칙"):
        DocumentOutputSelection(
            document,
            tmp_path,
            DocumentOutputMode.SAVED_DEFAULT,
            "직접입력.xlsx",
            1,
        )
    with pytest.raises(ValueError, match="찾을 수 없습니다"):
        DocumentOutputSelection(
            _document(),
            tmp_path / "missing",
            DocumentOutputMode.ONE_TIME,
            filename,
            1,
        )


@pytest.mark.parametrize(
    ("document", "expected"),
    (
        (
            _document(DocumentAction.INVOICE),
            "2026-08_청구서_20260831-005.xlsx",
        ),
        (
            _document(DocumentAction.CONFIRMATION),
            "2026-08_확인서_20260831-005.xlsx",
        ),
        (
            _document(
                DocumentAction.DELIVERY_REPORT,
                (Meal.BREAKFAST, Meal.DINNER),
                starts_on=date(2026, 8, 17),
                ends_on=date(2026, 8, 23),
            ),
            "2026-08-17~2026-08-23_배송리포트_조식-석식_20260831-005.xlsx",
        ),
        (
            _document(
                DocumentAction.DELIVERY_SUBSTITUTE,
                (Meal.LUNCH,),
            ),
            "2026-08-17_배송대타_중식_20260831-005.xlsx",
        ),
    ),
)
def test_filename_uses_period_job_partial_meals_and_revision(document, expected):
    assert suggest_document_filename(document) == expected


def test_existing_names_get_a_new_reissue_number_without_overwrite(
    tmp_path: Path,
):
    document = _document()
    base = tmp_path / suggest_document_filename(document)
    second = tmp_path / suggest_document_filename(document, 2)
    base.write_text("first issue", encoding="utf-8")
    second.write_text("second issue", encoding="utf-8")

    selected = next_available_document_output(
        document,
        tmp_path,
        DocumentOutputMode.SAVED_DEFAULT,
    )

    assert selected.reissue_number == 3
    assert selected.filename.endswith("_재발행03.xlsx")
    assert selected.output_path == tmp_path / selected.filename
    assert not selected.output_path.exists()
    assert base.read_text(encoding="utf-8") == "first issue"
    assert second.read_text(encoding="utf-8") == "second issue"


def test_dialog_uses_the_remembered_folder_without_another_choice(
    app,
    tmp_path: Path,
):
    default = tmp_path / "기본 문서함"
    default.mkdir()
    dialog = DocumentOutputDialog(_document(), default)
    dialog.show()
    app.processEvents()

    selection = dialog.selection()
    assert selection.directory == default.resolve()
    assert selection.mode is DocumentOutputMode.SAVED_DEFAULT
    assert selection.filename == "2026-08_청구서_20260831-005.xlsx"
    assert selection.reissue_number == 1
    assert not selection.remembers_directory
    assert dialog._folder_stamp.text() == "기본"
    assert dialog._save_button.isEnabled()
    assert dialog._one_time_button.isVisible()
    dialog.close()


def test_one_time_folder_does_not_replace_the_remembered_default(
    app,
    tmp_path: Path,
):
    default = tmp_path / "기본"
    one_time = tmp_path / "이번만"
    default.mkdir()
    one_time.mkdir()

    dialog = DocumentOutputDialog(
        _document(),
        default,
        directory_picker=lambda _parent, title, start: (
            str(one_time)
            if title == "이번 문서만 저장할 폴더 선택"
            and start == str(default.resolve())
            else ""
        ),
    )
    dialog.show()
    dialog._one_time_button.click()
    app.processEvents()

    selection = dialog.selection()
    assert selection.directory == one_time.resolve()
    assert selection.mode is DocumentOutputMode.ONE_TIME
    assert not selection.remembers_directory
    assert dialog._folder_stamp.text() == "이번만"
    assert dialog._restore_default_button.isVisible()

    dialog._restore_default_button.click()
    assert dialog.selection().directory == default.resolve()
    assert dialog.selection().mode is DocumentOutputMode.SAVED_DEFAULT
    dialog.close()


def test_first_document_requires_and_remembers_a_new_default_folder(
    app,
    tmp_path: Path,
):
    chosen = tmp_path / "새 문서함"
    chosen.mkdir()
    dialog = DocumentOutputDialog(
        _document(),
        None,
        directory_picker=lambda _parent, _title, _start: str(chosen),
    )
    dialog.show()
    app.processEvents()

    assert not dialog._save_button.isEnabled()
    assert not dialog._one_time_button.isVisible()
    with pytest.raises(ValueError, match="먼저 선택"):
        dialog.selection()

    dialog._change_default_button.click()
    app.processEvents()
    selection = dialog.selection()
    assert selection.directory == chosen.resolve()
    assert selection.mode is DocumentOutputMode.NEW_DEFAULT
    assert selection.remembers_directory
    assert dialog._folder_stamp.text() == "새 기본"
    assert dialog._save_button.isEnabled()
    dialog.close()


def test_missing_remembered_folder_is_not_used_silently(app, tmp_path: Path):
    missing = tmp_path / "분리된 USB 폴더"
    dialog = DocumentOutputDialog(_document(), missing)
    dialog.show()
    app.processEvents()

    assert not dialog._save_button.isEnabled()
    assert "찾을 수 없습니다" in dialog._error.text()
    assert str(missing.resolve()) in dialog._folder_path.text()
    dialog.close()


def test_dialog_previews_reissue_name_when_base_file_exists(app, tmp_path: Path):
    document = _document()
    (tmp_path / suggest_document_filename(document)).write_text(
        "keep me",
        encoding="utf-8",
    )
    dialog = DocumentOutputDialog(document, tmp_path)
    dialog.show()
    app.processEvents()

    selection = dialog.selection()
    assert selection.reissue_number == 2
    assert dialog._filename.text() == selection.filename
    assert "재발행 02" in dialog._filename_status.text()
    assert dialog._filename_panel.property("reissue") is True
    dialog.close()
