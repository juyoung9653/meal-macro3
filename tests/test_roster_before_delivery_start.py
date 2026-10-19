"""Roster visibility before first delivery must not grant meal eligibility."""

import os
from datetime import UTC, date, datetime
from uuid import UUID

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from src.desktop_v2.roster_window import RosterWindow
from src.domain.entitlements import EntitlementExclusion, decide_entitlement
from src.domain.roster import (
    InclusiveDateRange, Meal, MealEnrollment, Person, PersonProfile, RosterSnapshot,
)
from src.domain.status import MealStatus, StatusEvent, resolve_status_history
from src.services.inactive_roster import inactive_person_ids


REVISION = UUID(int=501)
IMPORTED = UUID(int=502)
ADDED = UUID(int=503)
STARTS = {IMPORTED: date(2026, 9, 15), ADDED: date(2026, 10, 5)}
MEALS = {IMPORTED: Meal.DINNER, ADDED: Meal.BREAKFAST}


@pytest.fixture(scope="module")
def app():
    yield QApplication.instance() or QApplication([])


def _roster():
    return RosterSnapshot(
        REVISION,
        persons=(
            Person(IMPORTED, PersonProfile("초기 명단")),
            Person(ADDED, PersonProfile("추가 대상")),
        ),
        enrollments=tuple(
            MealEnrollment(person_id, MEALS[person_id], InclusiveDateRange(starts_on))
            for person_id, starts_on in STARTS.items()
        ),
    )


def _visible_ids(window):
    return {
        window.table_model.person_at(row).person_id
        for row in range(window.table_model.rowCount())
    }


@pytest.mark.parametrize("days", [1, 365])
@pytest.mark.parametrize("day", [
    date(2026, 8, 31), date(2026, 9, 1), date(2026, 9, 14),
    date(2026, 9, 15), date(2026, 9, 30), date(2026, 10, 4),
    date(2026, 10, 5), date(2026, 10, 31),
])
def test_imported_and_added_rows_stay_visible_without_early_delivery(app, tmp_path, day, days):
    roster = _roster()
    settings = QSettings(str(tmp_path / "view.ini"), QSettings.Format.IniFormat)
    window = RosterWindow(roster, inactive_settings=settings)
    try:
        window._set_day(day)
        # Startup installs this provider; initial import supplies no visible changes.
        window.set_meal_change_history_provider(lambda person: ())
        window._refresh_archive(days=days)
        assert _visible_ids(window) == {IMPORTED, ADDED}
        counts = window.table_model.meal_status_counts()
        for person_id, starts_on in STARTS.items():
            meal = MEALS[person_id]
            started = day >= starts_on
            context = window.table_model._contexts[person_id]
            assert context.meal_statuses[meal] == ("배송" if started else "-")
            assert counts[meal][MealStatus.DELIVERY.value] == int(started)
            assert counts[meal][MealStatus.PAUSED.value] == 0
            decision = decide_entitlement(roster, person_id, meal, day)
            assert decision.entitled is started
            if not started:
                assert all(value == "-" for value in context.meal_statuses.values())
                assert EntitlementExclusion.NOT_ENROLLED in decision.exclusions
                assert roster.meals_for(person_id, on=day) == ()
        # An explicit membership filter still excludes future meal enrollments.
        window.table_model.set_context(day, Meal.BREAKFAST)
        window.table_model.set_active_only(True)
        assert _visible_ids(window) == ({ADDED} if day >= STARTS[ADDED] else set())
        window.table_model.set_active_only(False)
        window._search_input.setText("추가")
        assert _visible_ids(window) == {ADDED}
        assert roster == _roster()
    finally:
        window.close()


@pytest.mark.parametrize("days", [1, 365])
@pytest.mark.parametrize("has_visible_history", [None, lambda person_id: False, lambda person_id: True])
def test_future_only_enrollment_is_not_archived(days, has_visible_history):
    assert inactive_person_ids(
        _roster(), {}, as_of=date(2026, 9, 1), days=days,
        has_visible_history=has_visible_history,
    ) == frozenset()


def test_future_reenrollment_does_not_unhide_previously_stopped_person():
    roster = RosterSnapshot(
        REVISION,
        persons=(Person(IMPORTED, PersonProfile("중지 대상")),),
        enrollments=(
            MealEnrollment(IMPORTED, Meal.DINNER, InclusiveDateRange(date(2024, 1, 1), date(2024, 12, 31))),
            MealEnrollment(IMPORTED, Meal.DINNER, InclusiveDateRange(date(2027, 1, 1))),
        ),
    )
    assert inactive_person_ids(roster, {}, as_of=date(2026, 9, 1)) == frozenset({IMPORTED})


@pytest.mark.parametrize("status", [MealStatus.PAUSED, MealStatus.STOPPED])
def test_start_date_respects_saved_status_and_archive_filter(app, tmp_path, status):
    start = STARTS[ADDED]
    event = StatusEvent(
        UUID(int=504), REVISION, ADDED, Meal.BREAKFAST, status,
        InclusiveDateRange(start, date(2026, 10, 7) if status is MealStatus.PAUSED else None),
        datetime(2026, 10, 5, tzinfo=UTC),
    )
    histories = {(ADDED, Meal.BREAKFAST): resolve_status_history(ADDED, Meal.BREAKFAST, (event,))}
    settings = QSettings(str(tmp_path / "view.ini"), QSettings.Format.IniFormat)
    window = RosterWindow(_roster(), histories, inactive_settings=settings)
    try:
        window.set_meal_change_history_provider(lambda person: ())
        window._set_day(date(2026, 10, 4))
        assert ADDED in _visible_ids(window)
        assert window.table_model._contexts[ADDED].meal_statuses[Meal.BREAKFAST] == "-"
        window._set_day(start)
        assert window.table_model.effective_status_on(ADDED, Meal.BREAKFAST, start) is status
        if status is MealStatus.STOPPED:
            assert ADDED not in _visible_ids(window)
            window._archive_action.trigger()
            assert _visible_ids(window) == {ADDED}
        else:
            assert ADDED in _visible_ids(window)
            assert window.table_model._contexts[ADDED].meal_statuses[Meal.BREAKFAST] == "일시중지"
    finally:
        window.close()
