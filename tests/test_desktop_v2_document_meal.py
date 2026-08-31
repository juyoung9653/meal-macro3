"""Safe defaults and constraints for document meal-lane selection."""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import UTC, date, datetime
from uuid import UUID

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QLabel

from src.desktop_v2 import (
    DocumentAction,
    DocumentMealDialog,
    DocumentMealSelection,
    DocumentPeriodSelection,
    suggest_document_meals,
)
from src.domain import Meal, RevisionActor, RevisionSource, RevisionSummary


ANCHOR_DAY = date(2026, 8, 17)


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _period(
    action: DocumentAction,
    *,
    anchor_meal: Meal = Meal.LUNCH,
) -> DocumentPeriodSelection:
    source = RevisionSummary(
        sequence=4,
        revision_id=UUID("59000000-0000-0000-0000-000000000001"),
        display_code="20260831-004",
        recorded_at=datetime(2026, 8, 31, 5, 0, tzinfo=UTC),
        effective_on=ANCHOR_DAY,
        actor=RevisionActor("tester", "S-1-5-21-test"),
        reason="배송 상태 수정",
        source=RevisionSource.UI,
    )
    if action in (DocumentAction.INVOICE, DocumentAction.CONFIRMATION):
        starts_on, ends_on = date(2026, 8, 1), date(2026, 8, 31)
    else:
        starts_on = ends_on = ANCHOR_DAY
    return DocumentPeriodSelection(
        action,
        source,
        ANCHOR_DAY,
        anchor_meal,
        starts_on,
        ends_on,
    )


@pytest.mark.parametrize(
    "action",
    (
        DocumentAction.INVOICE,
        DocumentAction.CONFIRMATION,
        DocumentAction.DELIVERY_REPORT,
    ),
)
def test_omission_sensitive_documents_default_to_all_meals(action):
    assert suggest_document_meals(action, Meal.LUNCH) == tuple(Meal)


@pytest.mark.parametrize("anchor_meal", tuple(Meal))
def test_substitute_defaults_to_the_current_roster_meal(anchor_meal):
    assert suggest_document_meals(
        DocumentAction.DELIVERY_SUBSTITUTE,
        anchor_meal,
    ) == (anchor_meal,)


def test_selection_requires_unique_meals_and_stores_enum_order():
    period = _period(DocumentAction.DELIVERY_REPORT)

    selection = DocumentMealSelection(
        period,
        (Meal.DINNER, Meal.BREAKFAST),
    )

    assert selection.meals == (Meal.BREAKFAST, Meal.DINNER)
    with pytest.raises(ValueError, match="하나 이상"):
        replace(selection, meals=())
    with pytest.raises(ValueError, match="중복"):
        replace(selection, meals=(Meal.LUNCH, Meal.LUNCH))


def test_substitute_selection_accepts_exactly_one_meal():
    period = _period(DocumentAction.DELIVERY_SUBSTITUTE)

    assert DocumentMealSelection(period, (Meal.LUNCH,)).meals == (Meal.LUNCH,)
    with pytest.raises(ValueError, match="하나만"):
        DocumentMealSelection(period, (Meal.BREAKFAST, Meal.LUNCH))


def test_multi_meal_dialog_defaults_all_and_blocks_an_empty_selection(app):
    dialog = DocumentMealDialog(_period(DocumentAction.INVOICE))
    dialog.show()
    app.processEvents()

    assert dialog.selected_meals() == tuple(Meal)
    assert dialog.selection().meals == tuple(Meal)
    assert dialog._summary.text() == "청구서 포함 식종: 조식 · 중식 · 석식 전체"

    dialog._buttons_by_meal[Meal.DINNER].setChecked(False)
    app.processEvents()
    assert dialog.selected_meals() == (Meal.BREAKFAST, Meal.LUNCH)

    dialog._clear_all()
    app.processEvents()
    assert dialog.selected_meals() == ()
    assert not dialog._save_button.isEnabled()
    assert "하나 이상" in dialog._error.text()

    dialog._select_all()
    app.processEvents()
    assert dialog.selected_meals() == tuple(Meal)
    assert dialog._save_button.isEnabled()
    dialog.close()


def test_substitute_dialog_is_exclusive_and_marks_current_screen_meal(app):
    dialog = DocumentMealDialog(
        _period(
            DocumentAction.DELIVERY_SUBSTITUTE,
            anchor_meal=Meal.DINNER,
        )
    )
    dialog.show()
    app.processEvents()

    assert dialog.selected_meals() == (Meal.DINNER,)
    assert dialog._summary.text() == "배송대타 포함 식종: 석식"
    badges = dialog.findChildren(QLabel, "documentMealAnchorBadge")
    assert len(badges) == 1
    assert badges[0].text() == "현재 화면 식종"
    assert badges[0].parent() is dialog._cards[Meal.DINNER]

    dialog._buttons_by_meal[Meal.BREAKFAST].click()
    app.processEvents()
    assert dialog.selected_meals() == (Meal.BREAKFAST,)
    assert dialog._save_button.isEnabled()
    dialog.close()
