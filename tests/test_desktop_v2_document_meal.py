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

from src.desktop_v2.document_hub import DocumentAction
from src.desktop_v2.document_meal import (
    DocumentMealDialog,
    DocumentMealSelection,
    suggest_document_meals,
)
from src.desktop_v2.document_period import DocumentPeriodSelection
from src.domain.roster import Meal
from src.domain.revisions import (
    RevisionActor,
    RevisionSource,
    RevisionSummary,
)


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
    ),
)
def test_omission_sensitive_documents_default_to_all_meals(action):
    assert suggest_document_meals(action, Meal.LUNCH) == tuple(Meal)


@pytest.mark.parametrize("anchor_meal", tuple(Meal))
def test_substitute_default_uses_selected_date(anchor_meal):
    assert suggest_document_meals(
        DocumentAction.DELIVERY_SUBSTITUTE,
        anchor_meal,
        ANCHOR_DAY,
    ) == (Meal.DINNER,)


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


def test_substitute_selection_accepts_breakfast_pairs():
    period = _period(DocumentAction.DELIVERY_SUBSTITUTE)

    assert DocumentMealSelection(period, (Meal.LUNCH,)).meals == (Meal.LUNCH,)
    assert DocumentMealSelection(period, (Meal.BREAKFAST, Meal.LUNCH)).meals == (Meal.LUNCH, Meal.BREAKFAST)
    with pytest.raises(ValueError, match="조식을 함께"):
        DocumentMealSelection(period, (Meal.DINNER, Meal.LUNCH))


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


@pytest.mark.parametrize("profile_name", (None, "급식", "행복도시락"))
def test_substitute_dialog_is_exclusive_without_current_screen_meal(app, profile_name):
    from src.domain.document_templates import DocumentTemplateProfile
    profile = DocumentTemplateProfile(profile_name) if profile_name else None
    happy = profile is DocumentTemplateProfile.HAPPY_LUNCHBOX
    dialog = DocumentMealDialog(
        _period(
            DocumentAction.DELIVERY_SUBSTITUTE,
            anchor_meal=Meal.DINNER,
        )
    )
    dialog.show()
    app.processEvents()

    assert dialog.selected_meals() == (Meal.DINNER,)
    dialog.set_substitute_profile(profile)
    assert dialog._include_breakfast.isVisible() == happy
    assert dialog.selected_meals() == ((Meal.DINNER, Meal.BREAKFAST) if happy else (Meal.DINNER,))
    assert dialog._save_button.isEnabled()
    assert not dialog.findChildren(QLabel, "documentMealAnchorBadge")
    assert all("화면 식종" not in label.text() for label in dialog.findChildren(QLabel))

    dialog._buttons_by_meal[Meal.BREAKFAST].click()
    app.processEvents()
    assert dialog.selected_meals() == ((Meal.BREAKFAST, Meal.DINNER) if happy else (Meal.BREAKFAST,))
    dialog._buttons_by_meal[Meal.LUNCH].click()
    assert dialog.selected_meals() == ((Meal.LUNCH, Meal.BREAKFAST) if happy else (Meal.LUNCH,))
    if not happy:
        dialog._include_breakfast.setChecked(True)
        assert dialog.selection().meals == (Meal.LUNCH,)
    dialog._include_breakfast.setChecked(False)
    assert dialog.selected_meals() == (Meal.LUNCH,)
    assert dialog._save_button.isEnabled()
    dialog.close()


def test_report_dialog_selects_forms_and_blocks_all_unchecked(app):
    dialog = DocumentMealDialog(_period(DocumentAction.DELIVERY_REPORT))
    dialog.set_available_delivery_forms((None, "도시락", "식재료 박스"))
    dialog.show()
    app.processEvents()

    assert dialog.selection().delivery_forms == (None, "도시락", "식재료 박스")
    assert all(card.height() >= 80 for card in dialog._cards.values())
    assert all(button.height() >= 18 for button in dialog._buttons_by_form.values())
    dialog._buttons_by_form["식재료 박스"].setChecked(False)
    app.processEvents()
    assert dialog.selection().delivery_forms == (None, "도시락")
    assert "도시락" in dialog._summary.text()

    for button in dialog._buttons_by_form.values():
        button.setChecked(False)
    app.processEvents()
    assert not dialog._save_button.isEnabled()
    assert "배송 형태를 하나 이상" in dialog._error.text()
    with pytest.raises(ValueError, match="배송 형태를 하나 이상"):
        dialog.selection()
    dialog.close()


def test_report_form_selection_is_legacy_all_without_configured_choices():
    selection = DocumentMealSelection(
        _period(DocumentAction.DELIVERY_REPORT), (Meal.DINNER,),
    )
    assert selection.delivery_forms is None
    with pytest.raises(ValueError, match="일일식수 문서에서만"):
        DocumentMealSelection(
            _period(DocumentAction.INVOICE), (Meal.DINNER,),
            delivery_forms=(None,),
        )


def test_report_choices_persist_in_database_and_cancel_keeps_previous(app, tmp_path):
    from src.desktop_v2.document_meal import load_daily_report_choices, save_daily_report_choices
    from src.storage.database_session import DatabaseSession
    from tests.test_delivery_routing import initialize
    from tests.test_sequence_workflow import roster_fixture
    path = tmp_path / "choices.sqlite3"
    def open_dialog(session, forms=(None, "도시락", "식재료 박스")):
        dialog = DocumentMealDialog(_period(DocumentAction.DELIVERY_REPORT))
        dialog.set_available_delivery_forms(forms)
        dialog.set_report_choices(load_daily_report_choices(session))
        return dialog

    with DatabaseSession.create(path, "password") as session:
        initialize(session, roster_fixture())
        original = session.database.current_roster().revision_id
        first = open_dialog(session)
        first._clear_all()
        first._buttons_by_meal[Meal.DINNER].setChecked(True)
        first._buttons_by_form[None].setChecked(False)
        first._buttons_by_form["식재료 박스"].setChecked(False)
        first._accept_if_valid()
        roster, backup = save_daily_report_choices(session, first.selected_report_choices())
        assert roster.revision_id != original
        current = roster.revision_id
        assert save_daily_report_choices(session, first.selected_report_choices())[1] is None
        assert session.database.current_roster().revision_id == current

    # A new session restores choices entirely from the DB, with no PC settings.
    with DatabaseSession.open(path, "password") as session:
        reopened = open_dialog(session)
        assert reopened.selected_meals() == (Meal.DINNER,)
        assert reopened.selected_delivery_forms() == ("도시락",)
        reopened._select_all()
        reopened._buttons_by_form[None].setChecked(True)
        reopened.reject()
        assert load_daily_report_choices(session).meals == (Meal.DINNER,)
        historical = open_dialog(session, (None, "도시락", "새 형태"))
        assert historical.selected_meals() == (Meal.DINNER,)
        assert historical.selected_delivery_forms() == ("도시락", "새 형태")
        save_daily_report_choices(session, historical.selected_report_choices())
        historical.close()
        restored = open_dialog(session, (None, "도시락", "식재료 박스", "새 형태"))
        assert restored.selected_delivery_forms() == ("도시락", "새 형태")
        restored.reject()
        unavailable = open_dialog(session, (None,))
        assert unavailable.selected_delivery_forms() == ()
        assert not unavailable._save_button.isEnabled()
        unavailable.reject()
    with DatabaseSession.create(tmp_path / "other.sqlite3", "password") as other:
        initialize(other, roster_fixture())
        dialog = open_dialog(other)
        assert dialog.selected_meals() == (Meal.DINNER,)
        assert dialog.selected_delivery_forms() == (None, "도시락", "식재료 박스")
        dialog.close()


@pytest.mark.parametrize("profile_name", (None, "급식", "행복도시락"))
def test_report_setup_reads_profile_from_current_database_snapshot(profile_name):
    from types import SimpleNamespace
    from src.domain.document_templates import (
        DOCUMENT_TEMPLATE_SETTING_KIND, DOCUMENT_TEMPLATE_SETTING_ID,
        DocumentTemplateProfile, DocumentTemplateSetting, document_template_setting_state,
    )
    from src.domain.revisions import EntityRef, HistoricalSnapshot
    from src.desktop_v2.document_meal import load_daily_report_setup
    from tests.test_sqlite_delivery_report_service import _snapshot

    snapshot, _ = _snapshot()
    if profile_name is not None:
        setting = DocumentTemplateSetting(1, DocumentTemplateProfile(profile_name), snapshot.revision_id)
        snapshot = HistoricalSnapshot(snapshot.revision_id, {
            **snapshot.entities,
            EntityRef(DOCUMENT_TEMPLATE_SETTING_KIND, DOCUMENT_TEMPLATE_SETTING_ID):
                document_template_setting_state(setting),
        })
    requested = []
    def read_snapshot(revision_id):
        requested.append(revision_id)
        return snapshot
    session = SimpleNamespace(database=SimpleNamespace(
        current_roster=lambda: SimpleNamespace(revision_id=snapshot.revision_id),
        snapshot=read_snapshot,
    ))
    choices, profile = load_daily_report_setup(session)
    assert requested == [snapshot.revision_id]
    assert profile == (DocumentTemplateProfile(profile_name) if profile_name else None)
    assert choices.excluded_forms == ()
