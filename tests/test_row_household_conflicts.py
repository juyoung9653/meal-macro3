"""Manual additions resolve shared conflicts without losing or partially saving input."""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from dataclasses import replace
from datetime import UTC, date, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from PySide6.QtWidgets import QApplication, QDialog

from src.desktop_v2.roster_row_edit import RosterRowCreateDialog, RosterRowEditController
from src.desktop_v2.row_household_conflicts import RowHouseholdConflictDialog
from src.domain.household_shared import HOUSEHOLD_SHARED_ID, HOUSEHOLD_SHARED_KIND, shared_exceptions_from_state
from src.domain.projections import roster_audit_changes
from src.domain.revisions import AuditChange, ChangeOperation, RevisionActor, RevisionMetadata, RevisionRecord, RevisionSource
from src.domain.roster import Household, HouseholdMembership, InclusiveDateRange, Meal, Person, PersonProfile, RosterSnapshot
from src.domain.roster_row_changes import build_roster_row_revision
from src.domain.row_household_conflicts import row_household_conflicts, resolve_row_household_conflicts
from src.storage.encrypted_database import EncryptedDatabase

DAY = date(2026, 10, 21)
NOW = datetime(2026, 10, 21, tzinfo=UTC)
ACTOR = RevisionActor("test", "test")


@pytest.fixture
def setup(tmp_path):
    old = Person(uuid4(), PersonProfile("기존", mobile_phone="010-1234-5678", telephone="031-123-4567", jurisdiction="위례동", note="기존 메모"))
    household = Household(uuid4())
    roster = RosterSnapshot(uuid4(), persons=(old,), households=(household,), memberships=(HouseholdMembership(old.person_id, household.household_id),))
    state = {"columns": ["telephone", "jurisdiction", "note"], "address_columns_version": 2}
    initial = RevisionRecord(RevisionMetadata(roster.revision_id, None, NOW, DAY, ACTOR, "초기", RevisionSource.IMPORT),
        (*roster_audit_changes(None, roster), AuditChange(HOUSEHOLD_SHARED_KIND, HOUSEHOLD_SHARED_ID, ChangeOperation.INSERT, None, state)))
    path = tmp_path / "conflicts.db"
    with EncryptedDatabase.create(path, b"k" * 32) as database:
        database.append_revision(initial)
        person = Person(uuid4(), PersonProfile("신규", mobile_phone="010-1234-5678", telephone="031-123-4567", jurisdiction="정자동", note="입력 메모"))
        prepared = build_roster_row_revision(roster, actor=ACTOR, recorded_at=NOW, effective_on=DAY,
            revision_id=uuid4(), add_person=person, enroll_meals=(Meal.DINNER,), household_id=household.household_id)
        yield database, roster, person, prepared, path


def test_mixed_correction_exception_is_atomic_and_preserves_existing(setup):
    db, roster, person, prepared, path = setup
    snapshot = db.snapshot(roster.revision_id)
    assert {item.key for item in row_household_conflicts(snapshot, prepared)} == {"jurisdiction", "note"}
    resolved = resolve_row_household_conflicts(snapshot, prepared, {"jurisdiction": ("위례동",)}, {"note"})
    db.append_revision(resolved.record)
    assert len(db.revision_summaries()) == 2
    current = db.current_roster()
    assert current.person(roster.persons[0].person_id) == roster.persons[0]
    assert current.person(person.person_id).profile.jurisdiction == "위례동"
    assert current.person(person.person_id).profile.note == "입력 메모"
    assert current.household_for(person.person_id, on=date.min) is not None
    with EncryptedDatabase.open(path, b"k" * 32) as reopened:
        state = reopened.snapshot(current.revision_id).entity(HOUSEHOLD_SHARED_KIND, HOUSEHOLD_SHARED_ID)
        assert shared_exceptions_from_state(state) == {(person.person_id, "note")}
        assert reopened.current_roster() == current


def test_exception_cannot_bypass_other_conflict_and_failure_saves_nothing(setup):
    db, roster, person, prepared, _ = setup
    snapshot = db.snapshot(roster.revision_id)
    with pytest.raises(ValueError, match="공용 값 충돌"):
        resolve_row_household_conflicts(snapshot, prepared, {}, {"note"})
    state = snapshot.entity(HOUSEHOLD_SHARED_KIND, HOUSEHOLD_SHARED_ID)
    changed = {**state, "exceptions": [{"person_id": str(person.person_id), "column": "note"}]}
    record = replace(prepared.record, changes=(*prepared.record.changes,
        AuditChange(HOUSEHOLD_SHARED_KIND, HOUSEHOLD_SHARED_ID, ChangeOperation.UPDATE, state, changed)))
    with pytest.raises(ValueError, match="공용 값 충돌"):
        db.append_revision(record)
    assert db.current_roster() == roster
    assert len(db.revision_summaries()) == 1
    assert db.snapshot(roster.revision_id).entity(HOUSEHOLD_SHARED_KIND, HOUSEHOLD_SHARED_ID) == state


def test_matching_add_does_not_generate_duplicate_jurisdiction_events(setup):
    db, roster, person, prepared, _ = setup
    snapshot = db.snapshot(roster.revision_id)
    resolved = resolve_row_household_conflicts(snapshot, prepared, {"jurisdiction": ("위례동",), "note": ("기존 메모",)}, set())
    db.append_revision(resolved.record)
    saved = db.revision(resolved.record.metadata.revision_id)
    assert saved.changes == resolved.record.changes
    assert db.current_roster().person(roster.persons[0].person_id) == roster.persons[0]


def test_dialog_failed_validation_keeps_values_and_can_register(setup):
    app = QApplication.instance() or QApplication([])
    db, roster, _, prepared, _ = setup
    snapshot = db.snapshot(roster.revision_id)
    dialog = RowHouseholdConflictDialog(snapshot, prepared, row_household_conflicts(snapshot, prepared))
    dialog._accept_valid()
    assert dialog.result() != QDialog.DialogCode.Accepted
    assert dialog.editors["note"][0].text() == "입력 메모"
    dialog.editors["jurisdiction"][0].setText("위례동")
    dialog.exceptions["note"].setChecked(True)
    dialog._accept_valid()
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.resolved.snapshot.persons[-1].profile.note == "입력 메모"
    dialog.close()


def test_back_and_failed_save_retain_same_dialogs(setup, monkeypatch):
    app = QApplication.instance() or QApplication([])
    db, roster, _, _, _ = setup
    session = SimpleNamespace(database=db)
    messages = []
    controller = RosterRowEditController(session, message_box=SimpleNamespace(critical=lambda *args: messages.append(args)),
        effective_date_provider=lambda: DAY, recorded_at_provider=lambda: NOW)
    steps = controller.add.steps(controller, Meal.DINNER, service_day=DAY)
    dialog = next(steps).dialog
    assert isinstance(dialog, RosterRowCreateDialog)
    dialog._editors["name"].setText("입력 유지")
    dialog._editors["telephone"].setText("031-123-4567")
    dialog._editors["mobile_phone"].setText("010-1234-5678")
    dialog._editors["jurisdiction"].setText("정자동")
    dialog._editors["note"].setPlainText("입력 메모")
    dialog._accept_valid()
    conflict = steps.send(QDialog.DialogCode.Accepted).dialog
    assert isinstance(conflict, RowHouseholdConflictDialog)
    returned = steps.send(QDialog.DialogCode.Rejected).dialog
    assert returned is dialog
    assert returned._editors["name"].text() == "입력 유지"
    returned._accept_valid()
    conflict = steps.send(QDialog.DialogCode.Accepted).dialog
    for check in conflict.exceptions.values():
        check.setChecked(True)
    conflict._accept_valid()
    def fail(*args, **kwargs):
        raise RuntimeError("저장 실패")
    monkeypatch.setattr("src.desktop_v2.roster_row_edit.save_revision", fail)
    assert steps.send(QDialog.DialogCode.Accepted).dialog is conflict
    assert messages
    assert all(check.isChecked() for check in conflict.exceptions.values())
    assert db.current_roster() == roster
    assert steps.send(QDialog.DialogCode.Rejected).dialog is dialog
    with pytest.raises(StopIteration):
        steps.send(QDialog.DialogCode.Rejected)
    dialog.close()
    conflict.close()


@pytest.mark.parametrize("period, starts_on", [
    (InclusiveDateRange(date(2026, 11, 1)), date(2026, 11, 2)),
    (InclusiveDateRange(date(2026, 12, 1)), DAY),
    (InclusiveDateRange(None, date(2026, 9, 30)), DAY),
])
def test_membership_boundaries_cannot_bypass_ui_or_database_guard(tmp_path, period, starts_on):
    old = Person(uuid4(), PersonProfile("기존", note="공용 메모"))
    person = Person(uuid4(), PersonProfile("신규", note="입력 메모"))
    hid = uuid4()
    roster = RosterSnapshot(uuid4(), persons=(old,), households=(Household(hid),),
        memberships=(HouseholdMembership(old.person_id, hid, period),))
    state = {"columns": ["note"], "address_columns_version": 2}
    initial = RevisionRecord(RevisionMetadata(roster.revision_id, None, NOW, DAY, ACTOR, "초기", RevisionSource.IMPORT),
        (*roster_audit_changes(None, roster), AuditChange(HOUSEHOLD_SHARED_KIND, HOUSEHOLD_SHARED_ID, ChangeOperation.INSERT, None, state)))
    with EncryptedDatabase.create(tmp_path / "boundaries.db", b"k" * 32) as db:
        db.append_revision(initial)
        prepared = build_roster_row_revision(roster, actor=ACTOR, recorded_at=NOW, effective_on=DAY,
            revision_id=uuid4(), add_person=person, enroll_meals=(Meal.DINNER,),
            delivery_starts_on=starts_on, household_id=hid)
        snapshot = db.snapshot(roster.revision_id)
        assert [item.key for item in row_household_conflicts(snapshot, prepared)] == ["note"]
        with pytest.raises(ValueError, match="공용 값 충돌"):
            db.append_revision(prepared.record)
        assert db.current_roster() == roster
        resolved = resolve_row_household_conflicts(snapshot, prepared, {}, {"note"})
        db.append_revision(resolved.record)
        current = db.current_roster()
        assert current.person(old.person_id) == old
        assert current.person(person.person_id) == person
        assert shared_exceptions_from_state(db.snapshot(current.revision_id).entity(HOUSEHOLD_SHARED_KIND, HOUSEHOLD_SHARED_ID)) == {(person.person_id, "note")}
