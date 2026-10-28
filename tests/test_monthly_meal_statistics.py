"""Monthly statistics use physical delivery entitlements and current DB state."""

import os
from dataclasses import replace
from datetime import UTC, date, datetime
from uuid import uuid4

import pytest

from src.domain.projections import roster_audit_changes, roster_entity_states, roster_from_historical_snapshot, status_event_state
from src.domain.revisions import EntityRef, HistoricalSnapshot, RevisionActor, RevisionMetadata, RevisionRecord, RevisionSource
from src.domain.roster import HouseholdDeliverySetting, InclusiveDateRange, Meal, MealEnrollment, Person, PersonProfile, RosterSnapshot
from src.domain.status import MealStatus, StatusEvent
from src.domain.school_calendar import SCHOOL_LUNCH_SCHEDULE_KIND, SchoolLunchScheduleMissingError
from src.services.monthly_meal_statistics import build_monthly_meal_statistics
from src.services.sqlite_delivery_report_service import prepare_delivery_report_data, prepare_physical_delivery_data
from src.storage.database_session import DatabaseSession
from tests.test_sqlite_delivery_report_service import _snapshot


def test_full_month_counts_bundled_breakfast_and_zero_dates_without_driver():
    snapshot, _ = _snapshot(meals=(Meal.DINNER, Meal.BREAKFAST, Meal.LUNCH), include_driver=False)
    result = build_monthly_meal_statistics(snapshot, 2026, 8)
    assert result.revision_id == snapshot.revision_id
    assert len(result.days) == 31
    assert result.days[0].day == date(2026, 8, 1)
    friday = result.days[20]
    assert friday.day == date(2026, 8, 21)
    assert friday.breakfast == 3
    assert result.days[21].breakfast == 0
    assert result.days[22].breakfast == 0
    assert (result.dinner_total, result.breakfast_total, result.lunch_total) == (
        sum(day.dinner for day in result.days),
        sum(day.breakfast for day in result.days),
        sum(day.lunch for day in result.days),
    )
    assert result.total == sum(day.total for day in result.days)


@pytest.mark.parametrize("missing", ["all", "one_school", "partial_period"])
def test_missing_lunch_schedule_only_zeros_affected_lunch(missing):
    snapshot, ids = _snapshot(people=2, meals=(Meal.DINNER, Meal.BREAKFAST, Meal.LUNCH))
    baseline = build_monthly_meal_statistics(snapshot, 2026, 8)
    states = dict(snapshot.entities)
    schedule_ref = next(ref for ref in states if ref.entity_kind == SCHOOL_LUNCH_SCHEDULE_KIND)
    if missing == "all":
        states.pop(schedule_ref)
        expected_lunch = 0
    elif missing == "one_school":
        roster = roster_from_historical_snapshot(snapshot)
        changed = replace(roster, persons=tuple(
            replace(person, profile=replace(person.profile, school_name="일정 없는 학교"))
            if person.person_id == ids[1] else person for person in roster.persons
        ))
        states.update(roster_entity_states(changed))
        expected_lunch = baseline.lunch_total // 2
    else:
        schedule = states[schedule_ref]
        states[schedule_ref] = {
            **schedule,
            "covered_periods": [{"starts_on": "2026-08-21", "ends_on": "2026-08-21"}],
            "days": [day for day in schedule["days"] if day["service_on"] == "2026-08-21"],
        }
        expected_lunch = 2
    result = build_monthly_meal_statistics(HistoricalSnapshot(snapshot.revision_id, states), 2026, 8)
    assert result.lunch_total == expected_lunch
    assert result.dinner_total == baseline.dinner_total > 0
    assert result.breakfast_total == baseline.breakfast_total > 0
    assert result.total == result.dinner_total + result.breakfast_total + expected_lunch
    assert result.total == sum(day.total for day in result.days)
    if missing == "all":
        assert all(day.lunch == 0 for day in result.days)
    elif missing == "partial_period":
        assert [day.day for day in result.days if day.lunch] == [date(2026, 8, 21)]


@pytest.mark.parametrize("prepare", [prepare_delivery_report_data, prepare_physical_delivery_data])
def test_other_delivery_calculations_remain_strict_about_missing_lunch(prepare):
    snapshot, _ = _snapshot(meals=(Meal.DINNER, Meal.BREAKFAST, Meal.LUNCH))
    missing = HistoricalSnapshot(snapshot.revision_id, {
        ref: state for ref, state in snapshot.entities.items() if ref.entity_kind != SCHOOL_LUNCH_SCHEDULE_KIND
    })
    with pytest.raises(SchoolLunchScheduleMissingError):
        prepare(missing, date(2026, 8, 1), date(2026, 8, 31), (Meal.DINNER, Meal.BREAKFAST, Meal.LUNCH))


def test_monthly_statistics_does_not_hide_invalid_calendar_data():
    snapshot, _ = _snapshot(meals=(Meal.LUNCH,))
    states = dict(snapshot.entities)
    ref = next(ref for ref in states if ref.entity_kind == SCHOOL_LUNCH_SCHEDULE_KIND)
    states[ref] = {**states[ref], "days": []}
    with pytest.raises(ValueError, match="평일 범위"):
        build_monthly_meal_statistics(HistoricalSnapshot(snapshot.revision_id, states), 2026, 8)


def test_shifted_next_month_meal_counts_on_actual_previous_month_date():
    snapshot, _ = _snapshot(meals=(Meal.DINNER,), include_driver=False, delivery_settings=(
        HouseholdDeliverySetting(Meal.DINNER, delivery_weekdays=(4, 1, 2, 3, 4, 5, 6)),
    ))
    july = build_monthly_meal_statistics(snapshot, 2026, 7)
    august = build_monthly_meal_statistics(snapshot, 2026, 8)
    assert july.days[-1].day == date(2026, 7, 31)
    assert july.days[-1].dinner >= 1  # August 3 entitlement physically delivered July 31.
    assert august.days[2].day == date(2026, 8, 3)
    assert august.days[2].dinner == 0
    assert all(item.day.month == 8 for item in august.days)


def test_paused_breakfast_is_excluded_and_leap_month_has_all_dates():
    snapshot, ids = _snapshot(include_driver=False)
    event = StatusEvent(
        uuid4(), snapshot.revision_id, ids[0], Meal.BREAKFAST, MealStatus.PAUSED,
        InclusiveDateRange(date(2026, 8, 21), date(2026, 8, 23)),
        datetime(2026, 8, 1, tzinfo=UTC),
    )
    states = dict(snapshot.entities)
    states[EntityRef("status.event", event.event_id)] = status_event_state(event)
    paused = build_monthly_meal_statistics(HistoricalSnapshot(snapshot.revision_id, states), 2026, 8)
    assert paused.days[20].breakfast == 0
    assert paused.days[20].total == 0
    leap = build_monthly_meal_statistics(snapshot, 2024, 2)
    assert len(leap.days) == 29 and leap.days[-1].day == date(2024, 2, 29)
    assert leap.total == 0


@pytest.fixture
def app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_month_dialog_changes_month_refreshes_and_shows_failure(app):
    from src.desktop_v2.monthly_statistics import MonthlyStatisticsDialog

    snapshot, _ = _snapshot(include_driver=False)
    calls = []
    broken = False

    def load(year, month):
        calls.append((year, month))
        if broken:
            raise ValueError("일정 자료를 읽지 못했습니다")
        return build_monthly_meal_statistics(snapshot, year, month)

    dialog = MonthlyStatisticsDialog(date(2026, 8, 21), load)
    assert calls == [(2026, 8)]
    assert dialog.table.rowCount() == 32
    assert dialog.table.item(20, 2).text() == "3"
    assert dialog.table.item(31, 0).text() == "월 합계"
    dialog._shift_month(1)
    assert calls[-1] == (2026, 9)
    assert dialog.table.rowCount() == 31
    broken = True
    dialog.refresh()
    assert dialog.table.rowCount() == 0
    assert "일정 자료" in dialog.error.text()
    dialog.close()


def test_month_dialog_refresh_reads_latest_database_revision(app, tmp_path):
    from src.desktop_v2.monthly_statistics import MonthlyStatisticsDialog

    revision = uuid4()
    person_id = uuid4()
    actor = RevisionActor("tester", "test-sid")
    initial = RosterSnapshot(
        revision,
        persons=(Person(person_id, PersonProfile("합성 대상")),),
        enrollments=(MealEnrollment(person_id, Meal.BREAKFAST, InclusiveDateRange(date(2026, 1, 1), None)),),
    )
    with DatabaseSession.create(tmp_path / "monthly.sqlite3", "password", "recovery") as session:
        session.append_revision(RevisionRecord(
            RevisionMetadata(revision, None, datetime(2026, 8, 1, tzinfo=UTC), None, actor, "initial", RevisionSource.IMPORT),
            roster_audit_changes(RosterSnapshot(uuid4()), initial),
        ))
        seen = []

        def load(year, month):
            current = session.database.current_roster()
            seen.append(current.revision_id)
            return build_monthly_meal_statistics(session.database.snapshot(current.revision_id), year, month)

        dialog = MonthlyStatisticsDialog(date(2026, 8, 21), load)
        assert dialog.summary.text().endswith("전체 12")
        next_revision = uuid4()
        changed = replace(initial, revision_id=next_revision, enrollments=())
        session.append_revision(RevisionRecord(
            RevisionMetadata(next_revision, revision, datetime(2026, 8, 22, tzinfo=UTC), None, actor, "enrollment removed", RevisionSource.UI),
            roster_audit_changes(initial, changed),
        ))
        dialog.refresh()
        assert seen == [revision, next_revision]
        assert dialog.summary.text().endswith("전체 0")
        assert not dialog.error.isVisible()
        dialog.close()



def test_monthly_dialog_changes_date_basis_and_retains_it_across_months(app):
    from src.desktop_v2.monthly_statistics import MonthlyStatisticsDialog
    from src.services.monthly_meal_statistics import MealCountBasis
    snapshot, _ = _snapshot()
    dialog = MonthlyStatisticsDialog(date(2026, 10, 1), lambda y, m: build_monthly_meal_statistics(snapshot, y, m))
    try:
        assert dialog.table.item(29, 2).text() == "3"
        assert dialog.table.horizontalHeaderItem(0).text() == "배송일"
        dialog.basis.setCurrentIndex(dialog.basis.findData(MealCountBasis.SERVICE))
        assert dialog.table.item(29, 2).text() == "1"
        assert dialog.table.horizontalHeaderItem(0).text() == "식사일"
        dialog._shift_month(1)
        assert dialog.basis.currentData() == MealCountBasis.SERVICE
        assert dialog.table.item(0, 2).text() == "1"
        dialog.basis.setCurrentIndex(dialog.basis.findData(MealCountBasis.DELIVERY))
        assert dialog.table.item(0, 2).text() == "0"
    finally:
        dialog.close()
