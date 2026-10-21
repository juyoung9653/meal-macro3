"""Offscreen multi-meal delivery-status editor checks."""

import os
from dataclasses import replace
from datetime import date, timedelta
from uuid import UUID

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QDate, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QLabel

from src.desktop_v2.meal_status_edit import MealStatusChangeDialog
from src.domain.roster import (
    InclusiveDateRange,
    HouseholdDeliverySetting,
    Meal,
    MealEnrollment,
    Person,
    PersonProfile,
    RosterSnapshot,
)
from src.domain.status import (
    MealStatus,
    resolve_status_history,
)
from src.domain.intake import IntakeChannel


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
    assert dialog.selected_period() == InclusiveDateRange(date(2026, 9, 4), None)
    assert "중지" in dialog._summary_text.text()
    dialog.close()


def test_selecting_stop_after_pause_defaults_to_no_end_and_keeps_entered_date(
    app: QApplication,
):
    roster = _roster()
    dialog = MealStatusChangeDialog(
        roster.person(PERSON_ID),
        roster,
        _histories(),
        ANCHOR_DAY,
        Meal.BREAKFAST,
        MealStatus.PAUSED,
    )
    entered_end = ANCHOR_DAY + timedelta(days=10)
    dialog._ends_on.setDate(
        QDate(entered_end.year, entered_end.month, entered_end.day)
    )

    dialog._status_buttons[MealStatus.STOPPED].click()
    app.processEvents()

    assert dialog.selected_status() is MealStatus.STOPPED
    assert dialog.selected_period() == InclusiveDateRange(date(2026, 9, 4), None)
    assert dialog._ends_on.date() == QDate(
        entered_end.year, entered_end.month, entered_end.day
    )
    assert not dialog._ends_on.isEnabled()

    dialog._continue.click()
    dialog._meal_checks[Meal.DINNER].setChecked(True)
    app.processEvents()
    assert dialog.selected_period() == InclusiveDateRange(
        date(2026, 9, 4), entered_end
    )
    assert dialog.selected_periods()[Meal.DINNER].ends_on is None
    dialog.close()


def test_meal_card_body_label_and_checkbox_each_toggle_once(app: QApplication):
    roster = _roster()
    dialog = MealStatusChangeDialog(
        roster.person(PERSON_ID),
        roster,
        _histories(),
        ANCHOR_DAY,
        Meal.LUNCH,
        MealStatus.DELIVERY,
    )
    dialog.show()
    app.processEvents()

    dinner = dialog._meal_checks[Meal.DINNER]
    QTest.mouseClick(
        dialog._meal_cards[Meal.DINNER],
        Qt.MouseButton.LeftButton,
        pos=dialog._meal_cards[Meal.DINNER].rect().bottomRight(),
    )
    assert dinner.isChecked()

    QTest.mouseClick(
        dialog._current_labels[Meal.DINNER],
        Qt.MouseButton.LeftButton,
    )
    assert not dinner.isChecked()

    breakfast = dialog._meal_checks[Meal.BREAKFAST]
    QTest.mouseClick(breakfast, Qt.MouseButton.LeftButton)
    assert breakfast.isChecked()
    dialog.close()


def test_disabled_batch_meal_card_does_not_toggle(app: QApplication):
    roster = _roster()
    dialog = MealStatusChangeDialog(
        roster.person(PERSON_ID),
        roster,
        _histories(),
        ANCHOR_DAY,
        Meal.BREAKFAST,
        MealStatus.DELIVERY,
        targets=((PERSON_ID, Meal.BREAKFAST),),
    )
    dialog.show()
    app.processEvents()

    lunch_card = dialog._meal_cards[Meal.LUNCH]
    lunch = dialog._meal_checks[Meal.LUNCH]
    assert not lunch_card.isEnabled()
    assert not lunch.isChecked()
    QTest.mouseClick(lunch_card, Qt.MouseButton.LeftButton)
    QTest.mouseClick(dialog._current_labels[Meal.LUNCH], Qt.MouseButton.LeftButton)
    QTest.mouseClick(lunch, Qt.MouseButton.LeftButton)
    assert not lunch.isChecked()
    dialog.close()


def test_selected_meals_keep_independent_valid_periods(app: QApplication):
    roster = _roster()
    dialog = MealStatusChangeDialog(
        roster.person(PERSON_ID),
        roster,
        _histories(),
        ANCHOR_DAY,
        Meal.DINNER,
        MealStatus.DELIVERY,
    )
    try:
        dialog._meal_checks[Meal.BREAKFAST].setChecked(True)
        assert dialog.selected_periods() == {
            Meal.BREAKFAST: InclusiveDateRange(date(2026, 9, 4), None),
            Meal.DINNER: InclusiveDateRange(ANCHOR_DAY, None),
        }
        assert dialog._save_button.isEnabled()
        assert any(
            "실제 배송: 금요일" in label.text()
            for label in dialog.findChildren(QLabel)
        )
    finally:
        dialog.close()


def test_invalid_date_commit_is_prevented_and_deselect_keeps_meal_period(app: QApplication):
    roster = _roster()
    dialog = MealStatusChangeDialog(
        roster.person(PERSON_ID),
        roster,
        _histories(),
        ANCHOR_DAY,
        Meal.BREAKFAST,
        MealStatus.PAUSED,
    )
    try:
        breakfast_start = dialog._starts_on_by_meal[Meal.BREAKFAST]
        breakfast_end = dialog._ends_on_by_meal[Meal.BREAKFAST]
        breakfast_start.setFocus()
        QTest.keyClick(
            breakfast_start, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier
        )
        QTest.keyClicks(breakfast_start, "2026.09.03")
        QTest.keyClick(breakfast_start, Qt.Key.Key_Enter)
        breakfast_end.setDate(QDate(2026, 9, 4))
        assert breakfast_start.date() == QDate(2026, 9, 4)
        assert breakfast_start.text() == "2026.09.04"
        assert dialog._save_button.isEnabled()

        breakfast_start.setDate(QDate(2026, 9, 4))
        breakfast_end.setDate(QDate(2026, 9, 11))
        dialog._meal_checks[Meal.BREAKFAST].setChecked(False)
        dialog._meal_checks[Meal.BREAKFAST].setChecked(True)
        assert dialog.selected_periods()[Meal.BREAKFAST] == InclusiveDateRange(
            date(2026, 9, 4), date(2026, 9, 11)
        )
        assert dialog._save_button.isEnabled()
    finally:
        dialog.close()


def test_period_editor_uses_configured_delivery_mapping_not_service_weekdays(app):
    roster = _roster()
    roster = replace(roster, delivery_defaults=tuple(
        HouseholdDeliverySetting(
            Meal.BREAKFAST, (4, 5, 6), delivery_weekdays=(0, 1, 2, 3, 0, 3, 3)
        ) if setting.meal is Meal.BREAKFAST else setting
        for setting in roster.delivery_defaults
    ))
    dialog = MealStatusChangeDialog(
        roster.person(PERSON_ID), roster, _histories(), ANCHOR_DAY,
        Meal.BREAKFAST, MealStatus.DELIVERY,
    )
    try:
        editor = dialog._starts_on_by_meal[Meal.BREAKFAST]
        assert editor.date() == QDate(2026, 9, 3)
        editor.setFocus()
        editor.selectAll()
        QTest.keyClicks(editor, "2026.09.04")
        QTest.keyClick(editor, Qt.Key.Key_Return)
        assert editor.date() == QDate(2026, 9, 3)
        editor.selectAll()
        QTest.keyClicks(editor, "2026.09.07")
        QTest.keyClick(editor, Qt.Key.Key_Return)
        assert dialog.selected_period() == InclusiveDateRange(date(2026, 9, 7), None)
        assert dialog._save_button.isEnabled()
    finally:
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
