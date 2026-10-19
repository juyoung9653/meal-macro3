"""Direct corrections repair current history without rewriting reservations."""

from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest

from src.domain.household_shared import (
    build_profile_correction_revision,
    expand_household_shared_revision,
    profile_with_shared_routing,
)
from src.domain.roster import Meal, HouseholdMembership, InclusiveDateRange
from tests.test_household_shared import (
    ACTOR, DAY, NOW, AuditChange, ChangeOperation, ROUTING_EVENT_KIND,
    RoutingEvent, _routing_event_state, apply, roster_from_historical_snapshot,
    RevisionMetadata, RevisionRecord, RevisionSource, roster_audit_changes,
)
from tests.test_household_shared_address_columns import address_roster, snapshot, view
from src.storage.database_session import DatabaseSession


FIELDS = ("road_address", "detail_address", "jurisdiction")
COLUMNS = ("address", "detail_address", "jurisdiction")


def movement(pid, day, position, **fields):
    fields.setdefault("driver_ids", {})
    event = RoutingEvent(uuid4(), pid, day, position, **fields)
    return AuditChange(ROUTING_EVENT_KIND, event.event_id, ChangeOperation.INSERT,
                       None, _routing_event_state(event))


def setup_history(keys=(), exceptions=()):
    r = address_roster()
    r = replace(r, persons=tuple(replace(p, profile=replace(p.profile, jurisdiction=f"base-{i}"))
                                for i, p in enumerate(r.persons)))
    events = tuple(movement(p.person_id, DAY - timedelta(days=2), i + 1,
                            road_address=f"past-road-{i}", detail_address=f"past-unit-{i}",
                            jurisdiction=f"past-jurisdiction-{i}", driver_ids={Meal.DINNER: None})
                   for i, p in enumerate(r.persons))
    events += tuple(movement(p.person_id, DAY + timedelta(days=2), i + 10,
                             road_address=f"reserved-road-{i}", detail_address=f"reserved-unit-{i}",
                             jurisdiction=f"reserved-jurisdiction-{i}")
                    for i, p in enumerate(r.persons))
    return r, snapshot(r, keys, exceptions, events)


def correction(r, snap, pid, **fields):
    profile = profile_with_shared_routing(r, pid, DAY, snap)
    return build_profile_correction_revision(r, pid, replace(profile, **fields), snapshot=snap,
                                            actor=ACTOR, recorded_at=NOW, effective_on=DAY,
                                            revision_id=uuid4())


@pytest.mark.parametrize("field,column", tuple(zip(FIELDS, COLUMNS)))
@pytest.mark.parametrize("value", ["corrected", None])
@pytest.mark.parametrize("sharing", ["none", "shared", "source_exception", "target_exception"])
def test_correction_is_component_scoped_with_sharing_exceptions(field, column, value, sharing):
    r, original = setup_history()
    a, b, outsider = r.persons
    pairs = ()
    if sharing.endswith("exception"):
        pairs = (((a if sharing == "source_exception" else b).person_id, column),)
    events = tuple(AuditChange(ref.entity_kind, ref.entity_id, ChangeOperation.INSERT, None, state)
                   for ref, state in original.entities.items() if ref.entity_kind == ROUTING_EVENT_KIND)
    snap = snapshot(r, () if sharing == "none" else COLUMNS, pairs, events)
    prepared = correction(r, snap, a.person_id, **{field: value})
    expanded = expand_household_shared_revision(snap, prepared.record)
    final = apply(snap, expanded)
    recipients = {a.person_id, b.person_id} if sharing == "shared" else {a.person_id}
    for person in (a, b, outsider):
        for day in (DAY - timedelta(days=10), DAY - timedelta(days=1), DAY):
            before, after = view(snap, person.person_id, day), view(final, person.person_id, day)
            for component in FIELDS:
                expected = value if person.person_id in recipients and component == field else getattr(before, component)
                assert (getattr(after, component) or "") == (expected or "")
            assert after.drivers == before.drivers
        assert view(final, person.person_id, DAY + timedelta(days=2)) == view(snap, person.person_id, DAY + timedelta(days=2))
    event_changes = [c for c in expanded.changes if c.entity_kind == ROUTING_EVENT_KIND]
    assert all(c.operation is ChangeOperation.UPDATE for c in event_changes)
    for change in event_changes:
        assert change.before == snap.entity(change.entity_kind, change.entity_id)
        for preserved in ("effective_on", "position", "driver_ids", "person_id"):
            assert change.after[preserved] == change.before[preserved]


def test_simultaneous_phone_sharing_still_expands_and_unchanged_routing_stays_put():
    r, snap = setup_history(("address", "detail_address", "jurisdiction", "mobile"))
    a, b, outsider = r.persons
    prepared = correction(r, snap, a.person_id, road_address="corrected", mobile_phone="new phone")
    final = apply(snap, expand_household_shared_revision(snap, prepared.record))
    saved = roster_from_historical_snapshot(final)
    assert saved.person(b.person_id).profile.mobile_phone == "new phone"
    assert saved.person(outsider.person_id).profile.mobile_phone == "old"
    for person in (a, b):
        assert saved.person(person.person_id).profile.detail_address == person.profile.detail_address
        assert saved.person(person.person_id).profile.jurisdiction == person.profile.jurisdiction
        assert view(final, person.person_id).detail_address == view(snap, person.person_id).detail_address


def test_driver_and_jurisdiction_only_events_do_not_gain_an_address():
    r = address_roster()
    a = r.persons[0]
    events = (movement(a.person_id, DAY - timedelta(days=2), 1, jurisdiction="old"),
              movement(a.person_id, DAY + timedelta(days=1), 2, driver_ids={Meal.DINNER: None}))
    snap = snapshot(r, events=events)
    prepared = correction(r, snap, a.person_id, detail_address="corrected", jurisdiction="new")
    final = apply(snap, prepared.record)
    for change in events:
        state = final.entity(change.entity_kind, change.entity_id)
        assert state["road_address"] is None
        assert state["detail_address"] is None
    assert view(final, a.person_id, DAY + timedelta(days=1)).detail_address == "corrected"
    assert view(final, a.person_id, DAY - timedelta(days=3)).jurisdiction == "new"


def test_restoring_baseline_still_updates_overlay_and_old_snapshot_remains_immutable(tmp_path):
    r, snap = setup_history()
    a = r.persons[0]
    initial = RevisionRecord(RevisionMetadata(r.revision_id, None, NOW, None, ACTOR, "initial", RevisionSource.IMPORT),
                             (*roster_audit_changes(None, r), *(AuditChange(ref.entity_kind, ref.entity_id,
                               ChangeOperation.INSERT, None, state) for ref, state in snap.entities.items()
                               if ref.entity_kind == ROUTING_EVENT_KIND)))
    with DatabaseSession.create(tmp_path / "correction.sqlite3", "password", "recovery") as session:
        session.append_revision(initial)
        original = session.database.snapshot(r.revision_id)
        prepared = correction(r, original, a.person_id, road_address=a.profile.road_address)
        assert all(change.entity_kind == ROUTING_EVENT_KIND for change in prepared.record.changes)
        session.append_revision(prepared.record)
        final = session.database.snapshot(prepared.record.metadata.revision_id)
        assert view(final, a.person_id, DAY - timedelta(days=1)).road_address == a.profile.road_address
        assert session.database.snapshot(r.revision_id) == original


def test_correction_shares_using_current_membership_not_event_day():
    r, snap = setup_history(("address",))
    a, b, outsider = r.persons
    r = replace(r, memberships=(r.memberships[0],
        HouseholdMembership(b.person_id, r.households[1].household_id, InclusiveDateRange(None, DAY - timedelta(days=1))),
        HouseholdMembership(b.person_id, r.households[0].household_id, InclusiveDateRange(DAY, None)), r.memberships[2]))
    events = tuple(AuditChange(ref.entity_kind, ref.entity_id, ChangeOperation.INSERT, None, state)
                   for ref, state in snap.entities.items() if ref.entity_kind == ROUTING_EVENT_KIND)
    snap = snapshot(r, ("address",), events=events)
    prepared = correction(r, snap, a.person_id, road_address="corrected")
    final = apply(snap, expand_household_shared_revision(snap, prepared.record))
    assert view(final, b.person_id, DAY - timedelta(days=1)).road_address == "corrected"
    assert view(final, outsider.person_id) == view(snap, outsider.person_id)


def test_noop_rejected_without_new_revision_and_missing_snapshot_supported():
    r, snap = setup_history()
    a = r.persons[0]
    with pytest.raises(ValueError, match="변경된 사람 정보가 없습니다"):
        correction(r, snap, a.person_id)
    prepared = correction(r, None, a.person_id, road_address="corrected")
    assert prepared.snapshot.person(a.person_id).profile.road_address == "corrected"
