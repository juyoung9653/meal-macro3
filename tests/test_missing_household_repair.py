"""Repair only the historical manual-add omission, preserving later intent."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from dataclasses import replace
from datetime import UTC, date, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from PySide6.QtWidgets import QApplication, QDialog

from src.desktop_v2.application import DesktopStartupController
from src.desktop_v2.missing_household_repair import MissingHouseholdRepairController, MissingHouseholdRepairDialog
from src.desktop_v2.settings_hub import SettingsAction
from src.domain.projections import roster_audit_changes
from src.domain.revisions import ImportBatch, RevisionActor, RevisionMetadata, RevisionRecord, RevisionSource
from src.domain.roster import Household, HouseholdMembership, InclusiveDateRange, Meal, MealEnrollment, Person, PersonProfile, RosterSnapshot
from src.services.missing_household_repair import load_missing_household_context, plan_missing_household_repair
from src.services.sqlite_document_service import HouseholdServiceDatesError, _household_for_service_dates, prepare_monthly_document_data
from src.storage.database_session import AutomaticBackupResult, DatabaseSession

ACTOR = RevisionActor("tester", "test")
START = date(2026, 10, 1)
PERSON = UUID("97000000-0000-0000-0000-000000000001")
HOUSEHOLD = UUID("97000000-0000-0000-0000-000000000002")


def append_roster(session, roster, *, old=None, reason="후속 수정"):
    record = RevisionRecord(
        RevisionMetadata(roster.revision_id, old.revision_id if old else None,
                         datetime(2026, 10, 20, tzinfo=UTC), START, ACTOR, reason, RevisionSource.UI,
                         decisions={"roster_row_change": {"operation": "add", "person_id": str(PERSON)}} if old is None else {}),
        roster_audit_changes(old, roster),
    )
    session.append_revision(record)
    return record


def old_manual_add(session, starts_on=START):
    person = Person(PERSON, PersonProfile("복구 대상", school_name="가상학교"))
    roster = RosterSnapshot(uuid4(), persons=(person,),
                            enrollments=(MealEnrollment(PERSON, Meal.DINNER, InclusiveDateRange(starts_on)),))
    append_roster(session, roster, reason="명단 행 추가")
    return roster


class Messages:
    def __init__(self):
        self.errors = []
        self.warnings = []

    def critical(self, _parent, title, message):
        self.errors.append((title, message))

    def warning(self, _parent, title, message):
        self.warnings.append((title, message))


def dialog_type(accepted, hook=None):
    class Dialog:
        def __init__(self, context, parent=None):
            self.context = context

        def exec(self):
            if hook:
                hook()
            return QDialog.DialogCode.Accepted if accepted else QDialog.DialogCode.Rejected
    return Dialog


def test_preview_cancel_commit_invoice_and_repeat(tmp_path):
    app = QApplication.instance() or QApplication([])
    with DatabaseSession.create(tmp_path / "repair.db", "test-password") as session:
        original = old_manual_add(session)
        original_entities = session.database.snapshot(original.revision_id).entities
        context = load_missing_household_context(session.database)
        assert [(item.person.person_id, item.starts_on) for item in context.candidates] == [(PERSON, START)]
        with pytest.raises(HouseholdServiceDatesError):
            prepare_monthly_document_data(session.database.snapshot(original.revision_id), START, START, (Meal.DINNER,))
        dialog = MissingHouseholdRepairDialog(context)
        assert "복구 대상 1명" in dialog.preview_label.text()
        assert "2026-10-01" in dialog.preview_label.text()
        dialog.close()
        messages = Messages()
        canceled = MissingHouseholdRepairController(session, dialog_type=dialog_type(False), message_box=messages)
        assert not canceled.run().completed
        assert session.database.latest_revision().metadata.revision_id == original.revision_id
        saved = MissingHouseholdRepairController(session, dialog_type=dialog_type(True), message_box=messages,
                                                 actor_provider=lambda: ACTOR).run()
        assert saved.completed and saved.roster is not None
        assert _household_for_service_dates(saved.roster, PERSON, (START,)) == saved.roster.memberships[0].household_id
        assert saved.roster.memberships[0].period.starts_on == START
        assert session.database.current_roster() == saved.roster
        invoice = prepare_monthly_document_data(session.database.snapshot(saved.roster.revision_id),
                                                START, START, (Meal.DINNER,))
        assert len(invoice.batch.deliveries) == 1
        assert invoice.batch.deliveries[0].dinner_count == 1
        assert session.database.snapshot(original.revision_id).entities == original_entities
        assert not load_missing_household_context(session.database).candidates
        previous = session.database.latest_revision().metadata.revision_id
        assert not MissingHouseholdRepairController(session, dialog_type=dialog_type(True), message_box=messages).run().completed
        assert session.database.latest_revision().metadata.revision_id == previous
        assert messages.errors == []


def test_later_removed_membership_is_not_recreated(tmp_path):
    with DatabaseSession.create(tmp_path / "removed.db", "test-password") as session:
        old = old_manual_add(session)
        joined = replace(old, revision_id=uuid4(), households=(Household(HOUSEHOLD),),
                         memberships=(HouseholdMembership(PERSON, HOUSEHOLD, InclusiveDateRange(START)),))
        append_roster(session, joined, old=old)
        removed = replace(joined, revision_id=uuid4(), memberships=())
        append_roster(session, removed, old=joined)
        context = load_missing_household_context(session.database)
        assert not context.candidates
        assert any("이후 가구 소속" in item for item in context.skipped)
        assert plan_missing_household_repair(context).revision(actor=ACTOR,
            recorded_at=datetime(2026, 10, 20, tzinfo=UTC), revision_id=uuid4()) is None


def test_unbounded_enrollment_start_is_shown_and_covered(tmp_path):
    app = QApplication.instance() or QApplication([])
    with DatabaseSession.create(tmp_path / "unbounded.db", "test-password") as session:
        old_manual_add(session, starts_on=None)
        context = load_missing_household_context(session.database)
        assert context.candidates[0].starts_on is None
        dialog = MissingHouseholdRepairDialog(context)
        assert "적용 시작일 제한 없음" in dialog.preview_label.text()
        dialog.close()
        plan = plan_missing_household_repair(context)
        assert plan.roster.memberships[0].period.starts_on is None


def test_old_vacation_lunch_new_person_can_be_repaired(tmp_path):
    app = QApplication.instance() or QApplication([])
    with DatabaseSession.create(tmp_path / "old-lunch.db", "test-password") as session:
        baseline = RosterSnapshot(uuid4(), persons=(Person(PERSON, PersonProfile("기존 대상")),),
                                  households=(Household(HOUSEHOLD),),
                                  memberships=(HouseholdMembership(PERSON, HOUSEHOLD, InclusiveDateRange(START)),),
                                  enrollments=(MealEnrollment(PERSON, Meal.LUNCH, InclusiveDateRange(START)),))
        session.append_revision(RevisionRecord(
            RevisionMetadata(baseline.revision_id, None, datetime(2026, 10, 1, tzinfo=UTC), START,
                             ACTOR, "초기 명단", RevisionSource.UI),
            roster_audit_changes(None, baseline),
        ))
        new_id = uuid4()
        old_lunch = replace(baseline, revision_id=uuid4(),
                            persons=(*baseline.persons, Person(new_id, PersonProfile("중식 신규"))),
                            enrollments=(*baseline.enrollments, MealEnrollment(new_id, Meal.LUNCH, InclusiveDateRange(START))))
        session.append_revision(RevisionRecord(
            RevisionMetadata(old_lunch.revision_id, baseline.revision_id,
                             datetime(2026, 10, 2, tzinfo=UTC), START, ACTOR,
                             "방학 중식 명단 갱신", RevisionSource.IMPORT,
                             decisions={"import_purpose": "vacation_lunch"}),
            roster_audit_changes(baseline, old_lunch),
            ImportBatch(uuid4(), old_lunch.revision_id, "synthetic.xlsx", "a" * 64, 1,
                        "명단", "fingerprint", {}, START),
        ))
        old_entities = session.database.snapshot(old_lunch.revision_id).entities
        context = load_missing_household_context(session.database)
        assert [item.person.person_id for item in context.candidates] == [new_id]
        saved = MissingHouseholdRepairController(session, dialog_type=dialog_type(True),
                                                 actor_provider=lambda: ACTOR).run()
        assert saved.completed and saved.roster is not None
        assert _household_for_service_dates(saved.roster, new_id, (START,)) == saved.roster.memberships[-1].household_id
        assert saved.roster.memberships[0] == baseline.memberships[0]
        assert session.database.snapshot(old_lunch.revision_id).entities == old_entities


def test_stale_preview_does_not_overwrite_concurrent_change(tmp_path):
    app = QApplication.instance() or QApplication([])
    with DatabaseSession.create(tmp_path / "stale.db", "test-password") as session:
        original = old_manual_add(session)
        messages = Messages()
        def concurrent_change():
            changed = replace(original, revision_id=uuid4(), persons=(replace(
                original.persons[0], profile=replace(original.persons[0].profile, note="new edit")),))
            append_roster(session, changed, old=original)
        outcome = MissingHouseholdRepairController(
            session, dialog_type=dialog_type(True, hook=concurrent_change),
            message_box=messages, actor_provider=lambda: ACTOR,
        ).run()
        assert not outcome.completed
        current = session.database.current_roster()
        assert current.persons[0].profile.note == "new edit"
        assert current.memberships == ()
        assert messages.errors and "명단이 변경" in messages.errors[0][1]


def test_backup_failure_reports_committed_repair(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    with DatabaseSession.create(tmp_path / "backup.db", "test-password") as session:
        old_manual_add(session)
        original_append = session.append_revision
        def backup_failure(record):
            original_append(record)
            return AutomaticBackupResult(error=OSError("backup failed"))
        monkeypatch.setattr(session, "append_revision", backup_failure)
        messages = Messages()
        outcome = MissingHouseholdRepairController(session, dialog_type=dialog_type(True),
                                                   message_box=messages, actor_provider=lambda: ACTOR).run()
        assert outcome.completed
        assert len(messages.warnings) == 1
        assert session.database.current_roster().memberships


def test_settings_action_routes_and_refreshes_only_completed_result(monkeypatch):
    session, window, roster = object(), object(), object()
    calls = []
    class Hub:
        selected_action = SettingsAction.MISSING_HOUSEHOLD_REPAIR
        def __init__(self, parent=None):
            assert parent is window
        def exec(self):
            return QDialog.DialogCode.Accepted
    class Controller:
        def __init__(self, value):
            assert value is session
        def run(self, parent=None):
            assert parent is window
            return SimpleNamespace(completed=True, roster=roster)
    startup = DesktopStartupController(settings=object(), settings_hub_dialog_type=Hub,
                                       missing_household_repair_controller_type=Controller)
    monkeypatch.setattr(startup, "_replace_roster_if_changed", lambda *args: calls.append(args))
    startup._run_settings_hub(session, window)
    assert calls == [(session, window, roster)]


def legacy_late_manual_membership(session, *, ends_on=None):
    from src.domain.roster import DeliveryAssignment
    roster = RosterSnapshot(
        uuid4(), persons=(Person(PERSON, PersonProfile("시작일 복구 대상")),),
        households=(Household(HOUSEHOLD),),
        memberships=(HouseholdMembership(PERSON, HOUSEHOLD, InclusiveDateRange(date(2026, 10, 16), ends_on)),),
        enrollments=(MealEnrollment(PERSON, Meal.DINNER, InclusiveDateRange(date(2026, 10, 13))),),
        assignments=(DeliveryAssignment(PERSON, None, "검증 기사", "1"),),
    )
    append_roster(session, roster, reason="명단 행 추가")
    return roster


def test_late_manual_membership_repair_restores_both_delivery_documents(tmp_path):
    from src.services.sqlite_delivery_report_service import prepare_delivery_report_data
    from src.services.sqlite_delivery_substitute_service import prepare_delivery_substitute_data
    app = QApplication.instance() or QApplication([])
    day = date(2026, 10, 13)
    with DatabaseSession.create(tmp_path / "late-start.db", "test-password") as session:
        original = legacy_late_manual_membership(session)
        old_snapshot = session.database.snapshot(original.revision_id)
        def generate(snapshot):
            return (prepare_delivery_report_data(snapshot, day, day, (Meal.DINNER,)),
                    prepare_delivery_substitute_data(snapshot, day, Meal.DINNER))
        for callback in (lambda: prepare_delivery_report_data(old_snapshot, day, day, (Meal.DINNER,)),
                         lambda: prepare_delivery_substitute_data(old_snapshot, day, Meal.DINNER)):
            with pytest.raises(ValueError, match="유효한 가구"):
                callback()
        context = load_missing_household_context(session.database)
        assert len(context.candidates) == 1
        assert context.candidates[0].original_membership == original.memberships[0]
        assert context.candidates[0].starts_on == day
        dialog = MissingHouseholdRepairDialog(context)
        assert "2026-10-16 → 2026-10-13" in dialog.preview_label.text()
        dialog.close()
        assert not MissingHouseholdRepairController(session, dialog_type=dialog_type(False)).run().completed
        assert session.database.latest_revision().metadata.revision_id == original.revision_id
        messages = Messages()
        outcome = MissingHouseholdRepairController(session, dialog_type=dialog_type(True),
            message_box=messages, actor_provider=lambda: ACTOR).run()
        assert outcome.completed, messages.errors
        current = session.database.current_roster()
        assert current.households == original.households
        assert current.enrollments == original.enrollments
        assert current.memberships == (replace(original.memberships[0], period=InclusiveDateRange(day)),)
        report, substitute = generate(session.database.snapshot(current.revision_id))
        assert len(report.shipments) == 1
        assert len(substitute.rows) == 1
        assert not load_missing_household_context(session.database).candidates
        assert session.database.snapshot(original.revision_id).entities == old_snapshot.entities
        with pytest.raises(ValueError, match="유효한 가구"):
            generate(session.database.snapshot(original.revision_id))


@pytest.mark.parametrize("change", ["end", "transfer", "gap", "date_edit", "restore"])
def test_late_membership_repair_preserves_later_intent(tmp_path, change):
    with DatabaseSession.create(tmp_path / f"{change}.db", "test-password") as session:
        original = legacy_late_manual_membership(session,
            ends_on=date(2026, 10, 30) if change == "end" else None)
        if change != "end":
            other = uuid4()
            member = original.memberships[0]
            households = original.households
            if change == "transfer":
                households = (*households, Household(other))
                members = (replace(member, household_id=other),)
            elif change == "gap":
                members = (replace(member, period=InclusiveDateRange(date(2026, 10, 16), date(2026, 10, 20))),
                           replace(member, period=InclusiveDateRange(date(2026, 10, 25))))
            else:
                members = (replace(member, period=InclusiveDateRange(date(2026, 10, 17))),)
            changed = replace(original, revision_id=uuid4(), memberships=members, households=households)
            append_roster(session, changed, old=original)
            if change == "restore":
                restored = replace(original, revision_id=uuid4())
                append_roster(session, restored, old=changed)
        assert not load_missing_household_context(session.database).candidates


@pytest.mark.parametrize("memberships,expected", [
    ((), "기록 없음"),
    ((HouseholdMembership(PERSON, HOUSEHOLD, InclusiveDateRange(date(2026, 10, 16))),), "2026-10-16"),
    ((HouseholdMembership(PERSON, HOUSEHOLD, InclusiveDateRange(date(2026, 10, 1), date(2026, 10, 12))),), "종료일 2026-10-12"),
])
def test_delivery_household_diagnostic_shows_saved_dates(memberships, expected):
    from src.services.delivery_households import require_delivery_household
    roster = RosterSnapshot(uuid4(), persons=(Person(PERSON, PersonProfile("오류 대상")),),
                            households=(Household(HOUSEHOLD),), memberships=memberships)
    with pytest.raises(HouseholdServiceDatesError) as caught:
        require_delivery_household(roster, PERSON, (date(2026, 10, 13),))
    assert caught.value.person_id == PERSON
    text = str(caught.value)
    assert "2026-10-13" in text and expected in text
    assert "가구 소속 기록 복구" in text
    assert str(PERSON) not in text and str(HOUSEHOLD) not in text
