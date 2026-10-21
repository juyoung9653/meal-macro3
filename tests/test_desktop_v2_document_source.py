"""Offscreen behavior checks for the document revision picker."""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import UTC, date, datetime
from uuid import UUID

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from src.desktop_v2.document_source import DocumentSourceDialog
from src.domain.revisions import (
    RevisionActor,
    RevisionSource,
    RevisionSummary,
)


FIRST_ID = UUID("56000000-0000-0000-0000-000000000001")
LATEST_ID = UUID("56000000-0000-0000-0000-000000000002")


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _summary(
    sequence: int,
    revision_id: UUID,
    display_code: str,
    reason: str,
) -> RevisionSummary:
    return RevisionSummary(
        sequence=sequence,
        revision_id=revision_id,
        display_code=display_code,
        recorded_at=datetime(2026, 8, 26, sequence, 0, tzinfo=UTC),
        effective_on=date(2026, 8, 17 + sequence),
        actor=RevisionActor("tester", "S-1-5-21-test"),
        reason=reason,
        source=RevisionSource.UI,
    )


def _summaries() -> tuple[RevisionSummary, ...]:
    first = _summary(1, FIRST_ID, "20260826-001", "첫 명단 저장")
    latest = _summary(2, LATEST_ID, "20260826-002", "배송 상태 수정")
    return first, latest


def test_latest_revision_is_default_and_history_starts_hidden(app):
    first, latest = _summaries()
    dialog = DocumentSourceDialog((latest, first))
    dialog.show()
    app.processEvents()

    assert dialog.selected_revision_id == LATEST_ID
    assert dialog.using_latest
    assert dialog._source_code.text() == "20260826-110000"
    assert "2026.08.26 11:00" not in dialog._source_detail.text()
    assert dialog._history_button.text() == "이전 변경번호 선택"
    assert dialog._history_button.isVisible()
    assert not dialog._archive_frame.isVisible()
    assert "20260826-110000" in dialog._archive_list.item(0).text()
    assert "2026.08.26 11:00" not in dialog._archive_list.item(0).text()
    assert dialog._archive_list.count() == 2
    assert dialog._archive_list.currentRow() == 0
    assert dialog.selected_revision_id == LATEST_ID
    dialog.close()


def test_previous_revision_requires_explicit_row_selection_and_can_return_latest(app):
    dialog = DocumentSourceDialog(_summaries())
    dialog.show()
    dialog._history_button.click()
    app.processEvents()
    assert dialog._archive_frame.isVisible()
    assert dialog._history_button.text() == "변경번호 목록 닫기"
    dialog._archive_list.setCurrentRow(1)
    app.processEvents()

    assert dialog.selected_revision_id == FIRST_ID
    assert not dialog.using_latest
    assert dialog._source_stamp.text() == "이전 변경번호"
    assert "이후에 저장된 수정" in dialog._selection_note.text()
    assert dialog._latest_button.isVisible()

    dialog._latest_button.click()
    app.processEvents()
    assert dialog.selected_revision_id == LATEST_ID
    assert dialog.using_latest
    assert dialog._archive_list.currentRow() == 0
    dialog.close()


def test_single_revision_shows_latest_in_list(app):
    latest = _summaries()[1]
    dialog = DocumentSourceDialog((latest,))

    assert not dialog._history_button.isVisible()
    assert not dialog._archive_frame.isVisible()
    assert dialog._archive_list.count() == 1
    assert dialog._archive_list.currentRow() == 0
    assert dialog.selected_revision_id == LATEST_ID
    dialog.close()


def test_same_second_revisions_remain_individually_selectable(app):
    first, latest = _summaries()
    first = replace(first, recorded_at=latest.recorded_at)
    dialog = DocumentSourceDialog((first, latest))
    assert dialog.selected_revision_id == LATEST_ID
    code = dialog._source_code.text()
    dialog._history_button.click()
    dialog._archive_list.setCurrentRow(1)
    assert dialog._source_code.text() == code
    assert dialog.selected_revision_id == FIRST_ID
    assert dialog.selected_summary.reason == first.reason
    dialog.close()


def test_intentional_picker_open_shows_archive_and_keeps_previous_choice(app):
    first, latest = _summaries()
    dialog = DocumentSourceDialog((latest, first))
    dialog.set_selected_summary(first)
    dialog.show_archive()
    dialog.show()
    app.processEvents()

    assert dialog._archive_frame.isVisible()
    assert dialog._archive_list.currentRow() == 1
    assert dialog.selected_summary == first
    dialog.reject()
