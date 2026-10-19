"""Road and detail sharing are independent without changing routing contracts."""
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4
import pytest
from tests.test_household_shared import (roster, historical, apply, record_for, DAY, NOW, ACTOR,
    roster_from_historical_snapshot, RoutingEvent, _routing_event_state, AuditChange,
    ChangeOperation, ROUTING_EVENT_KIND, routing_state_from_snapshot, RevisionRecord,
    RevisionMetadata, RevisionSource, roster_audit_changes)
from src.domain.household_shared import (
    HOUSEHOLD_SHARED_KIND, HOUSEHOLD_SHARED_ID, shared_columns_from_state,
    shared_exceptions_from_state, household_shared_rows, household_shared_components,
    build_household_shared_revision, build_household_shared_exceptions_revision,
    expand_household_shared_revision, build_profile_revision_with_shared_routing,
    profile_with_shared_routing)
from src.domain.revisions import EntityRef, HistoricalSnapshot
from src.storage.database_session import DatabaseSession


def address_roster():
    r=roster()
    return replace(r,persons=tuple(replace(p,profile=replace(p.profile,road_address=f"road-{i}",detail_address=f"unit-{i}")) for i,p in enumerate(r.persons)))


def snapshot(r, keys=(), pairs=(), events=()):
    snap=historical(r,extra=events)
    entities=dict(snap.entities)
    entities[EntityRef(HOUSEHOLD_SHARED_KIND,HOUSEHOLD_SHARED_ID)]={"columns":list(keys),"address_columns_version":2,
        "exceptions":[{"person_id":str(pid),"column":key} for pid,key in pairs]}
    return HistoricalSnapshot(r.revision_id,entities)


def event(pid,day,position,road,detail):
    item=RoutingEvent(uuid4(),pid,day,position,{},road,detail)
    return AuditChange(ROUTING_EVENT_KIND,item.event_id,ChangeOperation.INSERT,None,_routing_event_state(item))


def view(snap,pid,day=DAY):
    return routing_state_from_snapshot(snap).resolve(roster_from_historical_snapshot(snap),pid,day)


def configure(r,snap,key,value,enabled=True):
    rows=household_shared_rows(r,key,DAY,snapshot=snap)
    record=build_household_shared_revision(r,snap.entity(HOUSEHOLD_SHARED_KIND,HOUSEHOLD_SHARED_ID),key,enabled,
        {row.household_id:(value,) for row in rows} if enabled else {},
        actor=ACTOR,recorded_at=NOW,effective_on=DAY,revision_id=uuid4(),snapshot=snap)
    return record


def profile_edit(r,snap,pid,**fields):
    profile=profile_with_shared_routing(r,pid,DAY,snap)
    prepared=build_profile_revision_with_shared_routing(r,pid,replace(profile,**fields),snapshot=snap,
        actor=ACTOR,recorded_at=NOW,effective_on=DAY,revision_id=uuid4())
    return apply(snap,expand_household_shared_revision(snap,prepared.record))


def test_legacy_state_and_exceptions_expand_without_mutation():
    pid=uuid4(); legacy={"columns":["address","mobile"],"exceptions":[{"person_id":str(pid),"column":"address"}]}
    assert shared_columns_from_state(legacy)=={"address","detail_address","mobile"}
    assert shared_exceptions_from_state(legacy)=={(pid,"address"),(pid,"detail_address")}
    assert legacy["columns"]==["address","mobile"]
    marked={**legacy,"address_columns_version":2}
    assert shared_columns_from_state(marked)=={"address","mobile"}
    assert shared_exceptions_from_state(marked)=={(pid,"address")}


@pytest.mark.parametrize("key,field,companion",[("address","road_address","detail_address"),("detail_address","detail_address","road_address")])
@pytest.mark.parametrize("value",["new",""])
def test_activation_changes_only_selected_field_and_preserves_temporal_companion(key,field,companion,value):
    r=address_roster(); future=DAY+timedelta(days=5)
    events=tuple(event(p.person_id,DAY-timedelta(days=2),i+1,f"current-road-{i}",f"current-unit-{i}") for i,p in enumerate(r.persons))
    events+=tuple(event(p.person_id,future,i+4,f"future-road-{i}",f"future-unit-{i}") for i,p in enumerate(r.persons))
    snap=snapshot(r,events=events)
    rows=household_shared_rows(r,key,DAY,snapshot=snap)
    assert len(household_shared_components(r,key))==1
    assert len(rows[0].values)==1
    final=apply(snap,configure(r,snap,key,value))
    state=final.entity(HOUSEHOLD_SHARED_KIND,HOUSEHOLD_SHARED_ID)
    assert state["address_columns_version"]==2
    assert shared_columns_from_state(state)=={key}
    for p in r.persons:
        assert getattr(view(final,p.person_id),field)==value
        assert getattr(view(final,p.person_id),companion)==getattr(view(snap,p.person_id),companion)
        assert view(final,p.person_id,DAY-timedelta(days=1))==view(snap,p.person_id,DAY-timedelta(days=1))
        assert view(final,p.person_id,future)==view(snap,p.person_id,future)


@pytest.mark.parametrize("key,field,companion",[("address","road_address","detail_address"),("detail_address","detail_address","road_address")])
@pytest.mark.parametrize("source_exempt",[False,True])
def test_profile_source_and_target_exemptions_are_per_component(key,field,companion,source_exempt):
    r=address_roster(); a,b,_=r.persons
    exempt=a if source_exempt else b
    snap=snapshot(r,("address","detail_address"),((exempt.person_id,key),))
    final=profile_edit(r,snap,a.person_id,**{field:"individual",companion:"shared"})
    assert getattr(view(final,a.person_id),field)=="individual"
    assert getattr(view(final,b.person_id),field)==getattr(view(snap,b.person_id),field)
    assert getattr(view(final,b.person_id),companion)=="shared"


@pytest.mark.parametrize("key,field,companion",[("address","road_address","detail_address"),("detail_address","detail_address","road_address")])
def test_dated_event_shares_only_selected_field(key,field,companion):
    r=address_roster();a,b,_=r.persons;snap=snapshot(r,(key,))
    change=event(a.person_id,DAY,1,"new-road","new-unit")
    final=apply(snap,expand_household_shared_revision(snap,record_for(r,replace(r,revision_id=uuid4()),extra=(change,))))
    assert getattr(view(final,b.person_id),field)==("new-road" if key=="address" else "new-unit")
    assert getattr(view(final,b.person_id),companion)==getattr(view(snap,b.person_id),companion)


def test_simultaneous_baseline_components_combine_without_overwrite():
    r=address_roster();a,b,_=r.persons;snap=snapshot(r,("address","detail_address"))
    changed=replace(r,revision_id=uuid4(),persons=tuple(replace(p,profile=replace(p.profile,road_address="road",detail_address="unit")) if p.person_id==a.person_id else p for p in r.persons))
    final=apply(snap,expand_household_shared_revision(snap,record_for(r,changed)))
    assert (view(final,b.person_id).road_address,view(final,b.person_id).detail_address)==("road","unit")
    assert view(final,b.person_id,DAY-timedelta(days=1))==view(snap,b.person_id,DAY-timedelta(days=1))


def test_same_revision_recipient_companion_edit_is_preserved():
    r=address_roster();a,b,_=r.persons;snap=snapshot(r,("address",))
    changes=(event(a.person_id,DAY,1,"shared-road","source-unit"),event(b.person_id,DAY,2,"shared-road","own-edited-unit"))
    final=apply(snap,expand_household_shared_revision(snap,record_for(r,replace(r,revision_id=uuid4()),extra=changes)))
    assert view(final,b.person_id).detail_address=="own-edited-unit"
    assert view(final,a.person_id).detail_address=="source-unit"


def test_legacy_toggle_persists_exact_keys_exceptions_and_reopens(tmp_path):
    r=address_roster();pid=r.persons[0].person_id
    initial=RevisionRecord(RevisionMetadata(r.revision_id,None,NOW,None,ACTOR,"initial",RevisionSource.IMPORT),roster_audit_changes(None,r))
    legacy={"columns":["address","mobile"],"exceptions":[{"person_id":str(pid),"column":"address"}]}
    path=tmp_path/"separate.sqlite3"
    with DatabaseSession.create(path,"password","recovery") as session:
        session.append_revision(initial)
        revision=uuid4()
        setup=RevisionRecord(RevisionMetadata(revision,r.revision_id,NOW,DAY,ACTOR,"legacy",RevisionSource.UI),
            (AuditChange(HOUSEHOLD_SHARED_KIND,HOUSEHOLD_SHARED_ID,ChangeOperation.INSERT,None,legacy),))
        session.append_revision(setup)
        current=session.database.current_roster();snap=session.database.snapshot(current.revision_id)
        session.append_revision(configure(current,snap,"address","",False))
    with DatabaseSession.open(path,"password") as session:
        current=session.database.current_roster();state=session.database.snapshot(current.revision_id).entity(HOUSEHOLD_SHARED_KIND,HOUSEHOLD_SHARED_ID)
        assert shared_columns_from_state(state)=={"detail_address","mobile"}
        assert shared_exceptions_from_state(state)=={(pid,"address"),(pid,"detail_address")}
        assert state["address_columns_version"]==2
        assert current.person(pid).profile==r.person(pid).profile


def test_exception_noop_does_not_migrate_legacy_state():
    r=address_roster();pid=r.persons[0].person_id
    state={"columns":["address"],"exceptions":[{"person_id":str(pid),"column":"address"}]}
    assert build_household_shared_exceptions_revision(r,state,shared_exceptions_from_state(state),actor=ACTOR,recorded_at=NOW,effective_on=DAY,revision_id=uuid4()) is None
    assert "address_columns_version" not in state


def test_road_review_ignores_differing_details():
    r=address_roster();r=replace(r,persons=tuple(replace(p,profile=replace(p.profile,road_address="same")) for p in r.persons))
    rows=household_shared_rows(r,"address",DAY,snapshot=snapshot(r))
    assert all(row.values==("same",) for row in rows)
    assert household_shared_rows(r,"detail_address",DAY,snapshot=snapshot(r))[0].values==(None,)


@pytest.mark.parametrize("key",["address","detail_address"])
@pytest.mark.parametrize("source_exempt",[False,True])
def test_dated_event_component_exemptions_and_future_reservation(key,source_exempt):
    r=address_roster();a,b,_=r.persons;pid=a.person_id if source_exempt else b.person_id
    future=DAY+timedelta(days=7)
    reservation=event(b.person_id,future,1,"reserved-road","reserved-unit")
    snap=snapshot(r,("address","detail_address"),((pid,key),),(reservation,))
    change=event(a.person_id,DAY,2,"new-road","new-unit")
    final=apply(snap,expand_household_shared_revision(snap,record_for(r,replace(r,revision_id=uuid4()),extra=(change,))))
    result=view(final,b.person_id)
    assert result.road_address==("road-1" if key=="address" else "new-road")
    assert result.detail_address==("unit-1" if key=="detail_address" else "new-unit")
    assert view(final,b.person_id,future)==view(snap,b.person_id,future)
    assert view(final,b.person_id,DAY-timedelta(days=1))==view(snap,b.person_id,DAY-timedelta(days=1))


@pytest.mark.parametrize("key,field",[("address","road_address"),("detail_address","detail_address")])
def test_membership_validation_compares_only_selected_address_column(key,field):
    r=address_roster();r=replace(r,persons=tuple(replace(p,profile=replace(p.profile,**{field:"same"})) for p in r.persons))
    third=r.persons[-1];snap=snapshot(r,(key,))
    changed=replace(r,revision_id=uuid4(),memberships=tuple(replace(m,household_id=r.households[0].household_id) if m.person_id==third.person_id else m for m in r.memberships))
    final=apply(snap,expand_household_shared_revision(snap,record_for(r,changed)))
    assert roster_from_historical_snapshot(final).person(third.person_id).profile==third.profile


def test_existing_legacy_enabled_nochange_does_not_create_marker_revision():
    r=address_roster();snap=historical(r,("address",))
    rows=household_shared_rows(r,"address",DAY,snapshot=snap)
    # Make every household already equal while keeping distinct detail addresses.
    r=replace(r,persons=tuple(replace(p,profile=replace(p.profile,road_address="same")) for p in r.persons))
    snap=historical(r,("address",))
    with pytest.raises(ValueError,match="변경된"):
        configure(r,snap,"address","same")
