"""Local preference and archive view controls."""

import os
from datetime import UTC, date, datetime, timedelta
from uuid import UUID

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QSettings
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialogButtonBox

from src.desktop_v2.inspector_history import InspectorMealHistory
from src.desktop_v2.inactive_roster import InactiveRosterSettings, InactiveThresholdDialog
from src.desktop_v2.roster_window import RosterWindow
from src.domain.projections import ENROLLMENT_KIND, STATUS_EVENT_KIND
from src.domain.revisions import (
    AuditChange, ChangeOperation, HistoricalSnapshot, ImportBatch,
    RevisionActor, RevisionMetadata, RevisionRecord, RevisionSource,
)
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
    window._archive_threshold_button.click()
    save_days()
    app.processEvents()
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


def test_archive_threshold_keeps_zero_history_and_applies_history_boundary(app, tmp_path):
    selected_day = date(2026, 9, 30)
    zero_history = UUID("af000000-0000-0000-0000-000000000008")
    stopped_two_days = UUID("af000000-0000-0000-0000-000000000009")
    stopped_365_days = UUID("af000000-0000-0000-0000-00000000000a")
    roster = RosterSnapshot(
        REVISION,
        persons=(
            Person(zero_history, PersonProfile("가입·이력 없음")),
            Person(stopped_two_days, PersonProfile("이틀 미배송")),
            Person(stopped_365_days, PersonProfile("365일 미배송")),
        ),
        enrollments=(
            MealEnrollment(stopped_two_days, Meal.DINNER, InclusiveDateRange(date(2020, 1, 1))),
            MealEnrollment(stopped_365_days, Meal.DINNER, InclusiveDateRange(date(2020, 1, 1))),
        ),
    )

    def stopped_history(person_id, starts_on, event_id):
        event = StatusEvent(
            event_id, REVISION, person_id, Meal.DINNER, MealStatus.STOPPED,
            InclusiveDateRange(starts_on), datetime(2026, 9, 30, tzinfo=UTC),
        )
        return resolve_status_history(person_id, Meal.DINNER, (event,))

    histories = {
        (stopped_two_days, Meal.DINNER): stopped_history(
            stopped_two_days, selected_day - timedelta(days=1), UUID(int=103),
        ),
        (stopped_365_days, Meal.DINNER): stopped_history(
            stopped_365_days, selected_day - timedelta(days=364), UUID(int=104),
        ),
    }
    settings = QSettings(str(tmp_path / "threshold-boundary.ini"), QSettings.Format.IniFormat)
    settings.setValue(InactiveRosterSettings.SETTINGS_KEY, 1)
    window = RosterWindow(roster, histories, inactive_settings=settings)
    window._set_day(selected_day)
    window._archive_action.trigger()

    assert _visible_ids(window) == {zero_history, stopped_two_days, stopped_365_days}
    window._refresh_archive(days=2)
    assert _visible_ids(window) == {zero_history, stopped_two_days, stopped_365_days}
    window._refresh_archive(days=365)
    assert _visible_ids(window) == {zero_history, stopped_365_days}
    window.close()


def test_nonstatus_visible_history_preserves_archive_boundary_with_empty_cards(app, tmp_path):
    selected_day = date(2026, 9, 30)
    yesterday = selected_day - timedelta(days=1)
    person_id = UUID(int=801)
    revision_id = UUID(int=802)
    roster = RosterSnapshot(
        revision_id,
        persons=(Person(person_id, PersonProfile("가입기간만 수정")),),
        enrollments=(MealEnrollment(
            person_id, Meal.DINNER, InclusiveDateRange(yesterday, yesterday),
        ),),
    )
    delivery = StatusEvent(
        UUID(int=803), revision_id, person_id, Meal.DINNER, MealStatus.DELIVERY,
        InclusiveDateRange(yesterday, yesterday), datetime(2026, 9, 29, tzinfo=UTC),
    )
    histories = {(person_id, Meal.DINNER): resolve_status_history(
        person_id, Meal.DINNER, (delivery,),
    )}
    record = RevisionRecord(
        RevisionMetadata(
            revision_id, None, datetime(2026, 9, 30, tzinfo=UTC), selected_day,
            RevisionActor("tester", "sid"), "가입기간 수정", RevisionSource.UI,
        ),
        (AuditChange(
            ENROLLMENT_KIND, UUID(int=804), ChangeOperation.INSERT, None,
            {"person_id": str(person_id), "meal": Meal.DINNER.value,
             "periods": ({"starts_on": str(yesterday), "ends_on": str(yesterday)},)},
        ),),
    )

    class Database:
        def snapshot(self, requested_revision):
            return HistoricalSnapshot(requested_revision, {})

        def revision(self, requested_revision):
            return record

    settings = QSettings(str(tmp_path / "nonstatus-history.ini"), QSettings.Format.IniFormat)
    settings.setValue(InactiveRosterSettings.SETTINGS_KEY, 2)
    window = RosterWindow(roster, histories, inactive_settings=settings)
    window._set_day(selected_day)
    history = InspectorMealHistory(Database())
    provider = lambda person: history.rows_for(window.roster, person.person_id)
    presence = lambda target: history.has_visible_history(window.roster, target)

    assert provider(roster.person(person_id)) == ()
    assert presence(person_id)
    window.set_meal_change_history_provider(provider)
    assert _visible_ids(window) == set()  # Empty cards alone would archive early.
    window.set_meal_change_history_provider(provider, history_presence_provider=presence)
    assert _visible_ids(window) == {person_id}
    window.close()


@pytest.mark.parametrize("days", [2, 365])
def test_imported_enrollment_with_no_visible_history_archives_at_any_threshold(
    app, tmp_path, days,
):
    selected_day = date(2026, 9, 30)
    person_id = UUID("af000000-0000-0000-0000-00000000000b")
    revision_id = UUID("af000000-0000-0000-0000-00000000000c")
    roster = RosterSnapshot(
        revision_id,
        persons=(Person(person_id, PersonProfile("가져온 중식 대상", school_name="샘플초")),),
        enrollments=(
            MealEnrollment(
                person_id, Meal.LUNCH, InclusiveDateRange(date(2026, 8, 26)),
            ),
        ),
    )
    record = RevisionRecord(
        RevisionMetadata(
            revision_id, None, datetime(2026, 8, 26, tzinfo=UTC), date(2026, 8, 26),
            RevisionActor("tester", "sid"), "Excel 명단 가져오기", RevisionSource.IMPORT,
        ),
        (
            AuditChange(
                ENROLLMENT_KIND, UUID(int=105), ChangeOperation.INSERT, None,
                {
                    "person_id": str(person_id),
                    "meal": Meal.LUNCH.value,
                    "periods": ({"starts_on": "2026-08-26", "ends_on": None},),
                },
            ),
            AuditChange(
                STATUS_EVENT_KIND, UUID(int=108), ChangeOperation.INSERT, None,
                {
                    "person_id": str(person_id),
                    "meal": Meal.LUNCH.value,
                    "status": MealStatus.DELIVERY.value,
                    "starts_on": "2026-08-26",
                    "ends_on": None,
                },
            ),
        ),
        ImportBatch(
            UUID(int=106), revision_id, "roster.xlsx", "0" * 64, 1, "명단",
            "headers", {}, date(2026, 8, 26),
        ),
    )

    records = {revision_id: record}

    class Database:
        def snapshot(self, requested_revision_id):
            assert requested_revision_id in records
            return HistoricalSnapshot(requested_revision_id, {})

        def revision(self, requested_revision_id):
            return records[requested_revision_id]

    settings = QSettings(str(tmp_path / f"imported-{days}.ini"), QSettings.Format.IniFormat)
    settings.setValue(InactiveRosterSettings.SETTINGS_KEY, days)
    imported_delivery = StatusEvent(
        UUID(int=108), revision_id, person_id, Meal.LUNCH,
        MealStatus.DELIVERY, InclusiveDateRange(date(2026, 8, 26)),
        datetime(2026, 8, 26, tzinfo=UTC),
    )
    imported_histories = {
        (person_id, Meal.LUNCH): resolve_status_history(
            person_id, Meal.LUNCH, (imported_delivery,),
        ),
    }
    window = RosterWindow(roster, imported_histories, inactive_settings=settings)
    window._set_day(selected_day)
    assert _visible_ids(window) == {person_id}
    meal_history = InspectorMealHistory(Database())

    def provider(person):
        return meal_history.rows_for(window.roster, person.person_id)

    presence = lambda person_id: meal_history.has_visible_history(window.roster, person_id)
    window.set_meal_change_history_provider(provider, history_presence_provider=presence)

    assert not meal_history.rows_for(roster, person_id)
    assert window.table_model._contexts[person_id].meal_statuses == {
        Meal.DINNER: "-",
        Meal.BREAKFAST: "-",
        Meal.LUNCH: "-",
    }
    assert _visible_ids(window) == set()
    window._archive_action.trigger()
    assert _visible_ids(window) == {person_id}

    window.set_meal_change_history_provider(None)
    assert _visible_ids(window) == set()
    window.set_meal_change_history_provider(provider, history_presence_provider=presence)
    assert _visible_ids(window) == {person_id}

    manual_revision_id = UUID("af000000-0000-0000-0000-00000000000d")
    manual_record = RevisionRecord(
        RevisionMetadata(
            manual_revision_id, revision_id, datetime(2026, 9, 30, tzinfo=UTC), selected_day,
            RevisionActor("tester", "sid"), "중식 중지", RevisionSource.UI,
        ),
        (
            AuditChange(
                STATUS_EVENT_KIND, UUID(int=107), ChangeOperation.INSERT, None,
                {
                    "person_id": str(person_id),
                    "meal": Meal.LUNCH.value,
                    "status": MealStatus.STOPPED.value,
                    "starts_on": str(selected_day),
                    "ends_on": None,
                    "revision_id": str(manual_revision_id),
                    "recorded_at": datetime(2026, 9, 30, tzinfo=UTC).isoformat(),
                    "source_text": None,
                },
            ),
        ),
    )
    records[manual_revision_id] = manual_record
    manual_roster = RosterSnapshot(
        manual_revision_id,
        persons=roster.persons,
        enrollments=roster.enrollments,
    )
    stop = StatusEvent(
        UUID(int=107), manual_revision_id, person_id, Meal.LUNCH,
        MealStatus.STOPPED, InclusiveDateRange(selected_day),
        datetime(2026, 9, 30, tzinfo=UTC),
    )
    window.replace_roster(
        manual_roster,
        {(person_id, Meal.LUNCH): resolve_status_history(person_id, Meal.LUNCH, (stop,))},
    )

    assert meal_history.rows_for(manual_roster, person_id)
    assert _visible_ids(window) == set()
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

    window._archive_threshold_button.click()
    cancel()
    app.processEvents()
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
    group = next(action for action in menu.actions() if action.text() == "명단 관리")
    QTest.mouseMove(menu, pos=menu.actionGeometry(group).center())
    QTest.keyClick(menu, Qt.Key.Key_Right)
    app.processEvents()
    submenu = group.menu()
    assert submenu.isVisible()
    QTest.mouseClick(submenu, Qt.MouseButton.LeftButton, pos=submenu.actionGeometry(window._archive_action).center())
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
