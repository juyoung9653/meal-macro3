"""Household sharing persists real, audited values across every edit boundary."""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest

from src.domain.household_shared import (
    HOUSEHOLD_SHARED_ID,
    HOUSEHOLD_SHARED_KIND,
    build_household_shared_revision,
    expand_household_shared_revision,
    household_shared_rows,
    shared_columns_from_state,
)
from src.domain.projections import (
    roster_audit_changes,
    roster_entity_states,
    roster_from_historical_snapshot,
    status_event_audit_change,
)
from src.domain.revisions import (
    AuditChange,
    ChangeOperation,
    EntityRef,
    HistoricalSnapshot,
    RevisionActor,
    RevisionMetadata,
    RevisionRecord,
    RevisionSource,
)
from src.domain.roster import (
    CustomFieldDefinition,
    CustomFieldType,
    DeliveryAssignment,
    Household,
    HouseholdDeliverySetting,
    HouseholdMembership,
    InclusiveDateRange,
    Meal,
    MealEnrollment,
    Person,
    PersonProfile,
    RosterSnapshot,
)
from src.domain.profile_edits import build_person_profile_revision
from src.domain.sequence_edits import build_sequence_revision
from src.domain.meal_status_changes import build_meal_status_revision
from src.domain.delivery_routing import (
    ROUTING_EVENT_KIND,
    RoutingEvent,
    routing_state_from_snapshot,
    _routing_event_state,
)
from src.domain.status import MealStatus, StatusEvent
from src.storage.database_session import DatabaseSession

DAY = date(2026, 10, 15)
NOW = datetime(2026, 10, 15, 3, tzinfo=UTC)
ACTOR = RevisionActor("tester", "sid")


def roster():
    household, other = Household(uuid4()), Household(uuid4())
    people = tuple(
        Person(uuid4(), PersonProfile(name, mobile_phone="old", note="keep"))
        for name in ("첫째", "둘째", "외부")
    )
    return RosterSnapshot(
        uuid4(),
        persons=people,
        households=(household, other),
        memberships=tuple(
            HouseholdMembership(
                p.person_id, household.household_id if i < 2 else other.household_id
            )
            for i, p in enumerate(people)
        ),
        enrollments=tuple(MealEnrollment(p.person_id, Meal.DINNER) for p in people),
    )


def historical(roster, keys=(), extra=()):
    entities = dict(roster_entity_states(roster))
    if keys:
        entities[EntityRef(HOUSEHOLD_SHARED_KIND, HOUSEHOLD_SHARED_ID)] = {
            "columns": list(keys)
        }
    for change in extra:
        entities[EntityRef(change.entity_kind, change.entity_id)] = change.after
    return HistoricalSnapshot(roster.revision_id, entities)


def apply(snapshot, record):
    states = dict(snapshot.entities)
    for c in record.changes:
        ref = EntityRef(c.entity_kind, c.entity_id)
        if c.after is None:
            states.pop(ref, None)
        else:
            states[ref] = c.after
    return HistoricalSnapshot(record.metadata.revision_id, states)


def record_for(before, after, *, extra=(), effective=DAY):
    return RevisionRecord(
        RevisionMetadata(
            after.revision_id,
            before.revision_id,
            NOW,
            effective,
            ACTOR,
            "edit",
            RevisionSource.UI,
        ),
        (*roster_audit_changes(before, after), *extra),
    )


def activate(r, key, values, snapshot=None):
    snap = snapshot or historical(r)
    rows = household_shared_rows(r, key, DAY, snapshot=snap)
    return build_household_shared_revision(
        r,
        snap.entity(HOUSEHOLD_SHARED_KIND, HOUSEHOLD_SHARED_ID),
        key,
        True,
        {row.household_id: values for row in rows},
        actor=ACTOR,
        recorded_at=NOW,
        effective_on=DAY,
        revision_id=uuid4(),
        snapshot=snap,
    )


@pytest.mark.parametrize("key", ["address", "jurisdiction", "status:dinner", "delivery_settings", "sequence"])
def test_enabling_equal_households_only_saves_setting_and_preserves_dated_states(key):
    r = roster()
    extra = []
    for index, person in enumerate(r.persons):
        for on, address in ((DAY - timedelta(days=10), "현재 주소"), (DAY + timedelta(days=20), f"예약 주소 {index}")):
            event = RoutingEvent(uuid4(), person.person_id, on, len(extra) + 1, {}, address, "101호", jurisdiction="현재동")
            extra.append(AuditChange(ROUTING_EVENT_KIND, event.event_id, ChangeOperation.INSERT, None, _routing_event_state(event)))
        for status, period in ((MealStatus.DELIVERY, InclusiveDateRange(DAY - timedelta(days=5), DAY + timedelta(days=5))),
                               (MealStatus.STOPPED, InclusiveDateRange(DAY + timedelta(days=20)))):
            event = StatusEvent(uuid4(), r.revision_id, person.person_id, Meal.DINNER, status, period, NOW)
            extra.append(status_event_audit_change(event))
    snapshot = historical(r, extra=extra)
    rows = household_shared_rows(r, key, DAY, snapshot=snapshot)
    assert all(all(value is not None for value in row.values) for row in rows)
    record = build_household_shared_revision(r, None, key, True,
        {row.household_id: row.values for row in rows}, actor=ACTOR, recorded_at=NOW,
        effective_on=DAY, revision_id=uuid4(), snapshot=snapshot)
    assert len(record.changes) == 1
    assert record.changes[0].entity_kind == HOUSEHOLD_SHARED_KIND
    after = apply(snapshot, record)
    assert {ref: state for ref, state in after.entities.items() if ref.entity_kind != HOUSEHOLD_SHARED_KIND} == dict(snapshot.entities)


def test_resolving_conflict_does_not_write_equal_household_routing_history():
    r = roster()
    r = replace(r, persons=tuple(replace(p, profile=replace(p.profile, road_address=value))
        for p, value in zip(r.persons, ("첫째 값", "둘째 값", "유지할 값"))))
    snapshot = historical(r)
    rows = household_shared_rows(r, "address", DAY, snapshot=snapshot)
    values = {row.household_id: row.values for row in rows}
    values[r.households[0].household_id] = ("직접 정한 값",)
    record = build_household_shared_revision(r, None, "address", True, values,
        actor=ACTOR, recorded_at=NOW, effective_on=DAY, revision_id=uuid4(), snapshot=snapshot)
    events = [c for c in record.changes if c.entity_kind == ROUTING_EVENT_KIND]
    assert {c.after["person_id"] for c in events} == {str(p.person_id) for p in r.persons[:2]}
    assert roster_from_historical_snapshot(apply(snapshot, record)).person(r.persons[2].person_id) == r.persons[2]


@pytest.mark.parametrize(
    "key,values,attribute,expected",
    [
        ("name", ("공용 이름",), "name", "공용 이름"),
        ("birth_date", ("2010-03-02",), "birth_date", date(2010, 3, 2)),
        ("school", ("학교",), "school_name", "학교"),
        ("grade", ("3",), "grade", "3"),
        ("telephone", ("02-111",), "telephone", "02-111"),
        ("mobile", ("010-222",), "mobile_phone", "010-222"),
        ("school_type", ("초등",), "school_type", "초등"),
        ("household_head_name", ("보호자",), "household_head_name", "보호자"),
        ("relationship_to_child", ("부",), "relationship_to_child", "부"),
        ("jurisdiction", ("정자동",), "jurisdiction", "정자동"),
        ("note", ("",), "note", None),
    ],
)
def test_activation_explicit_values_supports_all_profile_columns(
    key, values, attribute, expected
):
    r = roster()
    snap = historical(r)
    result = apply(snap, activate(r, key, values))
    saved = roster_from_historical_snapshot(result)
    if key == "jurisdiction":
        routing = routing_state_from_snapshot(result)
        assert all(
            routing.resolve(saved, p.person_id, DAY).jurisdiction == expected
            for p in saved.persons
        )
    else:
        assert all(getattr(p.profile, attribute) == expected for p in saved.persons)
    assert shared_columns_from_state(
        result.entity(HOUSEHOLD_SHARED_KIND, HOUSEHOLD_SHARED_ID)
    ) == {key}


def test_activation_requires_explicit_conflict_resolution_and_can_clear():
    r = roster()
    rows = household_shared_rows(r, "name", DAY)
    assert rows[0].values == (None,)
    with pytest.raises(ValueError, match="모든 가구"):
        build_household_shared_revision(
            r,
            None,
            "name",
            True,
            {},
            actor=ACTOR,
            recorded_at=NOW,
            effective_on=DAY,
            revision_id=uuid4(),
        )
    with pytest.raises(ValueError, match="충돌"):
        activate(r, "mobile", (None,))
    snap = historical(r, ("mobile",))
    disabled = build_household_shared_revision(
        r,
        snap.entity(HOUSEHOLD_SHARED_KIND, HOUSEHOLD_SHARED_ID),
        "mobile",
        False,
        {},
        actor=ACTOR,
        recorded_at=NOW,
        effective_on=DAY,
        revision_id=uuid4(),
        snapshot=snap,
    )
    assert len(disabled.changes) == 1
    assert roster_from_historical_snapshot(
        apply(snap, disabled)
    ).persons == r.persons or {
        p.person_id: p
        for p in roster_from_historical_snapshot(apply(snap, disabled)).persons
    } == {p.person_id: p for p in r.persons}


@pytest.mark.parametrize(
    "kind,value,expected",
    [
        (CustomFieldType.TEXT, "메모", "메모"),
        (CustomFieldType.NUMBER, "12.5", 12.5),
        (CustomFieldType.DATE, "2026-02-02", date(2026, 2, 2)),
        (CustomFieldType.CHOICE, "예", "예"),
    ],
)
def test_custom_field_types_and_clear(kind, value, expected):
    r = roster()
    field = CustomFieldDefinition(
        uuid4(),
        "항목",
        kind,
        0,
        ("예", "아니오") if kind is CustomFieldType.CHOICE else (),
    )
    r = replace(r, custom_fields=(field,))
    key = f"custom:{field.definition_id}"
    result = roster_from_historical_snapshot(
        apply(historical(r), activate(r, key, (value,)))
    )
    assert all(
        p.profile.custom_values[field.definition_id] == expected for p in result.persons
    )
    snap = historical(result, (key,))
    p = result.persons[0]
    changed = replace(p.profile, custom_values={})
    prepared = build_person_profile_revision(
        result,
        p.person_id,
        changed,
        actor=ACTOR,
        recorded_at=NOW,
        effective_on=DAY,
        revision_id=uuid4(),
    )
    expanded = expand_household_shared_revision(snap, prepared.record)
    final = roster_from_historical_snapshot(apply(snap, expanded))
    hid = result.household_for(p.person_id, on=DAY).household_id
    assert all(
        not m.profile.custom_values for m in final.household_members(hid, on=DAY)
    )


def test_cell_profile_change_expands_only_edited_component_and_members():
    r = roster()
    first, second, outside = r.persons
    snap = historical(r, ("mobile",))
    prepared = build_person_profile_revision(
        r,
        first.person_id,
        replace(first.profile, mobile_phone=None, school_name="개인"),
        actor=ACTOR,
        recorded_at=NOW,
        effective_on=DAY,
        revision_id=uuid4(),
    )
    result = expand_household_shared_revision(snap, prepared.record)
    saved = roster_from_historical_snapshot(apply(snap, result))
    assert saved.person(second.person_id).profile.mobile_phone is None
    assert saved.person(second.person_id).profile.school_name is None
    assert saved.person(outside.person_id).profile.mobile_phone == "old"
    assert len([c for c in result.changes if c.entity_kind == "roster.person"]) == 2


def test_bulk_conflict_rejected_before_expansion():
    r = roster()
    snap = historical(r, ("mobile",))
    persons = tuple(
        replace(p, profile=replace(p.profile, mobile_phone=str(i)))
        for i, p in enumerate(r.persons)
    )
    after = replace(r, revision_id=uuid4(), persons=persons)
    with pytest.raises(ValueError, match="공용 값 충돌"):
        expand_household_shared_revision(snap, record_for(r, after))


def test_sequence_sync_preserves_other_meals_and_driver():
    r = roster()
    first, second, _ = r.persons
    r = replace(
        r,
        assignments=(
            DeliveryAssignment(first.person_id, None, "A", "1"),
            DeliveryAssignment(second.person_id, None, "B", "2"),
        ),
    )
    snap = historical(r, ("sequence",))
    prepared = build_sequence_revision(
        r,
        first.person_id,
        {Meal.DINNER: "9"},
        actor=ACTOR,
        recorded_at=NOW,
        revision_id=uuid4(),
    )
    saved = roster_from_historical_snapshot(
        apply(snap, expand_household_shared_revision(snap, prepared.record))
    )
    assert saved.resolve_assignment(second.person_id, Meal.DINNER).sequence == "9"
    assert saved.resolve_assignment(second.person_id, Meal.BREAKFAST).sequence == "2"
    assert saved.resolve_assignment(second.person_id, Meal.DINNER).driver == "B"


def test_delivery_components_are_typed_and_sync():
    r = roster()
    vals = ("", "", "", "월,수", "도시락", "월,화,수,목,금,금,금", "", "", "")
    result = apply(historical(r), activate(r, "delivery_settings", vals))
    saved = roster_from_historical_snapshot(result)
    assert saved.persons[0].delivery_settings[0].weekdays == (0, 2)
    first, second, _ = r.persons
    new = replace(
        saved.person(first.person_id),
        delivery_settings=(HouseholdDeliverySetting(Meal.BREAKFAST, (1,), "특식"),),
    )
    after = replace(
        saved,
        revision_id=uuid4(),
        persons=tuple(
            new if p.person_id == first.person_id else p for p in saved.persons
        ),
    )
    final = roster_from_historical_snapshot(
        apply(
            result, expand_household_shared_revision(result, record_for(saved, after))
        )
    )
    assert final.person(second.person_id).delivery_settings == new.delivery_settings


def test_status_change_does_not_clip_to_recorded_membership_end():
    r = roster()
    first, second, outside = r.persons
    r = replace(
        r,
        memberships=tuple(
            replace(m, period=InclusiveDateRange(None, date(2026, 10, 20)))
            if m.person_id == second.person_id
            else m
            for m in r.memberships
        ),
    )
    snap = historical(r, ("status:dinner",))
    prepared = build_meal_status_revision(
        r,
        first.person_id,
        (Meal.DINNER,),
        MealStatus.STOPPED,
        InclusiveDateRange(DAY),
        actor=ACTOR,
        recorded_at=NOW,
        revision_id=uuid4(),
        event_ids={Meal.DINNER: uuid4()},
    )
    expanded = expand_household_shared_revision(snap, prepared.record)
    events = [c.after for c in expanded.changes if c.entity_kind == "status.event"]
    assert len(events) == 2
    child = next(e for e in events if e["person_id"] == str(second.person_id))
    assert child["ends_on"] is None
    assert all(e["person_id"] != str(outside.person_id) for e in events)


def test_overlapping_status_conflict_is_atomic_error():
    r = roster()
    first, second, _ = r.persons
    snap = historical(r, ("status:dinner",))
    rid = uuid4()
    events = (
        StatusEvent(
            uuid4(),
            rid,
            first.person_id,
            Meal.DINNER,
            MealStatus.STOPPED,
            InclusiveDateRange(DAY),
            NOW,
        ),
        StatusEvent(
            uuid4(),
            rid,
            second.person_id,
            Meal.DINNER,
            MealStatus.DELIVERY,
            InclusiveDateRange(DAY, date(2026, 10, 19)),
            NOW,
        ),
    )
    record = record_for(
        r,
        replace(r, revision_id=rid),
        extra=tuple(status_event_audit_change(e) for e in events),
    )
    with pytest.raises(ValueError, match="상태 변경.*충돌"):
        expand_household_shared_revision(snap, record)


def test_routing_changes_sync_only_selected_columns():
    r = roster()
    first, second, outside = r.persons
    event = RoutingEvent(
        uuid4(), first.person_id, DAY, 1, {}, "새 주소", "101", "새 관할"
    )
    change = AuditChange(
        ROUTING_EVENT_KIND,
        event.event_id,
        ChangeOperation.INSERT,
        None,
        _routing_event_state(event),
    )
    snap = historical(r, ("address",))
    record = record_for(r, replace(r, revision_id=uuid4()), extra=(change,))
    result = apply(snap, expand_household_shared_revision(snap, record))
    routing = routing_state_from_snapshot(result)
    assert routing.resolve(r, second.person_id, DAY).road_address == "새 주소"
    assert routing.resolve(r, second.person_id, DAY).jurisdiction is None
    assert routing.resolve(r, outside.person_id, DAY).road_address is None


def test_profile_address_edit_overrides_old_routing_without_erasing_future():
    r = roster()
    first, second, _ = r.persons
    events = [
        RoutingEvent(uuid4(), p.person_id, day, i + 1, {}, road, "")
        for i, (p, day, road) in enumerate(
            (
                (first, date(2026, 1, 1), "과거"),
                (second, date(2026, 1, 1), "과거"),
                (second, date(2026, 11, 1), "예정"),
            )
        )
    ]
    changes = tuple(
        AuditChange(
            ROUTING_EVENT_KIND,
            e.event_id,
            ChangeOperation.INSERT,
            None,
            _routing_event_state(e),
        )
        for e in events
    )
    snap = historical(r, ("address",), changes)
    prepared = build_person_profile_revision(
        r,
        first.person_id,
        replace(first.profile, road_address="현재"),
        actor=ACTOR,
        recorded_at=NOW,
        effective_on=DAY,
        revision_id=uuid4(),
    )
    result = apply(snap, expand_household_shared_revision(snap, prepared.record))
    saved = roster_from_historical_snapshot(result)
    routing = routing_state_from_snapshot(result)
    assert routing.resolve(saved, second.person_id, DAY).road_address == "현재"
    assert (
        routing.resolve(saved, second.person_id, date(2026, 11, 2)).road_address
        == "예정"
    )


def test_new_member_conflicts_do_not_select_arbitrary_winner():
    r = roster()
    new = Person(uuid4(), PersonProfile("추가", mobile_phone="다름"))
    after = replace(
        r,
        revision_id=uuid4(),
        persons=(*r.persons, new),
        memberships=(
            *r.memberships,
            HouseholdMembership(new.person_id, r.households[0].household_id),
        ),
    )
    with pytest.raises(ValueError, match="가구 공용 값 충돌"):
        expand_household_shared_revision(
            historical(r, ("mobile",)), record_for(r, after)
        )


def test_enrollment_removal_syncs_and_keeps_future_period():
    r = roster()
    first, second, _ = r.persons
    snap = historical(r, ("status:dinner",))
    enrollments = tuple(e for e in r.enrollments if e.person_id != first.person_id) + (
        MealEnrollment(
            first.person_id,
            Meal.DINNER,
            InclusiveDateRange(None, DAY - timedelta(days=1)),
        ),
        MealEnrollment(
            first.person_id, Meal.DINNER, InclusiveDateRange(date(2026, 11, 1))
        ),
    )
    after = replace(r, revision_id=uuid4(), enrollments=enrollments)
    saved = roster_from_historical_snapshot(
        apply(snap, expand_household_shared_revision(snap, record_for(r, after)))
    )
    assert Meal.DINNER not in saved.meals_for(second.person_id, on=DAY)
    assert Meal.DINNER in saved.meals_for(second.person_id, on=date(2026, 11, 2))


def test_encrypted_regroup_roundtrip_and_default_off_fast_path(
    tmp_path, monkeypatch
):
    r = roster()
    path = tmp_path / "shared.sqlite3"
    initial = RevisionRecord(
        RevisionMetadata(
            r.revision_id, None, NOW, None, ACTOR, "initial", RevisionSource.IMPORT
        ),
        roster_audit_changes(None, r),
    )
    with DatabaseSession.create(path, "password", "recovery") as session:
        original = session.database.snapshot
        monkeypatch.setattr(
            session.database,
            "snapshot",
            lambda _: pytest.fail("default OFF must not replay history"),
        )
        session.append_revision(initial)
        monkeypatch.setattr(session.database, "snapshot", original)
        session.append_revision(activate(r, "mobile", ("shared",)))
        saved = session.database.current_roster()
        first, second, _ = r.persons
        profile = replace(saved.person(first.person_id).profile, mobile_phone="new")
        prepared = build_person_profile_revision(
            saved,
            first.person_id,
            profile,
            actor=ACTOR,
            recorded_at=NOW,
            effective_on=DAY,
            revision_id=uuid4(),
        )
        session.append_revision(prepared.record)
        assert (
            session.last_committed_roster.person(second.person_id).profile.mobile_phone
            == "shared"
        )
        parent = session.database.current_roster()
        after = replace(
            parent,
            revision_id=uuid4(),
            persons=tuple(
                replace(p, profile=replace(p.profile, mobile_phone=str(i)))
                for i, p in enumerate(parent.persons)
            ),
        )
        session.append_revision(record_for(parent, after))
        parent = session.database.current_roster()
        assert len({m.household_id for m in parent.current_memberships}) == len(parent.persons)
        last_id = parent.revision_id
    with DatabaseSession.open(path, "password") as session:
        assert session.database.current_roster().revision_id == last_id
        assert (
            session.database.current_roster()
            .person(second.person_id)
            .profile.mobile_phone
            == after.person(second.person_id).profile.mobile_phone
        )
        assert shared_columns_from_state(
            session.database.snapshot(last_id).entity(
                HOUSEHOLD_SHARED_KIND, HOUSEHOLD_SHARED_ID
            )
        ) == {"mobile"}


@pytest.mark.parametrize(
    "key,values,attribute",
    [
        ("address", ("새 주소",), "road_address"),
        ("jurisdiction", ("새 관할",), "jurisdiction"),
        ("status:dinner", ("배송", "", "새 기사"), "driver"),
    ],
)
def test_temporal_activation_does_not_rewrite_past_baseline(key, values, attribute):
    r = roster()
    r = replace(
        r,
        persons=tuple(
            replace(
                p,
                profile=replace(
                    p.profile, road_address="이전 주소", jurisdiction="이전 관할"
                ),
            )
            for p in r.persons
        ),
        assignments=tuple(
            DeliveryAssignment(p.person_id, Meal.DINNER, "이전 기사") for p in r.persons
        ),
    )
    result = apply(historical(r), activate(r, key, values))
    saved = roster_from_historical_snapshot(result)
    routing = routing_state_from_snapshot(result)
    for p in saved.persons:
        old = routing.resolve(saved, p.person_id, DAY - timedelta(days=1))
        new = routing.resolve(saved, p.person_id, DAY)
        assert (
            old.drivers[Meal.DINNER]
            if attribute == "driver"
            else getattr(old, attribute)
        ) == {
            "road_address": "이전 주소",
            "jurisdiction": "이전 관할",
            "driver": "이전 기사",
        }[attribute]
        assert (
            new.drivers[Meal.DINNER]
            if attribute == "driver"
            else getattr(new, attribute)
        ) == values[-1 if attribute == "driver" else 0]


def test_profile_temporal_edit_keeps_previous_day_for_every_member():
    r = roster()
    first, second, _ = r.persons
    r = replace(
        r,
        persons=tuple(
            replace(p, profile=replace(p.profile, road_address="이전"))
            for p in r.persons
        ),
    )
    snap = historical(r, ("address",))
    prepared = build_person_profile_revision(
        r,
        first.person_id,
        replace(r.person(first.person_id).profile, road_address="변경"),
        actor=ACTOR,
        recorded_at=NOW,
        effective_on=DAY,
        revision_id=uuid4(),
    )
    result = apply(snap, expand_household_shared_revision(snap, prepared.record))
    saved = roster_from_historical_snapshot(result)
    routing = routing_state_from_snapshot(result)
    for p in (first, second):
        assert (
            routing.resolve(saved, p.person_id, DAY - timedelta(days=1)).road_address
            == "이전"
        )
        assert routing.resolve(saved, p.person_id, DAY).road_address == "변경"


def test_activation_status_preserves_scheduled_future_event_and_rejects_missing_pause_end():
    r = roster()
    first = r.persons[0]
    event = StatusEvent(
        uuid4(),
        r.revision_id,
        first.person_id,
        Meal.DINNER,
        MealStatus.STOPPED,
        InclusiveDateRange(date(2026, 11, 1)),
        NOW,
    )
    snap = historical(r, extra=(status_event_audit_change(event),))
    unchanged = activate(r, "status:dinner", ("배송", "", "새 기사"), snap)
    assert not any(c.entity_kind == "status.event" for c in unchanged.changes)
    record = activate(r, "status:dinner", ("중지", "", ""), snap)
    assert (
        next(
            c.after
            for c in record.changes
            if c.entity_kind == "status.event"
            and c.after["person_id"] == str(first.person_id)
        )["ends_on"]
        == "2026-10-31"
    )
    with pytest.raises(ValueError, match="종료일"):
        activate(r, "status:dinner", ("일시중지", "", ""), snap)


def test_clear_shared_address_has_real_dated_effect():
    r = roster()
    r = replace(
        r,
        persons=tuple(
            replace(
                p, profile=replace(p.profile, road_address="old", detail_address="101")
            )
            for p in r.persons
        ),
    )
    result = apply(historical(r), activate(r, "address", ("",)))
    saved = roster_from_historical_snapshot(result)
    routing = routing_state_from_snapshot(result)
    assert all(
        routing.resolve(saved, p.person_id, DAY).road_address == ""
        for p in saved.persons
    )
    assert all(
        routing.resolve(saved, p.person_id, DAY - timedelta(days=1)).road_address
        == "old"
        for p in saved.persons
    )


def test_conflicting_same_day_routing_rejected():
    r = roster()
    snap = historical(r, ("address",))
    first, second, _ = r.persons
    events = (
        RoutingEvent(uuid4(), first.person_id, DAY, 1, {}, "A", ""),
        RoutingEvent(uuid4(), second.person_id, DAY, 2, {}, "B", ""),
    )
    changes = tuple(
        AuditChange(
            ROUTING_EVENT_KIND,
            e.event_id,
            ChangeOperation.INSERT,
            None,
            _routing_event_state(e),
        )
        for e in events
    )
    with pytest.raises(ValueError, match="주소.*충돌"):
        expand_household_shared_revision(
            snap, record_for(r, replace(r, revision_id=uuid4()), extra=changes)
        )


def test_driver_only_activation_preserves_finite_enrollment_end():
    r = roster()
    end = date(2026, 10, 20)
    r = replace(
        r,
        enrollments=tuple(
            replace(e, period=InclusiveDateRange(None, end)) for e in r.enrollments
        ),
    )
    snap = historical(r)
    rows = household_shared_rows(r, "status:dinner", DAY, snapshot=snap)
    assert all(row.values[:2] == ("배송", "2026-10-20") for row in rows)
    record = activate(r, "status:dinner", ("배송", "2026-10-20", "새 기사"), snap)
    assert not any(
        c.entity_kind in ("roster.meal_enrollment", "status.event")
        for c in record.changes
    )
    saved = roster_from_historical_snapshot(apply(snap, record))
    assert all(
        Meal.DINNER not in saved.meals_for(p.person_id, on=date(2026, 11, 1))
        for p in saved.persons
    )


def test_preview_decodes_history_once_and_skips_routing_for_simple_fields(monkeypatch):
    import src.domain.household_shared as module

    r = roster()
    snap = historical(r)
    counts = {"routing": 0, "events": 0}
    original_routing = module.routing_state_from_snapshot
    original_events = module._events

    def routing(snapshot):
        counts["routing"] += 1
        return original_routing(snapshot)

    def events(snapshot):
        counts["events"] += 1
        return original_events(snapshot)

    monkeypatch.setattr(module, "routing_state_from_snapshot", routing)
    monkeypatch.setattr(module, "_events", events)
    household_shared_rows(r, "note", DAY, snapshot=snap)
    assert counts == {"routing": 0, "events": 0}
    household_shared_rows(r, "address", DAY, snapshot=snap)
    assert counts == {"routing": 1, "events": 0}
    household_shared_rows(r, "status:dinner", DAY, snapshot=snap)
    assert counts == {"routing": 2, "events": 1}


def test_cell_controller_returns_committed_expanded_roster_and_failed_save_is_unchanged(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace
    from src.desktop_v2.roster_cell_edit import RosterCellEditController
    from src.storage.database_session import AutomaticBackupResult

    r = roster()
    first, second, _ = r.persons
    initial = RevisionRecord(
        RevisionMetadata(
            r.revision_id, None, NOW, None, ACTOR, "initial", RevisionSource.IMPORT
        ),
        roster_audit_changes(None, r),
    )
    with DatabaseSession.create(
        tmp_path / "cell.sqlite3", "password", "recovery"
    ) as session:
        session.append_revision(initial)
        session.append_revision(activate(r, "mobile", ("shared",)))
        warnings = []
        failures = []
        controller = RosterCellEditController(
            session,
            message_box=SimpleNamespace(
                warning=lambda *a: warnings.append(a),
                critical=lambda *a: failures.append(a),
            ),
            actor_provider=lambda: ACTOR,
            recorded_at_provider=lambda: NOW,
            effective_date_provider=lambda: DAY,
        )
        monkeypatch.setattr(
            session,
            "try_backup_now",
            lambda: AutomaticBackupResult(error=OSError("backup offline")),
        )
        saved = controller.save(first.person_id, "mobile", "010-2345-6789")
        assert saved.completed and not warnings
        assert saved.roster.person(first.person_id).profile.mobile_phone == "010-2345-6789"
        assert saved.roster.person(second.person_id).profile.mobile_phone == "shared"
        assert saved.roster.current_membership(first.person_id).household_id != saved.roster.current_membership(second.person_id).household_id
        previous = session.database.current_roster()
        failed = controller.save(first.person_id, "birth_date", "invalid")
        assert not failed.completed and failures
        assert session.database.current_roster() == previous


def test_stop_sync_does_not_extend_finite_enrollment():
    r = roster()
    end = date(2026, 10, 20)
    r = replace(
        r,
        enrollments=tuple(
            replace(e, period=InclusiveDateRange(None, end)) for e in r.enrollments
        ),
    )
    first = r.persons[0]
    snap = historical(r, ("status:dinner",))
    prepared = build_meal_status_revision(
        r,
        first.person_id,
        (Meal.DINNER,),
        MealStatus.STOPPED,
        InclusiveDateRange(DAY),
        actor=ACTOR,
        recorded_at=NOW,
        revision_id=uuid4(),
        event_ids={Meal.DINNER: uuid4()},
    )
    result = apply(snap, expand_household_shared_revision(snap, prepared.record))
    saved = roster_from_historical_snapshot(result)
    assert all(
        Meal.DINNER not in saved.meals_for(p.person_id, on=date(2026, 11, 1))
        for p in saved.persons
    )


def test_setting_history_names_enabled_disabled_builtin_and_custom_columns():
    from src.services.revision_history import build_revision_change_rows

    r = roster()
    field = CustomFieldDefinition(uuid4(), "개인 항목", CustomFieldType.TEXT, 0)
    r = replace(r, custom_fields=(field,))
    custom = f"custom:{field.definition_id}"
    snap = historical(r, ("mobile", custom))
    record = build_household_shared_revision(
        r,
        snap.entity(HOUSEHOLD_SHARED_KIND, HOUSEHOLD_SHARED_ID),
        custom,
        False,
        {},
        actor=ACTOR,
        recorded_at=NOW,
        effective_on=DAY,
        revision_id=uuid4(),
        snapshot=snap,
    )
    rows = build_revision_change_rows(
        record, apply(snap, record), previous_snapshot=snap
    )
    assert rows == (("전체 명단", "", "개인 항목", "가구 공용 해제"),)
    initial = activate(r, "mobile", ("shared",))
    initial_rows = build_revision_change_rows(initial, apply(historical(r), initial))
    assert ("전체 명단", "", "핸드폰", "가구 공용 지정") in initial_rows


@pytest.mark.parametrize(
    "key,field,new_values",
    [
        ("address", "road_address", ("현재 주소",)),
        ("jurisdiction", "jurisdiction", ("현재 관할",)),
    ],
)
def test_profile_adapter_reset_to_baseline_preserves_past_and_future(
    key, field, new_values
):
    from src.domain.household_shared import (
        profile_with_shared_routing,
        build_profile_revision_with_shared_routing,
    )

    r = roster()
    first, second, _ = r.persons
    r = replace(
        r,
        persons=tuple(
            replace(
                p,
                profile=replace(
                    p.profile, road_address="기준 주소", jurisdiction="기준 관할"
                ),
            )
            for p in r.persons
        ),
    )
    future = RoutingEvent(
        uuid4(),
        second.person_id,
        date(2026, 11, 1),
        1,
        {},
        "미래 주소",
        "",
        "미래 관할",
    )
    snap = historical(
        r,
        extra=(
            AuditChange(
                ROUTING_EVENT_KIND,
                future.event_id,
                ChangeOperation.INSERT,
                None,
                _routing_event_state(future),
            ),
        ),
    )
    configured = apply(snap, activate(r, key, new_values, snap))
    saved = roster_from_historical_snapshot(configured)
    displayed = profile_with_shared_routing(saved, first.person_id, DAY, configured)
    assert getattr(displayed, field) == new_values[0]
    reset = replace(
        displayed, **{field: getattr(saved.person(first.person_id).profile, field)}
    )
    prepared = build_profile_revision_with_shared_routing(
        saved,
        first.person_id,
        reset,
        snapshot=configured,
        actor=ACTOR,
        recorded_at=NOW,
        effective_on=DAY,
        revision_id=uuid4(),
    )
    final = apply(
        configured, expand_household_shared_revision(configured, prepared.record)
    )
    final_roster = roster_from_historical_snapshot(final)
    routing = routing_state_from_snapshot(final)
    for pid in (first.person_id, second.person_id):
        assert getattr(routing.resolve(final_roster, pid, DAY), field) == getattr(
            r.person(pid).profile, field
        )
        assert getattr(
            routing.resolve(final_roster, pid, DAY - timedelta(days=1)), field
        ) == getattr(r.person(pid).profile, field)
    assert getattr(
        routing.resolve(final_roster, second.person_id, date(2026, 11, 1)), field
    ) == ("미래 주소" if key == "address" else "미래 관할")


def test_profile_adapter_unrelated_edit_keeps_routing_and_household_others_unchanged():
    from src.domain.household_shared import (
        profile_with_shared_routing,
        build_profile_revision_with_shared_routing,
    )

    r = roster()
    first, second, _ = r.persons
    configured = apply(historical(r), activate(r, "address", ("현재 주소",)))
    saved = roster_from_historical_snapshot(configured)
    displayed = profile_with_shared_routing(saved, first.person_id, DAY, configured)
    prepared = build_profile_revision_with_shared_routing(
        saved,
        first.person_id,
        replace(displayed, note="개인 메모"),
        snapshot=configured,
        actor=ACTOR,
        recorded_at=NOW,
        effective_on=DAY,
        revision_id=uuid4(),
    )
    assert not any(c.entity_kind == ROUTING_EVENT_KIND for c in prepared.record.changes)
    final = apply(
        configured, expand_household_shared_revision(configured, prepared.record)
    )
    final_roster = roster_from_historical_snapshot(final)
    assert final_roster.person(first.person_id).profile.note == "개인 메모"
    assert final_roster.person(second.person_id).profile.note == "keep"
    assert (
        routing_state_from_snapshot(final)
        .resolve(final_roster, first.person_id, DAY)
        .road_address
        == "현재 주소"
    )


def test_person_controller_seeds_shared_current_values_and_noop_writes_nothing(
    tmp_path,
):
    from types import SimpleNamespace
    from PySide6.QtWidgets import QDialog
    from src.desktop_v2.person_edit import (
        PersonProfileEditController,
        PersonProfileEditStatus,
    )

    r = roster()
    first = r.persons[0]
    initial = RevisionRecord(
        RevisionMetadata(
            r.revision_id, None, NOW, None, ACTOR, "initial", RevisionSource.IMPORT
        ),
        roster_audit_changes(None, r),
    )
    seen = []

    class Dialog:
        def __init__(self, person, *args, **kwargs):
            self.person = person
            seen.append(person.profile.road_address)

        def exec(self):
            return QDialog.DialogCode.Accepted

        def profile(self):
            return self.person.profile

    with DatabaseSession.create(
        tmp_path / "profile.sqlite3", "password", "recovery"
    ) as session:
        session.append_revision(initial)
        session.append_revision(activate(r, "address", ("현재 주소",)))
        before = session.database.current_roster()
        controller = PersonProfileEditController(
            session,
            dialog_type=Dialog,
            message_box=SimpleNamespace(critical=lambda *a: pytest.fail(str(a))),
            actor_provider=lambda: ACTOR,
            recorded_at_provider=lambda: NOW,
            effective_date_provider=lambda: DAY,
        )
        result = controller.run(first.person_id)
        assert seen == ["현재 주소"]
        assert result.status is PersonProfileEditStatus.CANCELLED
        assert session.database.current_roster() == before


def test_profile_adapter_road_edit_preserves_siblings_unedited_detail():
    from src.domain.household_shared import (
        profile_with_shared_routing,
        build_profile_revision_with_shared_routing,
    )

    r = roster()
    first, second, _ = r.persons
    events = (
        RoutingEvent(uuid4(), first.person_id, DAY, 1, {}, "공통 주소", "101"),
        RoutingEvent(uuid4(), second.person_id, DAY, 2, {}, "공통 주소", "202"),
    )
    changes = tuple(
        AuditChange(
            ROUTING_EVENT_KIND,
            e.event_id,
            ChangeOperation.INSERT,
            None,
            _routing_event_state(e),
        )
        for e in events
    )
    snap = historical(r, ("address",), changes)
    displayed = profile_with_shared_routing(r, first.person_id, DAY, snap)
    prepared = build_profile_revision_with_shared_routing(
        r,
        first.person_id,
        replace(displayed, road_address="변경 주소"),
        snapshot=snap,
        actor=ACTOR,
        recorded_at=NOW,
        effective_on=DAY,
        revision_id=uuid4(),
    )
    final = apply(snap, expand_household_shared_revision(snap, prepared.record))
    saved = roster_from_historical_snapshot(final)
    routing = routing_state_from_snapshot(final)
    assert routing.resolve(saved, second.person_id, DAY).road_address == "변경 주소"
    assert routing.resolve(saved, second.person_id, DAY).detail_address == "202"


@pytest.mark.parametrize(
    "key,field,values",
    [
        ("address", "road_address", ("가구 주소",)),
        ("jurisdiction", "jurisdiction", ("가구 관할",)),
    ],
)
def test_profile_after_disable_keeps_current_value_and_edits_only_one_person(
    tmp_path, key, field, values
):
    from types import SimpleNamespace
    from PySide6.QtWidgets import QDialog
    from src.desktop_v2.person_edit import (
        PersonProfileEditController,
        PersonProfileEditStatus,
    )

    r = roster()
    first, second, _ = r.persons
    initial = RevisionRecord(
        RevisionMetadata(
            r.revision_id, None, NOW, None, ACTOR, "initial", RevisionSource.IMPORT
        ),
        roster_audit_changes(None, r),
    )
    seen = []
    edit = [None]

    class Dialog:
        def __init__(self, person, *args, **kwargs):
            self.person = person
            seen.append(getattr(person.profile, field))

        def exec(self):
            return QDialog.DialogCode.Accepted

        def profile(self):
            return (
                self.person.profile
                if edit[0] is None
                else replace(self.person.profile, **{field: edit[0]})
            )

    with DatabaseSession.create(
        tmp_path / "disabled.sqlite3", "password", "recovery"
    ) as session:
        session.append_revision(initial)
        session.append_revision(activate(r, key, values))
        enabled = session.database.current_roster()
        snap = session.database.snapshot(enabled.revision_id)
        disabled = build_household_shared_revision(
            enabled,
            snap.entity(HOUSEHOLD_SHARED_KIND, HOUSEHOLD_SHARED_ID),
            key,
            False,
            {},
            actor=ACTOR,
            recorded_at=NOW,
            effective_on=DAY,
            revision_id=uuid4(),
            snapshot=snap,
        )
        session.append_revision(disabled)
        before = session.database.current_roster()
        controller = PersonProfileEditController(
            session,
            dialog_type=Dialog,
            message_box=SimpleNamespace(critical=lambda *a: pytest.fail(str(a))),
            actor_provider=lambda: ACTOR,
            recorded_at_provider=lambda: NOW,
            effective_date_provider=lambda: DAY,
        )
        noop = controller.run(first.person_id)
        assert noop.status is PersonProfileEditStatus.CANCELLED
        assert session.database.current_roster() == before
        edit[0] = "개인 변경"
        result = controller.run(first.person_id)
        assert result.status is PersonProfileEditStatus.COMPLETED
        assert seen == [values[0], values[0]]
        saved = session.database.current_roster()
        routing = routing_state_from_snapshot(
            session.database.snapshot(saved.revision_id)
        )
        assert (
            getattr(routing.resolve(saved, first.person_id, DAY), field) == "개인 변경"
        )
        assert (
            getattr(routing.resolve(saved, second.person_id, DAY), field) == values[0]
        )
        assert (
            getattr(
                routing.resolve(saved, first.person_id, DAY - timedelta(days=1)), field
            )
            == "개인 변경"
        )
