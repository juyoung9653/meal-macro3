"""Incremental lunch imports are bounded by a selected saved vacation."""

from dataclasses import replace
from datetime import UTC, date, datetime
from uuid import uuid4

import pytest

from src.domain.entitlements import SchoolLunchMark, decide_entitlement
from src.domain.projections import roster_entity_states
from src.domain.revisions import HistoricalSnapshot, RevisionActor
from src.domain.roster import InclusiveDateRange, Meal, MealEnrollment
from src.domain.status import MealStatus
from src.importing.application import apply_reconciliation
from src.importing.lunch_values import lunch_value_rows
from src.importing.reconciliation import stage_lunch_reconciliation
from src.importing.revision import ImportPurpose, build_import_revision
from src.importing.school_calendar import SchoolVacationRow
from src.importing.vacation_lunch import LunchDuplicatePolicy
from src.services.school_calendar_history import SchoolCalendarImportEntry
from tests.test_driver_lunch_periods import _calendar
from tests.test_vacation_lunch_import import _preview, _roster, KEEP, OMIT, RESTORE


def calendar_entry():
    return SchoolCalendarImportEntry(
        uuid4(), "변경 12", datetime(2026, 7, 1, tzinfo=UTC), "합성_여름방학.xlsx",
        (SchoolVacationRow(2, "기존 학교", date(2026, 8, 17), SchoolLunchMark.VENDOR_REQUIRED,
                           date(2026, 8, 26), SchoolLunchMark.VENDOR_REQUIRED),))


def _build(roster, preview, *, policy=LunchDuplicatePolicy.UPDATE, entry=None, events=(), decisions=None):
    entry = entry or calendar_entry()
    states = dict(roster_entity_states(roster))
    _calendar(states, "기존 학교", date(2026, 8, 17), date(2026, 8, 26))
    historical = HistoricalSnapshot(roster.revision_id, states)
    plan = stage_lunch_reconciliation(roster, preview.observations, effective_on=entry.period.starts_on)
    revision_id = uuid4()
    candidate = apply_reconciliation(roster, plan, new_revision_id=revision_id,
                                     decisions=decisions or {}, custom_fields=preview.custom_fields)
    choices = {row.key: row.proposed for row in lunch_value_rows(roster, candidate, plan) if not row.automatic}
    return build_import_revision(
        roster, preview, plan, decisions=decisions or {}, actor=RevisionActor("tester", "test"),
        recorded_at=datetime(2026, 8, 1, tzinfo=UTC), revision_id=revision_id, batch_id=uuid4(),
        purpose=ImportPurpose.VACATION_LUNCH, lunch_calendar_entry=entry,
        lunch_duplicate_policy=policy, lunch_value_choices=choices, lunch_status_events=events,
        household_grouping_snapshot=historical)


def test_first_and_second_import_accumulate_and_do_not_stop_missing_people():
    roster = replace(_roster(), enrollments=(MealEnrollment(OMIT, Meal.BREAKFAST),))
    first = _build(roster, _preview("유지"))
    second = _build(first.result.snapshot, _preview("제외"))
    for pid in (KEEP, OMIT):
        for day, expected in ((16, False), (17, True), (26, True), (27, False)):
            assert decide_entitlement(second.result.snapshot, pid, Meal.LUNCH, date(2026, 8, day)).entitled is expected
    assert second.result.snapshot.person(KEEP) == first.result.snapshot.person(KEEP)
    assert Meal.BREAKFAST in second.result.snapshot.meals_for(OMIT, on=date(2026, 8, 27))
    assert Meal.LUNCH not in second.result.snapshot.meals_for(RESTORE, on=date(2026, 8, 20))
    assert second.record.metadata.decisions["vacation_lunch"]["stopped"] == ()
    assert all(c.after["status"] == MealStatus.DELIVERY.value and c.after["ends_on"] == "2026-08-26"
               for c in second.record.changes if c.entity_kind == "status.event")


def test_keep_preserves_existing_values_and_manual_period_update_replaces_them():
    roster = _build(replace(_roster(), enrollments=()), _preview("유지")).result.snapshot
    roster = replace(roster, persons=tuple(
        replace(p, lunch_period=InclusiveDateRange(date(2026, 8, 19), date(2026, 8, 21))) if p.person_id == KEEP else p
        for p in roster.persons), enrollments=(MealEnrollment(
            KEEP, Meal.LUNCH, InclusiveDateRange(date(2026, 8, 19), date(2026, 8, 21))),))
    preview = _preview("유지")
    from src.importing.contracts import ProfileField
    obs = preview.observations[0]
    preview = replace(preview, observations=(replace(obs, profile_values={
        **obs.profile_values, ProfileField.NOTE: "2차 수정"}),))
    kept = _build(roster, preview, policy=LunchDuplicatePolicy.KEEP)
    assert kept.result.snapshot.person(KEEP) == roster.person(KEEP)
    assert kept.result.snapshot.enrollments == roster.enrollments
    assert not any(c.entity_kind == "status.event" for c in kept.record.changes)
    updated = _build(roster, preview)
    assert updated.result.snapshot.person(KEEP).profile.note == "2차 수정"
    assert updated.result.snapshot.person(KEEP).lunch_period == calendar_entry().period


def test_keep_adds_selected_vacation_and_preserves_other_seasons_and_future_events():
    winter = InclusiveDateRange(date(2026, 12, 21), date(2026, 12, 31))
    spring = InclusiveDateRange(date(2026, 2, 2), date(2026, 2, 20))
    roster = replace(_roster(), enrollments=tuple(MealEnrollment(KEEP, Meal.LUNCH, p) for p in (spring, winter)))
    from tests.test_vacation_lunch_import import _stopped
    prepared = _build(roster, _preview("유지"), policy=LunchDuplicatePolicy.KEEP,
                      events=(_stopped(OMIT, date(2026, 12, 23)),))
    periods = {e.period for e in prepared.result.snapshot.enrollments if e.person_id == KEEP}
    assert periods == {spring, winter, calendar_entry().period}
    assert Meal.LUNCH not in prepared.result.snapshot.meals_for(KEEP, on=date(2026, 9, 1))


def test_legacy_unbounded_registration_is_closed_after_selected_vacation():
    prepared = _build(_roster(), _preview("유지"), policy=LunchDuplicatePolicy.KEEP)
    assert Meal.LUNCH not in prepared.result.snapshot.meals_for(KEEP, on=date(2026, 12, 21))
    assert prepared.result.snapshot.person(OMIT) == _roster().person(OMIT)
    assert [e for e in prepared.result.snapshot.enrollments if e.person_id == OMIT] == [
        e for e in _roster().enrollments if e.person_id == OMIT]


def test_selected_record_cannot_invent_dates_for_a_missing_school():
    entry = replace(calendar_entry(), rows=(replace(calendar_entry().rows[0], school_name="다른 학교"),))
    with pytest.raises(ValueError, match="선택한 방학 기록에 학교 일정"):
        _build(_roster(), _preview("유지"), entry=entry)


def test_selected_record_bounds_are_used_even_when_calendar_has_more_days():
    entry = replace(calendar_entry(), rows=(replace(calendar_entry().rows[0],
        starts_on=date(2026, 8, 19), ends_on=date(2026, 8, 21)),))
    prepared = _build(replace(_roster(), enrollments=()), _preview("유지"), entry=entry)
    assert prepared.result.snapshot.person(KEEP).lunch_period == entry.period


def test_new_person_gets_only_selected_lunch_period():
    from src.importing.application import IdentityResolution, ReconciliationDecision
    preview = _preview("신규")
    pid = uuid4()
    obs = preview.observations[0]
    prepared = _build(_roster(), preview, policy=LunchDuplicatePolicy.KEEP,
        decisions={obs.observation_id: ReconciliationDecision(
            obs.observation_id, IdentityResolution.CREATE_NEW, pid)})
    snapshot = prepared.result.snapshot
    assert snapshot.meals_for(pid, on=date(2026, 8, 16)) == ()
    assert snapshot.meals_for(pid, on=date(2026, 8, 17)) == (Meal.LUNCH,)
    assert snapshot.meals_for(pid, on=date(2026, 8, 27)) == ()
    assert snapshot.person(pid).lunch_period == calendar_entry().period


def test_wizard_requires_saved_vacation_and_retains_selection_across_modes():
    from PySide6.QtWidgets import QApplication
    from src.desktop_v2.import_wizard import RosterImportWizard
    app = QApplication.instance() or QApplication([])
    wizard = RosterImportWizard(allow_vacation_lunch=True)
    wizard.purpose_combo.setCurrentIndex(wizard.purpose_combo.findData(ImportPurpose.VACATION_LUNCH))
    assert not wizard._finish_import_if_valid()
    assert "학교 일정" in wizard.error_label.text()
    first, newer = calendar_entry(), replace(calendar_entry(), recorded_at=datetime(2026, 8, 1, tzinfo=UTC))
    wizard.set_lunch_calendar_entries((first, newer))
    assert wizard.lunch_calendar_combo.currentData() == newer
    wizard.lunch_calendar_combo.setCurrentIndex(1)
    wizard.lunch_duplicate_combo.setCurrentIndex(1)
    wizard.purpose_combo.setCurrentIndex(0)
    assert wizard.lunch_filter_panel.isHidden()
    assert not wizard.effective_date_edit.isHidden()
    wizard.purpose_combo.setCurrentIndex(1)
    assert wizard.effective_date_edit.isHidden()
    assert wizard.lunch_calendar_combo.currentData() == first
    assert wizard.lunch_duplicate_combo.currentData() == LunchDuplicatePolicy.UPDATE
    wizard.reject()
    assert wizard._selection is None
    wizard.deleteLater()
    app.processEvents()


def test_controller_persists_selected_record_policy_and_periods_on_reopen(tmp_path):
    from PySide6.QtWidgets import QDialog
    from src.desktop_v2.import_controller import GuidedRosterImportController
    from src.desktop_v2.import_wizard import ImportWizardSelection
    from src.importing.application import IdentityResolution, ReconciliationDecision
    from src.importing.households import HouseholdMatchingRule
    from src.importing.profile_history import import_profiles_from_snapshot
    from src.importing.reconciliation import stage_reconciliation
    from src.importing.school_calendar import (
        SchoolCalendarColumnMapping, SchoolCalendarImportPreview, build_school_calendar_import_revision)
    from src.services.school_calendar_history import school_calendar_import_history
    from src.storage.database_session import DatabaseSession

    actor = RevisionActor("tester", "test")
    preview = _preview("유지", "제외")
    preview = replace(preview, observations=tuple(replace(o, meals=(Meal.BREAKFAST,)) for o in preview.observations))
    plan = stage_reconciliation(None, preview.observations, effective_on=date(2026, 7, 1))
    initial = build_import_revision(None, preview, plan,
        decisions={o.observation_id: ReconciliationDecision(o.observation_id, IdentityResolution.CREATE_NEW, pid)
                   for o, pid in zip(preview.observations, (KEEP, OMIT))},
        actor=actor, recorded_at=datetime(2026, 7, 1, tzinfo=UTC),
        revision_id=uuid4(), batch_id=uuid4())
    path = tmp_path / "operation.sqlite3"
    backup = tmp_path / "backups"
    backup.mkdir()
    summaries = []

    class Messages:
        @staticmethod
        def information(parent, title, message):
            summaries.append(message)
        @staticmethod
        def critical(parent, title, message):
            pytest.fail(title + ": " + message)
        warning = critical

    class NoReview:
        def __init__(self, *args, **kwargs):
            pytest.fail("Exact matches with unchanged values do not need review.")

    with DatabaseSession.create(path, "test-password", "test-recovery", backup_directory=backup) as session:
        session.append_revision(initial.record)
        entry = calendar_entry()
        calendar_preview = SchoolCalendarImportPreview(
            entry.filename, "a" * 64, 100, "일정", "b" * 64,
            SchoolCalendarColumnMapping("일정", 1, 2, 2026, 1, 2, 3, 4, 5),
            InclusiveDateRange(date(2026, 8, 1), date(2026, 8, 31)), entry.rows, (), ())
        prepared = build_school_calendar_import_revision(
            session.database.snapshot(initial.record.metadata.revision_id), calendar_preview,
            decisions={}, actor=actor, recorded_at=datetime(2026, 7, 2, tzinfo=UTC))
        session.append_revision(prepared.record)
        for names, policy in ((("유지",), LunchDuplicatePolicy.KEEP),
                              (("제외",), LunchDuplicatePolicy.UPDATE),
                              (("유지", "제외"), LunchDuplicatePolicy.KEEP)):
            historical = session.database.snapshot(session.database.current_roster().revision_id)
            profile = import_profiles_from_snapshot(historical)[0]
            entries = school_calendar_import_history(session.database, historical.revision_id)
            class Wizard:
                def __init__(self, **kwargs):
                    pass
                def set_lunch_calendar_entries(self, entries):
                    self.entries = entries
                def exec(self):
                    assert self.entries == entries
                    return QDialog.DialogCode.Accepted
                def selection(self):
                    return ImportWizardSelection(_preview(*names), entry.period.starts_on, profile,
                        HouseholdMatchingRule(uuid4(), 1), None, ImportPurpose.VACATION_LUNCH,
                        entries[0], policy)
            result = GuidedRosterImportController(session, wizard_type=Wizard,
                review_type=NoReview, value_review_type=NoReview,
                message_box=Messages, actor_provider=lambda: actor).run()
            assert result.completed
        session.database.check_integrity()
        assert "변경할 내용이 없어" in summaries[-1]
    with DatabaseSession.open(path, "test-password") as session:
        roster = session.database.current_roster()
        session.database.check_integrity()
        for pid in (KEEP, OMIT):
            assert roster.person(pid).lunch_period == entry.period
            for day, expected in ((16, False), (17, True), (26, True), (27, False)):
                assert decide_entitlement(roster, pid, Meal.LUNCH, date(2026, 8, day)).entitled is expected
        lunch = session.database.latest_revision().metadata.decisions["vacation_lunch"]
        assert lunch["calendar_revision_id"] == str(entries[0].revision_id)
        assert lunch["duplicate_policy"] == "update"
        assert lunch["present"] == (str(OMIT),)
