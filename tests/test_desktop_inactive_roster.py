"""Local preference and archive view controls."""

import os
from datetime import UTC, date, datetime, timedelta
from uuid import UUID

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QSettings
from PySide6.QtCore import QTimer
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialogButtonBox

from src.desktop_v2.inactive_roster import InactiveRosterSettings, InactiveThresholdDialog
from src.desktop_v2.roster_window import RosterWindow
from src.domain.roster import (
    InclusiveDateRange, Meal, MealEnrollment, Person, PersonProfile, RosterSnapshot,
)
from src.domain.status import MealStatus, StatusEvent, resolve_status_history
from src.domain.entitlements import LunchDayRule, SchoolLunchMark
from src.domain.school_calendar import SchoolLunchCalendar, SchoolLunchDay, SchoolLunchSchoolSchedule


REVISION = UUID("af000000-0000-0000-0000-000000000001")
ARCHIVED = UUID("af000000-0000-0000-0000-000000000002")
ACTIVE = UUID("af000000-0000-0000-0000-000000000003")


@pytest.fixture(scope="module")
def app():
    yield QApplication.instance() or QApplication([])


def test_archive_settings_default_migrate_and_preserve_legacy(app, tmp_path):
    settings = QSettings(str(tmp_path / "archive.ini"), QSettings.Format.IniFormat)
    archive = InactiveRosterSettings(settings)
    assert archive.days == 365
    settings.setValue(archive.LEGACY_KEY, 36)
    assert archive.days == 1095
    assert settings.value(archive.LEGACY_KEY) == 36
    archive.save_days(1080)
    settings.sync()
    assert InactiveRosterSettings(settings).days == 1080


def test_invalid_saved_threshold_uses_default(app, tmp_path):
    settings = QSettings(str(tmp_path / "archive.ini"), QSettings.Format.IniFormat)
    settings.setValue(InactiveRosterSettings.SETTINGS_KEY, 36501)
    archive = InactiveRosterSettings(settings)
    assert archive.days == 365
    assert settings.value(InactiveRosterSettings.SETTINGS_KEY) == 36501
    dialog = InactiveThresholdDialog(archive.days)
    assert dialog.days.minimum() == 1
    assert dialog.days.maximum() == 36500


@pytest.mark.parametrize("legacy,expected", [(12, 365), (1, 30), (1200, 36500)])
def test_legacy_month_values_convert_without_removing_original(app, tmp_path, legacy, expected):
    settings = QSettings(str(tmp_path / f"archive-{legacy}.ini"), QSettings.Format.IniFormat)
    settings.setValue(InactiveRosterSettings.LEGACY_KEY, legacy)
    assert InactiveRosterSettings(settings).days == expected
    assert settings.value(InactiveRosterSettings.SETTINGS_KEY, type=int) == expected
    assert settings.value(InactiveRosterSettings.LEGACY_KEY, type=int) == legacy


def test_invalid_legacy_value_remains_available_for_recovery(app, tmp_path):
    settings = QSettings(str(tmp_path / "archive.ini"), QSettings.Format.IniFormat)
    settings.setValue(InactiveRosterSettings.LEGACY_KEY, "broken")
    assert InactiveRosterSettings(settings).days == 365
    assert settings.value(InactiveRosterSettings.LEGACY_KEY) == "broken"
    assert not settings.contains(InactiveRosterSettings.SETTINGS_KEY)


def _roster_and_histories():
    roster = RosterSnapshot(
        REVISION,
        persons=(
            Person(ARCHIVED, PersonProfile("보관 대상")),
            Person(ACTIVE, PersonProfile("배송 대상")),
        ),
        enrollments=(
            MealEnrollment(ARCHIVED, Meal.DINNER, InclusiveDateRange(date(2020, 1, 1))),
            MealEnrollment(ACTIVE, Meal.LUNCH, InclusiveDateRange(date(2020, 1, 1))),
        ),
    )
    stop = StatusEvent(
        UUID("af000000-0000-0000-0000-000000000004"), REVISION, ARCHIVED,
        Meal.DINNER, MealStatus.STOPPED, InclusiveDateRange(date(2020, 1, 2)),
        datetime(2020, 1, 2, tzinfo=UTC),
    )
    histories = {
        (ARCHIVED, Meal.DINNER): resolve_status_history(ARCHIVED, Meal.DINNER, (stop,))
    }
    return roster, histories


def _visible_ids(window):
    return {
        window.table_model.person_at(row).person_id
        for row in range(window.table_model.rowCount())
    }


def test_window_archive_switch_follows_view_day_and_restores_after_roster_refresh(app, tmp_path):
    settings = QSettings(str(tmp_path / "archive.ini"), QSettings.Format.IniFormat)
    roster, histories = _roster_and_histories()
    window = RosterWindow(roster, histories, inactive_settings=settings)

    assert window._archive_action.isChecked() is False
    assert window._archive_threshold_button.isHidden()
    assert window._roster_title.text() == "대상자 명단"
    assert _visible_ids(window) == {ACTIVE}
    window._archive_action.trigger()
    assert not window._archive_threshold_button.isHidden()
    assert window._roster_title.text() == "장기 미배송 보관함 (1명)"
    assert _visible_ids(window) == {ARCHIVED}
    window._on_column_search_changed("name", "보관")
    assert _visible_ids(window) == {ARCHIVED}
    window._archive_action.trigger()
    assert _visible_ids(window) == set()
    window._archive_action.trigger()
    assert _visible_ids(window) == {ARCHIVED}
    window._on_column_search_changed("name", "")
    window._set_day(date(2020, 1, 1))
    assert _visible_ids(window) == set()
    window._set_day(date.today() - timedelta(days=45))
    assert _visible_ids(window) == {ARCHIVED}

    def save_days():
        dialog = window.findChild(InactiveThresholdDialog)
        dialog.days.setValue(36500)
        assert window._roster_title.text() == "장기 미배송 보관함 (0명)"
        assert _visible_ids(window) == set()
        assert not settings.contains(InactiveRosterSettings.SETTINGS_KEY)
        dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Save).click()
    QTimer.singleShot(0, save_days)
    window._archive_threshold_button.click()
    assert settings.value(InactiveRosterSettings.SETTINGS_KEY, type=int) == 36500
    assert window._roster_title.text() == "장기 미배송 보관함 (0명)"
    assert _visible_ids(window) == set()
    window._archive_settings.save_days(365)
    window._refresh_archive()
    assert _visible_ids(window) == {ARCHIVED}

    window.replace_roster(roster, {})
    assert window._roster_title.text() == "장기 미배송 보관함 (0명)"
    assert _visible_ids(window) == set()
    window._archive_action.trigger()
    assert window._archive_threshold_button.isHidden()
    assert _visible_ids(window) == {ARCHIVED, ACTIVE}
    window.close()


def test_one_day_archive_matches_selected_day_status_cells(app, tmp_path):
    selected_day = date(2026, 9, 30)
    mixed = UUID("af000000-0000-0000-0000-000000000005")
    all_dash = UUID("af000000-0000-0000-0000-000000000006")
    paused = UUID("af000000-0000-0000-0000-000000000007")
    roster = RosterSnapshot(
        REVISION,
        persons=(
            Person(mixed, PersonProfile("미배송·미제공", school_name="샘플초")),
            Person(all_dash, PersonProfile("전체 미제공")),
            Person(paused, PersonProfile("일시중지")),
        ),
        enrollments=(
            MealEnrollment(mixed, Meal.DINNER, InclusiveDateRange(date(2024, 1, 1))),
            MealEnrollment(mixed, Meal.LUNCH, InclusiveDateRange(date(2024, 1, 1))),
            MealEnrollment(paused, Meal.DINNER, InclusiveDateRange(date(2024, 1, 1))),
        ),
    )
    histories = {
        (mixed, Meal.DINNER): resolve_status_history(mixed, Meal.DINNER, (
            StatusEvent(UUID(int=101), REVISION, mixed, Meal.DINNER,
                        MealStatus.STOPPED, InclusiveDateRange(selected_day),
                        datetime(2026, 9, 30, tzinfo=UTC)),
        )),
        (paused, Meal.DINNER): resolve_status_history(paused, Meal.DINNER, (
            StatusEvent(UUID(int=102), REVISION, paused, Meal.DINNER,
                        MealStatus.PAUSED, InclusiveDateRange(selected_day, selected_day),
                        datetime(2026, 9, 30, tzinfo=UTC)),
        )),
    }
    settings = QSettings(str(tmp_path / "one-day.ini"), QSettings.Format.IniFormat)
    settings.setValue(InactiveRosterSettings.SETTINGS_KEY, 1)
    window = RosterWindow(roster, histories, inactive_settings=settings)
    window._set_day(selected_day)

    assert window.table_model._contexts[mixed].meal_statuses == {
        Meal.DINNER: MealStatus.STOPPED.value,
        Meal.BREAKFAST: "-",
        Meal.LUNCH: "-",
    }
    assert _visible_ids(window) == {paused}
    window._archive_action.trigger()
    assert _visible_ids(window) == {mixed, all_dash}
    calendar = SchoolLunchCalendar((SchoolLunchSchoolSchedule(
        "샘플초", (InclusiveDateRange(selected_day, selected_day),),
        (SchoolLunchDay(selected_day, LunchDayRule(SchoolLunchMark.VENDOR_REQUIRED)),),
    ),))
    window.set_school_calendar(calendar)
    assert _visible_ids(window) == {all_dash}
    window.set_school_calendar(SchoolLunchCalendar())
    assert _visible_ids(window) == {mixed, all_dash}
    window._set_day(selected_day - timedelta(days=1))
    assert _visible_ids(window) == {all_dash}
    window._archive_action.trigger()
    assert _visible_ids(window) == {mixed, paused}
    window.close()


@pytest.mark.parametrize("close_method", ["cancel", "escape", "close"])
def test_threshold_cancel_restores_filtered_roster(app, tmp_path, close_method):
    settings = QSettings(str(tmp_path / "archive.ini"), QSettings.Format.IniFormat)
    window = RosterWindow(*_roster_and_histories(), inactive_settings=settings)
    window._search_input.setText("보관")
    window._on_column_search_changed("name", "대상")
    assert _visible_ids(window) == set()

    def cancel():
        dialog = window.findChild(InactiveThresholdDialog)
        dialog.days.setValue(36500)
        assert _visible_ids(window) == {ARCHIVED}
        assert window._roster_title.text() == "대상자 명단"
        assert not settings.contains(InactiveRosterSettings.SETTINGS_KEY)
        if close_method == "cancel":
            dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Cancel).click()
        elif close_method == "escape":
            QTest.keyClick(dialog, Qt.Key.Key_Escape)
        else:
            dialog.close()

    QTimer.singleShot(0, cancel)
    window._archive_threshold_button.click()
    assert window._archive_settings.days == 365
    assert not settings.contains(InactiveRosterSettings.SETTINGS_KEY)
    assert window._search_input.text() == "보관"
    assert window.table_model._column_searches["name"] == "대상"
    assert _visible_ids(window) == set()
    window.close()


def test_more_menu_click_and_global_search_combine_with_header_filter(app, tmp_path):
    settings = QSettings(str(tmp_path / "archive.ini"), QSettings.Format.IniFormat)
    window = RosterWindow(*_roster_and_histories(), inactive_settings=settings)
    window.show()
    app.processEvents()
    assert window._search_input.isVisible()
    assert window._search_input.placeholderText() == "성명, 학교, 전화, 주소 검색"
    menu = window._more_menu
    assert "보관 기준 설정" not in {action.text() for action in menu.actions()}
    menu.popup(window._more_button.mapToGlobal(window._more_button.rect().bottomLeft()))
    app.processEvents()
    QTest.mouseClick(menu, Qt.MouseButton.LeftButton, pos=menu.actionGeometry(window._archive_action).center())
    assert window._archive_action.isChecked()
    assert _visible_ids(window) == {ARCHIVED}
    window._search_input.setText("보관")
    window._on_column_search_changed("name", "대상")
    assert _visible_ids(window) == {ARCHIVED}
    window._on_column_search_changed("name", "배송")
    assert _visible_ids(window) == set()
    window._on_column_search_changed("name", "")
    assert _visible_ids(window) == {ARCHIVED}
    window.close()
