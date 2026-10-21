"""Manual row creation exposes its household and delivery start independently."""

import os
from dataclasses import replace
from datetime import UTC, date, datetime
from uuid import uuid4

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from src.desktop_v2.inspector_history import InspectorMealHistory
from src.desktop_v2.meal_history import build_person_meal_history_rows
from src.desktop_v2.roster_window import RosterWindow
from src.domain.projections import roster_audit_changes, status_event_audit_change
from src.domain.revisions import RevisionActor, RevisionMetadata, RevisionRecord, RevisionSource
from src.domain.roster import InclusiveDateRange, Meal, Person, PersonProfile, RosterSnapshot
from src.domain.roster_row_changes import build_roster_row_revision
from src.domain.status import MealStatus, StatusEvent
from src.storage.encrypted_database import EncryptedDatabase


START = date(2026, 10, 5)
BREAKFAST_START = date(2026, 10, 9)
BEFORE = date(2026, 9, 1)
RECORDED = datetime(2026, 10, 1, tzinfo=UTC)
ACTOR = RevisionActor("담당자", "test")


def _addition(**kwargs):
    original = RosterSnapshot(uuid4(), persons=(Person(uuid4(), PersonProfile("기존 대상")),))
    person = Person(uuid4(), PersonProfile("수동 추가"))
    added = build_roster_row_revision(
        original, actor=ACTOR, recorded_at=RECORDED, effective_on=RECORDED.date(),
        revision_id=uuid4(), add_person=person,
        enroll_meals=(Meal.DINNER, Meal.BREAKFAST),
        delivery_starts_on=START,
        delivery_starts_by_meal={
            Meal.DINNER: START,
            Meal.BREAKFAST: BREAKFAST_START,
        },
        **kwargs,
    )
    return original, person, added


def test_manual_add_history_is_visible_after_save_and_reopen(tmp_path):
    original, person, added = _addition()
    initial = RevisionRecord(
        RevisionMetadata(original.revision_id, None, RECORDED, BEFORE, ACTOR, "초기", RevisionSource.UI),
        roster_audit_changes(None, original),
    )
    path = tmp_path / "manual.db"
    with EncryptedDatabase.create(path, b"k" * 32) as database:
        database.append_revision(initial)
        history = InspectorMealHistory(database)
        assert history.rows_for(original, person.person_id) == ()
        database.append_revision(added.record)
        stored = database.current_roster()
        rows = history.rows_for(stored, person.person_id)
        assert {tuple(row) for row in rows} == {
            ("석식", "시작", "2026-10-05 ~ "), ("조식", "시작", "2026-10-09 ~ "),
        }
        assert database.status_events(person_id=person.person_id) == ()
    with EncryptedDatabase.open(path, b"k" * 32) as database:
        assert InspectorMealHistory(database).rows_for(database.current_roster(), person.person_id) == rows
    personal = build_person_meal_history_rows((added.record,), person.person_id)
    assert {row[:3] for row in personal} == {tuple(row) for row in rows}
    assert all(row[3] == ACTOR.username for row in personal)


@pytest.mark.parametrize("source,decisions", [
    (RevisionSource.IMPORT, {"import_purpose": "database"}),
    (RevisionSource.IMPORT, {"import_purpose": "lunch"}),
    (RevisionSource.UI, {}),
    (RevisionSource.UI, {"roster_row_change": {"operation": "delete"}}),
])
def test_start_rows_only_come_from_manual_add(source, decisions):
    _, person, added = _addition()
    record = replace(added.record, metadata=replace(added.record.metadata, source=source, decisions=decisions))
    assert build_person_meal_history_rows((record,), person.person_id) == ()


@pytest.mark.parametrize("status,category", [
    (MealStatus.DELIVERY, "시작"), (MealStatus.PAUSED, "일시중지"), (MealStatus.STOPPED, "중지"),
])
def test_explicit_initial_status_is_not_duplicated_as_start(status, category):
    _, person, added = _addition()
    end = date(2026, 10, 8) if status is MealStatus.PAUSED else None
    event = StatusEvent(uuid4(), added.record.metadata.revision_id, person.person_id,
                        Meal.DINNER, status, InclusiveDateRange(START, end), RECORDED)
    record = replace(added.record, changes=(*added.record.changes, status_event_audit_change(event)))
    rows = build_person_meal_history_rows((record,), person.person_id)
    assert len(rows) == 2
    assert {row[:2] for row in rows} == {("석식", category), ("조식", "시작")}


def test_new_row_inspector_shows_self_before_delivery_start(tmp_path):
    app = QApplication.instance() or QApplication([])
    original, person, added = _addition()
    settings = QSettings(str(tmp_path / "view.ini"), QSettings.Format.IniFormat)
    window = RosterWindow(original, inactive_settings=settings)
    try:
        window._set_day(BEFORE)
        window.replace_roster(added.snapshot)
        row = next(index for index in range(window.table_model.rowCount())
                   if window.table_model.person_at(index).person_id == person.person_id)
        window._table.selectRow(row)
        app.processEvents()
        assert window._household_list.count() == 1
        assert window._household_list.item(0).text() == "수동 추가   -"
        assert window.roster.household_for(person.person_id, on=BEFORE) is not None
        assert window.roster.meals_for(person.person_id, on=BEFORE) == ()
        window._set_day(START)
        row = next(index for index in range(window.table_model.rowCount())
                   if window.table_model.person_at(index).person_id == person.person_id)
        window._table.selectRow(row)
        app.processEvents()
        assert "석식" in window._household_list.item(0).text()
        assert "조식" not in window._household_list.item(0).text()
        window._set_day(BREAKFAST_START)
        row = next(index for index in range(window.table_model.rowCount())
                   if window.table_model.person_at(index).person_id == person.person_id)
        window._table.selectRow(row)
        app.processEvents()
        assert "석식" in window._household_list.item(0).text()
        assert "조식" in window._household_list.item(0).text()
    finally:
        window.close()


@pytest.mark.parametrize("day,expected_meals", [
    (date(2026, 10, 12), set()),
    (date(2026, 10, 13), {"석식", "조식"}),
    (date(2026, 10, 15), {"석식", "조식"}),
    (date(2026, 10, 16), {"석식", "조식", "중식"}),
])
def test_legacy_household_before_saved_start_keeps_individual_meal_dates(tmp_path, day, expected_meals):
    from src.domain.entitlements import LunchDayRule, SchoolLunchMark
    from src.domain.roster import Household, HouseholdMembership, MealEnrollment
    from src.domain.school_calendar import SchoolLunchCalendar, SchoolLunchDay, SchoolLunchSchoolSchedule

    app = QApplication.instance() or QApplication([])
    person = Person(uuid4(), PersonProfile("이전 추가", school_name="테스트초"))
    sibling = Person(uuid4(), PersonProfile("가구원"))
    household = Household(uuid4())
    first, last = date(2026, 10, 13), date(2026, 10, 16)
    roster = RosterSnapshot(
        uuid4(), persons=(person, sibling), households=(household,),
        memberships=(
            HouseholdMembership(person.person_id, household.household_id, InclusiveDateRange(last)),
            HouseholdMembership(sibling.person_id, household.household_id, InclusiveDateRange(first)),
        ),
        enrollments=tuple(MealEnrollment(person.person_id, meal, InclusiveDateRange(
            last if meal is Meal.LUNCH else first,
        )) for meal in Meal),
    )
    record = RevisionRecord(
        RevisionMetadata(roster.revision_id, None, RECORDED, first, ACTOR, "기존 저장값", RevisionSource.UI),
        roster_audit_changes(None, roster),
    )
    path = tmp_path / "legacy.db"
    with EncryptedDatabase.create(path, b"k" * 32) as database:
        database.append_revision(record)
    with EncryptedDatabase.open(path, b"k" * 32) as database:
        saved = database.current_roster()
        calendar = SchoolLunchCalendar((SchoolLunchSchoolSchedule(
            "테스트초", (InclusiveDateRange(first, last),),
            tuple(SchoolLunchDay(date(2026, 10, number), LunchDayRule(SchoolLunchMark.VENDOR_REQUIRED))
                  for number in (13, 14, 15, 16)),
        ),))
        window = RosterWindow(saved, school_calendar=calendar,
                              inactive_settings=QSettings(str(tmp_path / "view.ini"), QSettings.Format.IniFormat))
        try:
            window._set_day(day)
            row = next(index for index in range(window.table_model.rowCount())
                       if window.table_model.person_at(index).person_id == person.person_id)
            window._table.selectRow(row)
            app.processEvents()
            texts = [window._household_list.item(i).text() for i in range(window._household_list.count())]
            assert len(texts) == 2
            assert "가구원   -" in texts
            text = next(value for value in texts if value.startswith("이전 추가   "))
            meals = text.split("   ", 1)[1]
            assert (set() if meals == "-" else set(meals.split(" · "))) == expected_meals
            assert database.current_roster() == saved
            assert (saved.household_for(person.person_id, on=day) is not None) == (day >= last)
        finally:
            window.close()


@pytest.mark.parametrize("number,expected_group", [
    (9, "first"), (10, "first"), (15, "first"),
    (16, None), (19, None), (20, "second"), (25, "second"), (26, None),
])
def test_inspector_household_context_preserves_transfers_gaps_and_ends(number, expected_group):
    from src.desktop_v2.roster_window import _inspector_household_members
    from src.domain.roster import Household, HouseholdMembership

    person, old_peer, new_peer = (Person(uuid4(), PersonProfile(name)) for name in ("대상", "이전 가구원", "새 가구원"))
    first, second = Household(uuid4()), Household(uuid4())
    roster = RosterSnapshot(
        uuid4(), persons=(person, old_peer, new_peer), households=(first, second),
        memberships=(
            HouseholdMembership(person.person_id, second.household_id, InclusiveDateRange(date(2026, 10, 20), date(2026, 10, 25))),
            HouseholdMembership(person.person_id, first.household_id, InclusiveDateRange(date(2026, 10, 10), date(2026, 10, 15))),
            HouseholdMembership(old_peer.person_id, first.household_id),
            HouseholdMembership(new_peer.person_id, second.household_id),
        ),
    )
    expected = () if expected_group is None else (person, old_peer if expected_group == "first" else new_peer)
    assert _inspector_household_members(roster, person.person_id, date(2026, 10, number)) == expected


def test_inspector_does_not_infer_household_without_membership():
    from src.desktop_v2.roster_window import _inspector_household_members
    person = Person(uuid4(), PersonProfile("가구 기록 없음"))
    roster = RosterSnapshot(uuid4(), persons=(person,))
    assert _inspector_household_members(roster, person.person_id, BEFORE) == ()
