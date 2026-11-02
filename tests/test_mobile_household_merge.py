"""Mobile merges retain dates and require atomic shared-column decisions."""
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from src.domain.household_shared import (
    HOUSEHOLD_SHARED_ID, HOUSEHOLD_SHARED_KIND, build_household_shared_revision, household_shared_rows,
)
from src.domain.mobile_household_merge import (
    MERGE_DECISION, MobileHouseholdConflict, prepare_mobile_household_revision, with_mobile_shared_values,
)
from src.domain.profile_edits import build_person_profile_revision
from src.domain.projections import roster_audit_changes, roster_from_historical_snapshot
from src.domain.revisions import AuditChange, ChangeOperation, RevisionActor, RevisionMetadata, RevisionRecord, RevisionSource
from src.domain.roster import Household, HouseholdMembership, InclusiveDateRange, Person, PersonProfile, RosterSnapshot
from src.domain.delivery_routing import routing_state_from_snapshot
from src.storage.database_session import DatabaseSession

DAY = date(2026, 10, 29)
NOW = datetime(2026, 10, 29, 3, tzinfo=UTC)
ACTOR = RevisionActor('tester', 'test')
TARGET = UUID(int=1)
SOURCE = UUID(int=2)


def setup(session, *, keys=('mobile',), same=False, conflict=False, exception=False, future=False):
    people = (
        Person(UUID(int=10), PersonProfile('조하늘', mobile_phone='010-1234-5678', telephone='031-111-1111', road_address='주소 A', note='첫째')),
        Person(UUID(int=11), PersonProfile('조사랑', mobile_phone='01012345678' if same else '010-8765-4321', telephone='031-111-1111', road_address='주소 B' if conflict else '주소 A', note='둘째')),
    )
    memberships = (HouseholdMembership(people[0].person_id, TARGET),
        HouseholdMembership(people[1].person_id, SOURCE, InclusiveDateRange(ends_on=DAY+timedelta(days=2) if future else None)))
    if future:
        memberships += (HouseholdMembership(people[1].person_id, SOURCE, InclusiveDateRange(starts_on=DAY+timedelta(days=3))),)
    r = RosterSnapshot(uuid4(), persons=people, households=(Household(TARGET), Household(SOURCE)), memberships=memberships)
    state = {'columns': list(keys), 'address_columns_version': 2,
             'exceptions': [{'person_id': str(people[1].person_id), 'column':'mobile'}] if exception else []}
    record = RevisionRecord(RevisionMetadata(r.revision_id, None, NOW, DAY, ACTOR, 'initial', RevisionSource.UI),
        (*roster_audit_changes(None, r), AuditChange(HOUSEHOLD_SHARED_KIND, HOUSEHOLD_SHARED_ID, ChangeOperation.INSERT, None, state)))
    session.append_revision(record)
    return session.database.current_roster()


def edit(r, number='010-1234-5678'):
    return build_person_profile_revision(r, UUID(int=11), replace(r.person(UUID(int=11)).profile, mobile_phone=number),
        actor=ACTOR, recorded_at=NOW, effective_on=DAY, revision_id=uuid4()).record


def household(r, pid, day=DAY):
    return r.household_for(UUID(int=pid), on=day).household_id


@pytest.mark.parametrize('keys', [(), ('mobile',)])
def test_edit_merges_even_without_shared_mobile_and_keeps_history(tmp_path, keys):
    with DatabaseSession.create(tmp_path/'m.sqlite3', 'password') as session:
        before = setup(session, keys=keys, future=True)
        session.append_revision(edit(before))
        after = session.database.current_roster()
        assert household(after, 10) == household(after, 11) == TARGET
        assert household(after, 11, DAY-timedelta(days=1)) == TARGET
        assert household(after, 11, DAY+timedelta(days=3)) == TARGET
        assert household(session.database.roster_snapshot(before.revision_id), 11) == SOURCE
        assert MERGE_DECISION in session.database.latest_revision().metadata.decisions
        assert session.last_committed_roster == after
        assert len(session.database.revision_summaries()) == 2


@pytest.mark.parametrize('number,exception', [(None, False), ('---', False), ('010-1234-5678', True)])
def test_blank_number_stays_separate_but_mobile_exception_still_merges(tmp_path, number, exception):
    with DatabaseSession.create(tmp_path/'m.sqlite3', 'password') as session:
        before = setup(session, exception=exception)
        session.append_revision(edit(before, number))
        assert household(session.database.current_roster(), 11) == (TARGET if exception else SOURCE)


def test_telephone_alone_does_not_merge(tmp_path):
    with DatabaseSession.create(tmp_path/'m.sqlite3', 'password') as session:
        r = setup(session, keys=('telephone',))
        record = build_person_profile_revision(r, UUID(int=11), replace(r.person(UUID(int=11)).profile, telephone='0311111111'),
            actor=ACTOR, recorded_at=NOW, effective_on=DAY, revision_id=uuid4()).record
        session.append_revision(record)
        assert household(session.database.current_roster(), 11) == SOURCE


def test_enable_shared_mobile_merges_existing_equal_numbers(tmp_path):
    with DatabaseSession.create(tmp_path/'m.sqlite3', 'password') as session:
        r = setup(session, keys=(), same=True)
        snap = session.database.snapshot(r.revision_id)
        rows = household_shared_rows(r, 'mobile', DAY, snapshot=snap)
        record = build_household_shared_revision(r, snap.entity(HOUSEHOLD_SHARED_KIND,HOUSEHOLD_SHARED_ID), 'mobile', True,
            {row.household_id:row.values for row in rows}, actor=ACTOR, recorded_at=NOW,
            effective_on=DAY, revision_id=uuid4(), snapshot=snap)
        session.append_revision(record)
        after = session.database.current_roster()
        assert household(after, 11) == TARGET
        assert len({p.profile.mobile_phone for p in after.persons}) == 1


def test_conflict_review_is_atomic_and_resolves_multiple_columns(tmp_path):
    with DatabaseSession.create(tmp_path/'m.sqlite3', 'password') as session:
        before = setup(session, keys=('mobile','address','note'), conflict=True)
        record = edit(before)
        with pytest.raises(MobileHouseholdConflict) as caught:
            session.append_revision(record)
        assert caught.value.column_key == 'address'
        assert household(roster_from_historical_snapshot(caught.value.candidate), 11) == TARGET
        assert session.database.current_roster().revision_id == before.revision_id
        record = with_mobile_shared_values(record, 'address', {TARGET: ('선택 주소',)})
        with pytest.raises(MobileHouseholdConflict) as caught:
            session.append_revision(record)
        assert caught.value.column_key == 'note'
        assert session.database.current_roster().revision_id == before.revision_id
        record = with_mobile_shared_values(record, 'note', {TARGET: ('',)})
        session.append_revision(record)
        after = session.database.current_roster()
        snap = session.database.snapshot(after.revision_id)
        routing = routing_state_from_snapshot(snap)
        assert {routing.resolve(after,p.person_id,DAY).road_address for p in after.persons} == {'선택 주소'}
        assert all(p.profile.note is None for p in after.persons)
        assert len(session.database.revision_summaries()) == 2
        changes = session.database.latest_revision().changes
        assert len({(c.entity_kind,c.entity_id) for c in changes}) == len(changes)
    with DatabaseSession.open(tmp_path/'m.sqlite3', 'password') as session:
        assert household(session.database.current_roster(),11) == TARGET
        session.database.check_integrity()


def test_review_dialog_cancel_leaves_everything_unsaved(tmp_path, monkeypatch):
    import os
    os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
    from PySide6.QtWidgets import QApplication, QDialog
    from src.desktop_v2.household_shared_columns import HouseholdSharedColumnDialog
    from src.desktop_v2.mobile_household_merge import review_mobile_household_merge
    app = QApplication.instance() or QApplication([])
    class Cancel(HouseholdSharedColumnDialog):
        def exec(self):
            assert self.table.rowCount() == 1
            return QDialog.DialogCode.Rejected
    with DatabaseSession.create(tmp_path/'m.sqlite3','password') as session:
        r = setup(session, keys=('mobile','address'), conflict=True)
        assert review_mobile_household_merge(session, edit(r), dialog_type=Cancel) is None
        assert session.database.current_roster().revision_id == r.revision_id


def test_cell_edit_opens_review_and_commits_once(tmp_path, monkeypatch):
    import os
    os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
    from PySide6.QtWidgets import QApplication, QDialog
    from src.desktop_v2.household_shared_columns import HouseholdSharedColumnDialog
    from src.desktop_v2.roster_cell_edit import RosterCellEditController
    import src.desktop_v2.household_shared_columns as shared_ui
    app = QApplication.instance() or QApplication([])
    class Accept(HouseholdSharedColumnDialog):
        def exec(self):
            self.validation_callback(True, {TARGET:('선택 주소',)})
            return QDialog.DialogCode.Accepted
        def selected_values(self):
            return {TARGET:('선택 주소',)}
    monkeypatch.setattr(shared_ui,'HouseholdSharedColumnDialog',Accept)
    with DatabaseSession.create(tmp_path/'m.sqlite3','password') as session:
        setup(session,keys=('mobile','address'),conflict=True)
        backups=[]
        original=session.try_backup_now
        session.try_backup_now=lambda: (backups.append(True),original())[1]
        result=RosterCellEditController(session,actor_provider=lambda:ACTOR,
            recorded_at_provider=lambda:NOW,effective_date_provider=lambda:DAY).save(UUID(int=11),'mobile','010-1234-5678')
        assert result.completed
        assert household(result.roster,11) == TARGET
        assert backups == [True]


def test_merge_restore_recovers_original_separate_households(tmp_path):
    from src.services.revision_restore import restore_revision
    with DatabaseSession.create(tmp_path/'m.sqlite3','password') as session:
        before=setup(session)
        session.append_revision(edit(before))
        merged=session.database.latest_revision_id()
        restore_revision(session,before.revision_id,merged,ACTOR)
        assert household(session.database.current_roster(),11) == SOURCE
        assert household(session.database.roster_snapshot(merged),11) == TARGET
        session.database.check_integrity()


def test_enabling_shared_mobile_reviews_other_columns_and_cancel_is_atomic(tmp_path, monkeypatch):
    import os
    os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
    from PySide6.QtWidgets import QApplication, QDialog
    from src.desktop_v2.household_shared_columns import HouseholdSharedColumnDialog, HouseholdSharedColumnController
    import src.desktop_v2.household_shared_columns as shared_ui
    app=QApplication.instance() or QApplication([])
    class Cancel(HouseholdSharedColumnDialog):
        def exec(self):
            assert '도로명주소' in self.windowTitle()
            return QDialog.DialogCode.Rejected
    monkeypatch.setattr(shared_ui,'HouseholdSharedColumnDialog',Cancel)
    with DatabaseSession.create(tmp_path/'m.sqlite3','password') as session:
        before=setup(session,keys=('address',),same=True,conflict=True)
        outcome=HouseholdSharedColumnController(session,actor_provider=lambda:ACTOR,
            recorded_at_provider=lambda:NOW).run('mobile','핸드폰',DAY)
        assert outcome.roster is None
        assert session.database.latest_revision_id()==before.revision_id
        assert session.database.snapshot(before.revision_id).entity(HOUSEHOLD_SHARED_KIND,HOUSEHOLD_SHARED_ID)['columns']==('address',)
