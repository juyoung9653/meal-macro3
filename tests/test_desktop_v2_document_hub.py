"""Offscreen checks for the single document-output entry point."""

from __future__ import annotations

import os
from datetime import UTC, date, datetime
from uuid import UUID

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QDialog

from src.desktop_v2.document_hub import (
    DocumentAction,
    DocumentHubDialog,
    DocumentSetupRequest,
)
from src.domain.roster import Meal
from src.domain.revisions import (
    RevisionActor,
    RevisionSource,
    RevisionSummary,
)


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_hub_groups_exact_four_document_jobs_without_default_choice(app):
    dialog = DocumentHubDialog()
    dialog.show()
    app.processEvents()

    assert [action.label for action in dialog._cards] == [
        "일일식수",
        "배송목록",
        "청구서",
        "확인서",
    ]
    assert [card.accessibleName() for card in dialog._cards.values()] == [
        "1 일일식수", "2 배송목록", "3 청구서", "4 확인서",
    ]
    assert dialog._cards[DocumentAction.INVOICE].property("category") == "settlement"
    assert (
        dialog._cards[DocumentAction.DELIVERY_REPORT].property("category")
        == "route"
    )
    with pytest.raises(RuntimeError, match="아직 선택"):
        _ = dialog.selected_action
    dialog.close()


@pytest.mark.parametrize("action", tuple(DocumentAction))
def test_one_card_click_accepts_the_exact_document_job(app, action):
    dialog = DocumentHubDialog()

    dialog._cards[action].click()
    app.processEvents()

    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.selected_action is action


def test_card_copy_explains_the_output_in_plain_language(app):
    assert "한 달 식수" in DocumentAction.INVOICE.description
    assert "선택한 기간" in DocumentAction.CONFIRMATION.description
    assert DocumentAction.DELIVERY_REPORT.description == "가구수 및 도시락 갯수를 날짜별 엑셀로 정리합니다"
    assert DocumentAction.DELIVERY_SUBSTITUTE.description == "기사별 배송 리스트를 만듭니다"


def test_setup_request_keeps_the_exact_action_and_sealed_source(app):
    summary = RevisionSummary(
        sequence=3,
        revision_id=UUID("57000000-0000-0000-0000-000000000003"),
        display_code="20260831-003",
        recorded_at=datetime(2026, 8, 31, 4, 0, tzinfo=UTC),
        effective_on=date(2026, 8, 31),
        actor=RevisionActor("tester", "S-1-5-21-test"),
        reason="휴무 일정 저장",
        source=RevisionSource.UI,
    )

    request = DocumentSetupRequest(
        DocumentAction.INVOICE,
        summary,
        date(2026, 8, 17),
        Meal.LUNCH,
    )

    assert request.action is DocumentAction.INVOICE
    assert request.source.display_code == "20260831-003"
    assert request.anchor_day == date(2026, 8, 17)
    assert request.anchor_meal is Meal.LUNCH
    with pytest.raises(TypeError, match="DocumentAction"):
        DocumentSetupRequest(
            "invoice",
            summary,
            date(2026, 8, 17),
            Meal.LUNCH,
        )
