"""Current-roster-date defaults and editable document period checks."""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import UTC, date, datetime
from uuid import UUID

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QDate
from PySide6.QtWidgets import QApplication

from src.desktop_v2 import (
    DocumentAction,
    DocumentPeriodDialog,
    DocumentPeriodSelection,
    DocumentSetupRequest,
    suggest_document_period,
)
from src.domain import Meal, RevisionActor, RevisionSource, RevisionSummary


ANCHOR_DAY = date(2026, 8, 17)
REVISION_ID = UUID("58000000-0000-0000-0000-000000000001")


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _summary() -> RevisionSummary:
    return RevisionSummary(
        sequence=4,
        revision_id=REVISION_ID,
        display_code="20260831-004",
        recorded_at=datetime(2026, 8, 31, 5, 0, tzinfo=UTC),
        effective_on=ANCHOR_DAY,
        actor=RevisionActor("tester", "S-1-5-21-test"),
        reason="배송 상태 수정",
        source=RevisionSource.UI,
    )


def _request(action: DocumentAction) -> DocumentSetupRequest:
    return DocumentSetupRequest(action, _summary(), ANCHOR_DAY, Meal.LUNCH)


@pytest.mark.parametrize(
    ("action", "expected"),
    (
        (
            DocumentAction.INVOICE,
            (date(2026, 8, 1), date(2026, 8, 31)),
        ),
        (
            DocumentAction.CONFIRMATION,
            (date(2026, 8, 1), date(2026, 8, 31)),
        ),
        (
            DocumentAction.DELIVERY_REPORT,
            (ANCHOR_DAY, ANCHOR_DAY),
        ),
        (
            DocumentAction.DELIVERY_SUBSTITUTE,
            (ANCHOR_DAY, ANCHOR_DAY),
        ),
    ),
)
def test_period_suggestion_uses_the_roster_screen_date(action, expected):
    assert suggest_document_period(action, ANCHOR_DAY) == expected


def test_month_suggestion_uses_real_leap_year_end():
    assert suggest_document_period(
        DocumentAction.INVOICE,
        date(2024, 2, 18),
    ) == (date(2024, 2, 1), date(2024, 2, 29))


def test_selection_contract_keeps_month_and_single_day_shapes():
    invoice = DocumentPeriodSelection(
        DocumentAction.INVOICE,
        _summary(),
        ANCHOR_DAY,
        Meal.LUNCH,
        date(2026, 8, 1),
        date(2026, 8, 31),
    )

    assert invoice.starts_on == date(2026, 8, 1)
    with pytest.raises(ValueError, match="한 달 전체"):
        replace(invoice, starts_on=date(2026, 8, 2))
    with pytest.raises(ValueError, match="하루"):
        DocumentPeriodSelection(
            DocumentAction.DELIVERY_SUBSTITUTE,
            _summary(),
            ANCHOR_DAY,
            Meal.LUNCH,
            date(2026, 8, 17),
            date(2026, 8, 18),
        )


@pytest.mark.parametrize(
    "action",
    (DocumentAction.INVOICE, DocumentAction.CONFIRMATION),
)
def test_monthly_dialog_opens_on_anchor_month_and_allows_another_month(
    app,
    action,
):
    dialog = DocumentPeriodDialog(_request(action))
    dialog.show()
    app.processEvents()

    initial = dialog.selected_period()
    assert (initial.starts_on, initial.ends_on) == (
        date(2026, 8, 1),
        date(2026, 8, 31),
    )
    assert "20260831-004" in dialog.findChild(
        type(dialog._summary),
        "documentPeriodAnchorDetail",
    ).text()

    dialog._month.setCurrentIndex(1)
    app.processEvents()

    changed = dialog.selected_period()
    assert (changed.starts_on, changed.ends_on) == (
        date(2026, 2, 1),
        date(2026, 2, 28),
    )
    assert "02월" in dialog._summary.text()
    dialog.close()


def test_report_dialog_starts_as_one_day_then_accepts_a_range(app):
    dialog = DocumentPeriodDialog(_request(DocumentAction.DELIVERY_REPORT))
    dialog.show()
    app.processEvents()

    assert dialog.selected_period().starts_on == ANCHOR_DAY
    assert dialog.selected_period().ends_on == ANCHOR_DAY

    dialog._starts_on.setDate(QDate(2026, 8, 20))
    dialog._ends_on.setDate(QDate(2026, 8, 19))
    app.processEvents()
    assert not dialog._save_button.isEnabled()
    assert "늦을 수 없습니다" in dialog._error.text()

    dialog._ends_on.setDate(QDate(2026, 8, 23))
    app.processEvents()
    assert dialog._save_button.isEnabled()
    assert dialog.selected_period().ends_on == date(2026, 8, 23)
    dialog.close()


def test_substitute_dialog_keeps_exactly_one_editable_day(app):
    dialog = DocumentPeriodDialog(
        _request(DocumentAction.DELIVERY_SUBSTITUTE)
    )

    assert dialog._day.date() == QDate(2026, 8, 17)
    assert dialog._starts_on is None
    assert dialog._ends_on is None

    dialog._day.setDate(QDate(2026, 8, 21))
    selected = dialog.selected_period()
    assert selected.starts_on == selected.ends_on == date(2026, 8, 21)
    dialog.close()
