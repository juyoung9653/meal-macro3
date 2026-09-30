"""Offscreen multi-meal delivery-status editor checks."""

import os
from datetime import date
from uuid import UUID

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QDialog

from src.desktop_v2 import MealStatusChangeDialog
from src.domain import (
    InclusiveDateRange,
    Meal,
    MealEnrollment,
    MealStatus,
    IntakeChannel,
    Person,
    PersonProfile,
    RosterSnapshot,
    resolve_status_history,
)


REVISION_ID = UUID("89000000-0000-0000-0000-000000000001")
PERSON_ID = UUID("89000000-0000-0000-0000-000000000002")
ANCHOR_DAY = date(2026, 9, 1)


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _roster() -> RosterSnapshot:
    return RosterSnapshot(
        REVISION_ID,
        persons=(Person(PERSON_ID, PersonProfile("홍길동")),),
        enrollments=(
            MealEnrollment(PERSON_ID, Meal.BREAKFAST),
            MealEnrollment(PERSON_ID, Meal.DINNER),
        ),
    )


def _histories():
    return {
        meal: resolve_status_history(PERSON_ID, meal, ())
        for meal in Meal
    }


def test_delivery_allows_unenrolled_meal_and_summary_marks_auto_add(
    app: QApplication,
):
    roster = _roster()
    dialog = MealStatusChangeDialog(
        roster.person(PERSON_ID),
        roster,
        _histories(),
        ANCHOR_DAY,
        Meal.LUNCH,
        MealStatus.DELIVERY,
    )

    assert dialog.selected_meals() == (Meal.LUNCH,)
    assert dialog.selected_status() is MealStatus.DELIVERY
    assert dialog.selected_period() == InclusiveDateRange(ANCHOR_DAY, None)
    assert "- → 식종 추가 · 배송" in dialog._summary_text.text()
    assert dialog._save_button.isEnabled()

    dialog._meal_checks[Meal.BREAKFAST].setChecked(True)
    app.processEvents()
    assert dialog.selected_meals() == (Meal.BREAKFAST, Meal.LUNCH)
    assert "조식" in dialog._summary_text.text()
    assert "중식" in dialog._summary_text.text()
    dialog.close()


def test_pause_blocks_unenrolled_meal_then_accepts_multiple_enrolled_meals(
    app: QApplication,
):
    roster = _roster()
    dialog = MealStatusChangeDialog(
        roster.person(PERSON_ID),
        roster,
        _histories(),
        ANCHOR_DAY,
        Meal.LUNCH,
        MealStatus.DELIVERY,
    )

    dialog._status_buttons[MealStatus.PAUSED].click()
    app.processEvents()
    assert not dialog._save_button.isEnabled()
    assert "이용 중인 식종이 아닙니다" in dialog._error.text()
    assert not dialog._continue.isEnabled()
    assert dialog.selected_period() == InclusiveDateRange(ANCHOR_DAY, ANCHOR_DAY)

    dialog._meal_checks[Meal.LUNCH].setChecked(False)
    dialog._meal_checks[Meal.BREAKFAST].setChecked(True)
    dialog._meal_checks[Meal.DINNER].setChecked(True)
    app.processEvents()

    assert dialog.selected_meals() == (Meal.BREAKFAST, Meal.DINNER)
    assert dialog._save_button.isEnabled()
    dialog._save_button.click()
    assert dialog.result() == QDialog.DialogCode.Accepted


def test_open_ended_stop_keeps_selected_meal_set(app: QApplication):
    roster = _roster()
    dialog = MealStatusChangeDialog(
        roster.person(PERSON_ID),
        roster,
        _histories(),
        ANCHOR_DAY,
        Meal.BREAKFAST,
        MealStatus.STOPPED,
    )
    dialog._meal_checks[Meal.DINNER].setChecked(True)
    app.processEvents()

    assert dialog.selected_meals() == (Meal.BREAKFAST, Meal.DINNER)
    assert dialog.selected_period() == InclusiveDateRange(ANCHOR_DAY, None)
    assert "미배송" in dialog._summary_text.text()
    dialog.close()


def test_intake_channel_is_optional_and_returns_selected_channel(app: QApplication):
    roster = _roster()
    dialog = MealStatusChangeDialog(
        roster.person(PERSON_ID),
        roster,
        _histories(),
        ANCHOR_DAY,
        Meal.BREAKFAST,
        MealStatus.DELIVERY,
    )
    try:
        assert dialog._intake_channel.currentText() == "미선택"
        assert dialog.selected_intake_channel() is None
        dialog._intake_channel.setCurrentText("기타")
        assert dialog.selected_intake_channel() is IntakeChannel.OTHER
    finally:
        dialog.close()
