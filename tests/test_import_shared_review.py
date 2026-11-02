"""Import conflicts are reviewed in drafts and committed with the import once."""
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from PySide6.QtWidgets import QApplication, QDialog

from src.desktop_v2.household_shared_columns import HouseholdSharedColumnDialog
from src.desktop_v2.import_controller import GuidedRosterImportController, GuidedImportStatus
from src.domain.household_shared import build_household_shared_revision, household_shared_rows
from src.domain.import_shared import IMPORT_SHARED_VALUES
from src.domain.delivery_routing import routing_state_from_snapshot
from src.domain.revisions import RevisionActor
from src.importing.contracts import ProfileField
from tests.test_desktop_v2_application import _selection, _wizard_type, _session, _Messages, _AcceptedReview

ACTOR = RevisionActor("synthetic", "synthetic-sid")


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.mark.parametrize("cancel_second", (False, True))
def test_import_reviews_two_columns_and_saves_or_cancels_everything(app, tmp_path, cancel_second):
    _Messages.reset()
    with _session(tmp_path) as session:
        selection = _selection(tmp_path)
        initial = GuidedRosterImportController(session, wizard_type=_wizard_type(selection), review_type=_AcceptedReview,
            actor_provider=lambda: ACTOR, message_box=_Messages).run()
        assert initial.completed
        for key in ("school", "address"):
            roster = session.database.current_roster()
            snapshot = session.database.snapshot(roster.revision_id)
            from src.domain.household_shared import HOUSEHOLD_SHARED_KIND, HOUSEHOLD_SHARED_ID
            rows = household_shared_rows(roster, key, selection.effective_on, snapshot=snapshot)
            session.append_revision(build_household_shared_revision(
                roster, snapshot.entity(HOUSEHOLD_SHARED_KIND, HOUSEHOLD_SHARED_ID), key, True,
                {row.household_id: row.values for row in rows}, actor=ACTOR,
                recorded_at=datetime.now(UTC), effective_on=selection.effective_on,
                revision_id=uuid4(), snapshot=snapshot,
            ))
        before = session.database.current_roster()
        revision_count = len(session.database.revision_summaries())
        from src.storage.database_backup import inspect_backup
        backup_path = next((tmp_path / "backups").glob("*.mealreplica"))
        backup_before = inspect_backup(backup_path)
        original = selection.preview.observations[0]
        added = replace(original, observation_id=uuid4(), profile_values={
            **original.profile_values, ProfileField.NAME: "합성 둘째", ProfileField.BIRTH_DATE: None,
            ProfileField.SCHOOL_NAME: "다른 학교", ProfileField.ROAD_ADDRESS: "다른 주소",
        })
        profiles, rule = GuidedRosterImportController(session)._current_import_settings()
        selection = replace(selection, preview=replace(selection.preview, observations=(original, added)),
                            existing_profile=profiles[0], existing_household_rule=rule)
        opened = []
        class IdentityReview:
            def __init__(self, roster, plan, parent=None, *, household_plan=None, preview=None, column_labels=None):
                self.plan, self.household_plan = plan, household_plan
            def exec(self):
                return QDialog.DialogCode.Accepted
            def decisions(self):
                from src.importing.application import IdentityResolution, ReconciliationDecision
                return {added.observation_id: ReconciliationDecision(
                    added.observation_id, IdentityResolution.CREATE_NEW, uuid4())}
            def household_decisions(self):
                return self.household_plan.default_decisions()
        class Review(HouseholdSharedColumnDialog):
            def exec(self):
                opened.append(self._components[0].label)
                assert session.database.current_roster().revision_id == before.revision_id
                if cancel_second and len(opened) == 2:
                    return QDialog.DialogCode.Rejected
                for row in range(self.table.rowCount()):
                    self.table.item(row, 1).setText("선택 학교" if self._components[0].label == "학교" else "선택 주소")
                self.accept()
                assert self.result() == QDialog.DialogCode.Accepted, self.error.text()
                return self.result()
        outcome = GuidedRosterImportController(session, wizard_type=_wizard_type(selection), review_type=IdentityReview,
            shared_review_type=Review, actor_provider=lambda: ACTOR, message_box=_Messages).run()
        assert set(opened) == {"학교", "도로명주소"}, _Messages.critical_calls
        assert not _Messages.critical_calls
        assert len(session.database.revision_summaries()) == revision_count + (not cancel_second)
        assert len(tuple((tmp_path / "backups").glob("*.mealreplica"))) == 1
        backup_after = inspect_backup(backup_path)
        assert backup_after.latest_revision_id == session.database.latest_revision_id()
        assert (backup_after == backup_before) is cancel_second
        if cancel_second:
            assert outcome.status is GuidedImportStatus.CANCELLED
            assert session.database.current_roster() == before
        else:
            assert outcome.completed
            assert len(outcome.roster.persons) == 2
            assert {p.profile.school_name for p in outcome.roster.persons} == {"선택 학교"}
            snapshot = session.database.snapshot(outcome.roster.revision_id)
            routing = routing_state_from_snapshot(snapshot)
            assert {routing.resolve(outcome.roster, p.person_id, selection.effective_on).road_address
                    for p in outcome.roster.persons} == {"선택 주소"}
            latest = session.database.latest_revision()
            assert set(latest.metadata.decisions[IMPORT_SHARED_VALUES]) == {"school", "address"}
            assert latest.import_batch.decisions[IMPORT_SHARED_VALUES] == latest.metadata.decisions[IMPORT_SHARED_VALUES]
            assert len({(c.entity_kind, c.entity_id) for c in latest.changes}) == len(latest.changes)



def test_review_preserves_exempt_member_and_still_propagates_other_shared_columns():
    from tests.test_household_shared import roster, historical, record_for, apply, DAY
    from src.domain.household_shared import HOUSEHOLD_SHARED_KIND, HOUSEHOLD_SHARED_ID, expand_household_shared_revision
    from src.domain.import_shared import with_import_shared_values
    from src.domain.projections import roster_from_historical_snapshot
    from src.domain.revisions import EntityRef, HistoricalSnapshot, RevisionSource
    from src.domain.roster import Person, PersonProfile, HouseholdMembership
    before = roster()
    exempt = Person(uuid4(), PersonProfile("예외", note="개인 메모", mobile_phone="old"))
    hid = before.households[0].household_id
    before = replace(before, persons=(*before.persons, exempt),
                     memberships=(*before.memberships, HouseholdMembership(exempt.person_id, hid)))
    snapshot = historical(before, ("note", "mobile"))
    states = dict(snapshot.entities)
    shared_ref = EntityRef(HOUSEHOLD_SHARED_KIND, HOUSEHOLD_SHARED_ID)
    states[shared_ref] = {"columns": ["note", "mobile"],
                          "exceptions": [{"person_id": str(exempt.person_id), "column": "note"}]}
    snapshot = HistoricalSnapshot(snapshot.revision_id, states)
    a, b, outside, _ = before.persons
    after = replace(before, revision_id=uuid4(), persons=(
        replace(a, profile=replace(a.profile, note="A", mobile_phone="새 연락처")),
        replace(b, profile=replace(b.profile, note="B")), outside, exempt,
    ))
    record = record_for(before, after)
    record = replace(record, metadata=replace(record.metadata, source=RevisionSource.IMPORT))
    record = with_import_shared_values(record, "note", DAY, {hid: ("선택 메모",), before.households[1].household_id: ("keep",)})
    final = apply(snapshot, expand_household_shared_revision(snapshot, record))
    saved = roster_from_historical_snapshot(final)
    assert saved.person(a.person_id).profile.note == saved.person(b.person_id).profile.note == "선택 메모"
    assert saved.person(exempt.person_id).profile.note == "개인 메모"
    assert {saved.person(pid).profile.mobile_phone for pid in (a.person_id, b.person_id, exempt.person_id)} == {"새 연락처"}
    assert saved.person(outside.person_id) == outside
    assert final.entities[shared_ref] == snapshot.entities[shared_ref]
