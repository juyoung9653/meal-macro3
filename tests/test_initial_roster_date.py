"""Initial import coverage corrections use append-only, real encrypted ledgers."""
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import openpyxl
import pytest

from src.domain.delivery_routing import ROUTING_EVENT_KIND
from src.domain.entitlements import decide_entitlement
from src.domain.projections import roster_audit_changes, status_event_audit_change
from src.domain.revisions import AuditChange, ChangeOperation, RevisionActor, RevisionMetadata, RevisionRecord, RevisionSource
from src.domain.roster import DeliveryAssignment, Household, HouseholdMembership, InclusiveDateRange, Meal, MealEnrollment, Person, PersonProfile
from src.domain.status import MealStatus, StatusEvent
from src.importing.application import IdentityResolution, ReconciliationDecision
from src.importing.contracts import ImportMappingProfile, ProfileField
from src.importing.households import HouseholdMatchingRule, stage_household_grouping
from src.importing.reconciliation import stage_reconciliation
from src.importing.revision import build_import_revision
from src.importing.workbook import parse_workbook
from src.services.initial_roster_date import load_initial_roster_date_context, plan_initial_roster_date
from src.services.sqlite_delivery_report_service import prepare_delivery_report_data
from src.services.revision_restore import restore_revision
from src.storage.database_session import DatabaseSession

ACTOR = RevisionActor('tester', 'S-1-test')
NOW = datetime(2026, 10, 21, tzinfo=UTC)
START = date(2026, 10, 19)
FIRST = date(2026, 10, 1)


def imported_session(tmp_path, *, settings_first=False, second_meals="석식 / 조식"):
    path = tmp_path / 'source.xlsx'
    workbook = openpyxl.Workbook()
    workbook.active.append(('성명', '식종', '핸드폰', '기사'))
    workbook.active.append(('가상아동1', '석식 / 조식', '010-1234-5678', '기사A'))
    workbook.active.append(('가상아동2', second_meals, '010-1234-5678', '기사A'))
    workbook.save(path)
    workbook.close()
    profile = ImportMappingProfile(uuid4(), 1, 'synthetic', 'roster-xlsx',
                                   {ProfileField.NAME: 'A', ProfileField.MOBILE_PHONE: 'C'},
                                   combined_meals_column='B', driver_column='D')
    preview = parse_workbook(path, profile)
    plan = stage_reconciliation(None, preview.observations, effective_on=START)
    household_plan = stage_household_grouping(preview.observations, HouseholdMatchingRule(uuid4(), 1), effective_on=START)
    prepared = build_import_revision(None, preview, plan, decisions={
        observation.observation_id: ReconciliationDecision(observation.observation_id, IdentityResolution.CREATE_NEW, uuid4())
        for observation in preview.observations
    }, actor=ACTOR, recorded_at=NOW, revision_id=uuid4(), batch_id=uuid4(),
        household_plan=household_plan, household_decisions=household_plan.default_decisions())
    (tmp_path / 'backups').mkdir()
    session = DatabaseSession.create(tmp_path / 'data.sqlite3', 'test-password', backup_directory=tmp_path / 'backups')
    if settings_first:
        prefix = RevisionRecord(RevisionMetadata(uuid4(), None, NOW, None, ACTOR, 'initial settings', RevisionSource.UI),
                                (AuditChange('settings.test', uuid4(), ChangeOperation.INSERT, None, {'value': 'before roster'}),))
        session.append_revision(prefix)
        prepared = replace(prepared, record=replace(prepared.record, metadata=replace(prepared.record.metadata, parent_revision_id=prefix.metadata.revision_id)))
    session.append_revision(prepared.record)
    return session, prepared.record


def correction(session, day=FIRST):
    context = load_initial_roster_date_context(session.database)
    plan = plan_initial_roster_date(context, day)
    return plan, plan.revision(actor=ACTOR, recorded_at=NOW, revision_id=uuid4())


def append_roster(session, roster, *extras):
    old = session.database.current_roster()
    revision_id = uuid4()
    extras = tuple(replace(change, after={**change.after, 'revision_id': str(revision_id)})
                   if change.entity_kind == 'status.event' else change for change in extras)
    record = RevisionRecord(RevisionMetadata(revision_id, old.revision_id, NOW, START, ACTOR, 'later changes', RevisionSource.UI),
                            (*roster_audit_changes(old, roster), *extras))
    session.append_revision(record)
    return record


def test_october_correction_changes_counts_reports_but_keeps_original_and_restore(tmp_path):
    session, initial = imported_session(tmp_path)
    path = session.path
    try:
        original = session.database.snapshot(initial.metadata.revision_id)
        old = session.database.current_roster()
        assert decide_entitlement(old, old.persons[0].person_id, Meal.DINNER, FIRST).entitlement is None
        assert prepare_delivery_report_data(original, FIRST, FIRST, (Meal.DINNER,)).shipments == ()
        plan, record = correction(session)
        assert plan.affected_people == 2
        from src.domain.projections import MEMBERSHIP_KIND, ENROLLMENT_KIND
        assert {change.entity_kind for change in record.changes} == {MEMBERSHIP_KIND, ENROLLMENT_KIND}
        assert session.append_revision(record).error is None
        current = session.database.current_roster()
        assert decide_entitlement(current, current.persons[0].person_id, Meal.DINNER, FIRST).entitlement is not None
        report = prepare_delivery_report_data(session.database.snapshot(current.revision_id), FIRST, date(2026, 10, 31), (Meal.DINNER,))
        assert report.reports[0].totals.person_total == 2
        expected_days = sum((FIRST + timedelta(days=i)).weekday() in (0, 1, 2, 3) for i in range(31))
        assert sum(s.entitlement_count for s in report.shipments) == 2 * expected_days
        assert session.database.snapshot(initial.metadata.revision_id) == original
        from src.desktop_v2.ledger_reader import load_revision_chain
        from src.services.person_history import build_history_tables
        tables = build_history_tables(current, session.database.snapshot(current.revision_id),
                                      load_revision_chain(session.database, current.revision_id))
        headers, rows = tables['식종 변경']
        assert len(rows) == 4
        assert all('2026-10-19' in row[headers.index('이전 값')] and '2026-10-01' in row[headers.index('변경 값')] for row in rows)
        assert tables['정보 변경'][1] == ()
        assert correction(session)[1] is None
        assert correction(session, date(2026, 10, 10))[1] is None
        corrected_id = current.revision_id
    finally:
        session.close()
    with DatabaseSession.open(path, 'test-password', backup_directory=tmp_path / 'backups') as reopened:
        assert reopened.database.current_roster().meals_for(old.persons[0].person_id, on=FIRST)
        result = restore_revision(reopened, initial.metadata.revision_id, corrected_id, ACTOR)
        assert result.revision_id is not None
        assert reopened.database.current_roster().meals_for(old.persons[0].person_id, on=FIRST) == ()
        assert reopened.database.snapshot(corrected_id).entities != original.entities
        # Restoring the original state allows a new append-only correction.
        assert correction(reopened)[1] is not None


def test_later_people_gaps_moves_pauses_and_routing_are_preserved(tmp_path):
    session, _ = imported_session(tmp_path)
    try:
        roster = session.database.current_roster()
        pid = roster.persons[0].person_id
        later = Person(uuid4(), PersonProfile('나중추가'))
        new_household = Household(uuid4())
        memberships = tuple(m for m in roster.memberships if m.person_id != pid) + (
            HouseholdMembership(pid, roster.households[0].household_id, InclusiveDateRange(START, date(2026, 10, 23))),
            HouseholdMembership(pid, new_household.household_id, InclusiveDateRange(date(2026, 10, 24))),
            HouseholdMembership(later.person_id, roster.households[0].household_id, InclusiveDateRange(date(2026, 10, 20))),
        )
        enrollments = tuple(e for e in roster.enrollments if not (e.person_id == pid and e.meal is Meal.DINNER)) + (
            MealEnrollment(pid, Meal.DINNER, InclusiveDateRange(START, date(2026, 10, 22))),
            MealEnrollment(pid, Meal.DINNER, InclusiveDateRange(date(2026, 10, 26))),
            MealEnrollment(later.person_id, Meal.DINNER, InclusiveDateRange(date(2026, 10, 20))),
        )
        changed = replace(roster, persons=roster.persons + (later,), households=roster.households + (new_household,),
                          memberships=memberships, enrollments=enrollments,
                          assignments=roster.assignments + (DeliveryAssignment(later.person_id, driver='기사A'),))
        pause = StatusEvent(uuid4(), roster.revision_id, pid, Meal.DINNER, MealStatus.PAUSED,
                            InclusiveDateRange(date(2026, 10, 5), date(2026, 10, 6)), NOW)
        routing = AuditChange(ROUTING_EVENT_KIND, uuid4(), ChangeOperation.INSERT, None, {
            'person_id': str(pid), 'effective_on': '2026-10-24', 'position': 1, 'driver_ids': {},
            'road_address': '새주소', 'detail_address': None, 'jurisdiction': '새동',
        })
        append_roster(session, changed, status_event_audit_change(pause), routing)
        before = session.database.snapshot(session.database.current_roster().revision_id)
        plan, record = correction(session)
        session.append_revision(record)
        current = session.database.current_roster()
        assert current.meals_for(later.person_id, on=FIRST) == ()
        assert Meal.DINNER not in current.meals_for(pid, on=date(2026, 10, 23))
        assert Meal.DINNER in current.meals_for(pid, on=date(2026, 10, 26))
        assert current.household_for(pid, on=date(2026, 10, 24)).household_id == new_household.household_id
        after = session.database.snapshot(current.revision_id)
        for ref, state in before.entities.items():
            if ref.entity_kind in ('status.event', ROUTING_EVENT_KIND):
                assert after.entities[ref] == state
        report = prepare_delivery_report_data(after, date(2026, 10, 5), date(2026, 10, 5), (Meal.DINNER,))
        assert {item.person_id for item in report.shipments} == {roster.persons[1].person_id}
    finally:
        session.close()


def test_deleted_people_and_explicitly_changed_start_are_not_backdated(tmp_path):
    session, _ = imported_session(tmp_path)
    try:
        roster = session.database.current_roster()
        deleted, kept = roster.persons
        changed = replace(roster, persons=(kept,),
            memberships=tuple(m for m in roster.memberships if m.person_id == kept.person_id),
            enrollments=tuple(replace(e, period=InclusiveDateRange(date(2026, 10, 21))) for e in roster.enrollments if e.person_id == kept.person_id),
            assignments=tuple(a for a in roster.assignments if a.person_id == kept.person_id))
        append_roster(session, changed)
        plan, record = correction(session)
        assert record is None
        assert plan.affected_people == 0
        assert any('삭제' in reason for reason in plan.skipped)
        assert any('처음 가입 기간' in reason for reason in plan.skipped)
    finally:
        session.close()


def test_missing_or_mismatched_import_provenance_is_rejected(tmp_path):
    session, initial = imported_session(tmp_path)
    try:
        class Database:
            def latest_revision(self):
                return replace(initial, import_batch=None)
        with pytest.raises(ValueError, match='기록'):
            load_initial_roster_date_context(Database())
        with pytest.raises(ValueError, match='이른 날짜'):
            correction(session, date(2026, 10, 20))
    finally:
        session.close()


def test_later_manual_reset_to_old_start_is_not_mistaken_for_initial_coverage(tmp_path):
    session, _ = imported_session(tmp_path)
    try:
        session.append_revision(correction(session)[1])
        roster = session.database.current_roster()
        pid = roster.persons[0].person_id
        changed = replace(roster, enrollments=tuple(
            replace(item, period=InclusiveDateRange(START, item.period.ends_on))
            if item.person_id == pid else item for item in roster.enrollments
        ))
        append_roster(session, changed)
        plan, record = correction(session, date(2026, 9, 1))
        assert plan.affected_people == 1
        assert all(item.period.starts_on == START for item in plan.roster.enrollments if item.person_id == pid)
        assert any('나중에 수정' in reason for reason in plan.skipped)
    finally:
        session.close()


def test_settings_only_ancestor_does_not_hide_first_roster_import(tmp_path):
    session, initial = imported_session(tmp_path, settings_first=True)
    try:
        assert initial.metadata.parent_revision_id is not None
        plan, record = correction(session)
        assert plan.affected_people == 2
        assert record is not None
        session.append_revision(record)
    finally:
        session.close()


def test_shared_meal_rules_cannot_add_an_unimported_meal_during_correction(tmp_path):
    from src.domain.household_shared import HOUSEHOLD_SHARED_KIND, HOUSEHOLD_SHARED_ID
    session, _ = imported_session(tmp_path, second_meals='석식')
    try:
        roster = session.database.current_roster()
        second = next(p for p in roster.persons if p.profile.name == '가상아동2')
        changed = replace(roster, enrollments=roster.enrollments + (
            MealEnrollment(second.person_id, Meal.BREAKFAST, InclusiveDateRange(date(2026, 10, 20))),
        ))
        setting = AuditChange(HOUSEHOLD_SHARED_KIND, HOUSEHOLD_SHARED_ID, ChangeOperation.INSERT, None,
                              {'columns': ['status:breakfast'], 'address_columns_version': 2})
        append_roster(session, changed, setting)
        latest = session.database.latest_revision().metadata.revision_id
        with pytest.raises(ValueError, match='공용'):
            correction(session)
        assert session.database.latest_revision().metadata.revision_id == latest
    finally:
        session.close()
