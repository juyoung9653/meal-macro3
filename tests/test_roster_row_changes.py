"""A roster row change keeps its dependents and audit revision consistent."""

from datetime import UTC, date, datetime
from uuid import UUID

import pytest

from src.domain.delivery_routing import DRIVER_KIND, Driver, RoutingState, routing_state_from_snapshot
from src.domain.entitlements import EntitlementExclusion, decide_entitlement
from src.domain.projections import PERSON_KIND, roster_audit_changes
from src.domain.revisions import AuditChange, ChangeOperation, RevisionActor, RevisionMetadata, RevisionRecord, RevisionSource
from src.domain.roster import CustomFieldDefinition, CustomFieldType, DeliveryAssignment, Household, HouseholdMembership, Meal, MealEnrollment, Person, PersonProfile, RosterSnapshot
from src.domain.roster_row_changes import build_roster_row_revision
from src.domain.status import MealStatus
from src.storage.encrypted_database import EncryptedDatabase


OLD = UUID("89000000-0000-0000-0000-000000000001")
NEW = UUID("89000000-0000-0000-0000-000000000002")
TARGET = UUID("89000000-0000-0000-0000-000000000003")
KEEP = UUID("89000000-0000-0000-0000-000000000004")
HOUSEHOLD = UUID("89000000-0000-0000-0000-000000000005")
ARGS = dict(actor=RevisionActor("tester", "test"), recorded_at=datetime(2026, 9, 29, tzinfo=UTC), effective_on=date(2026, 9, 29), revision_id=NEW)


def _roster():
    return RosterSnapshot(
        OLD,
        persons=(Person(TARGET, PersonProfile("삭제 대상")), Person(KEEP, PersonProfile("유지 대상"))),
        households=(Household(HOUSEHOLD),),
        memberships=(HouseholdMembership(TARGET, HOUSEHOLD), HouseholdMembership(KEEP, HOUSEHOLD)),
        enrollments=(MealEnrollment(TARGET, Meal.LUNCH), MealEnrollment(TARGET, Meal.DINNER), MealEnrollment(KEEP, Meal.LUNCH)),
        assignments=(DeliveryAssignment(TARGET, Meal.LUNCH, sequence="001"),),
    )


def test_add_person_enrolls_selected_meals_and_audits_insert():
    person = Person(UUID("89000000-0000-0000-0000-000000000006"), PersonProfile("새 대상"))
    original = _roster()
    result = build_roster_row_revision(original, add_person=person, enroll_meals=(Meal.DINNER, Meal.LUNCH), delivery_starts_on=date(2026, 10, 5), **ARGS)
    assert result.snapshot.person(person.person_id) == person
    assert result.snapshot.meals_for(person.person_id, on=date(2026, 10, 4)) == ()
    assert result.snapshot.meals_for(person.person_id, on=date(2026, 10, 5)) == (Meal.LUNCH, Meal.DINNER)
    assert result.snapshot.meals_for(person.person_id, on=date(2026, 10, 6)) == (Meal.LUNCH, Meal.DINNER)
    members = [item for item in result.snapshot.memberships if item.person_id == person.person_id]
    assert len(members) == 1
    assert members[0].period.starts_on is None
    assert members[0].household_id not in {item.household_id for item in original.households}
    assert {item.household_id for item in result.snapshot.households} == {HOUSEHOLD, members[0].household_id}
    assert result.snapshot.meals_for(KEEP, on=date(2026, 10, 4)) == original.meals_for(KEEP, on=date(2026, 10, 4))
    assert any(change.entity_kind == PERSON_KIND and change.entity_id == person.person_id and change.operation is ChangeOperation.INSERT for change in result.record.changes)
    assert result.record.metadata.parent_revision_id == OLD


@pytest.mark.parametrize("household_id", [None, HOUSEHOLD])
def test_add_start_date_persists_through_revision_reload(tmp_path, household_id):
    original = _roster()
    person = Person(UUID("89000000-0000-0000-0000-000000000006"), PersonProfile("새 대상"))
    initial = RevisionRecord(
        RevisionMetadata(OLD, None, ARGS["recorded_at"], ARGS["effective_on"], ARGS["actor"], "초기 명단", RevisionSource.UI),
        roster_audit_changes(None, original),
    )
    added = build_roster_row_revision(original, add_person=person, enroll_meals=(Meal.LUNCH,), delivery_starts_on=date(2026, 10, 5), household_id=household_id, **ARGS)
    expected_household = added.snapshot.household_for(person.person_id, on=date(2026, 10, 5))
    with EncryptedDatabase.create(tmp_path / "rows.db", b"k" * 32) as database:
        database.append_revision(initial)
        database.append_revision(added.record)
        restored = database.current_roster()
        assert restored is not None
        for day, expected in ((date(2026, 10, 4), ()), (date(2026, 10, 5), (Meal.LUNCH,)), (date(2026, 10, 6), (Meal.LUNCH,))):
            assert restored.meals_for(person.person_id, on=day) == expected
        assert restored.household_for(person.person_id, on=date(2026, 9, 1)) == expected_household
        assert restored.memberships == added.snapshot.memberships
    with EncryptedDatabase.open(tmp_path / "rows.db", b"k" * 32) as database:
        restored = database.current_roster()
        for day in (date.min, date(2026, 9, 1), date(2026, 10, 4)):
            assert restored.household_for(person.person_id, on=day) == expected_household
            decision = decide_entitlement(restored, person.person_id, Meal.LUNCH, day)
            assert not decision.entitled
            assert EntitlementExclusion.NOT_ENROLLED in decision.exclusions
        assert database.snapshot(OLD).entity(PERSON_KIND, person.person_id) is None


def test_add_full_row_persists_distinct_registered_drivers_sequences_and_status(tmp_path):
    fields = (
        CustomFieldDefinition(UUID("89000000-0000-0000-0000-000000000011"), "추가 전화", CustomFieldType.TEXT, 0),
        CustomFieldDefinition(UUID("89000000-0000-0000-0000-000000000012"), "수량", CustomFieldType.NUMBER, 1),
        CustomFieldDefinition(UUID("89000000-0000-0000-0000-000000000013"), "확인일", CustomFieldType.DATE, 2),
        CustomFieldDefinition(UUID("89000000-0000-0000-0000-000000000014"), "구분", CustomFieldType.CHOICE, 3, ("A", "B")),
    )
    base = _roster()
    original = RosterSnapshot(OLD, base.persons, base.households, base.memberships, base.enrollments, base.assignments, fields)
    person = Person(UUID("89000000-0000-0000-0000-000000000006"), PersonProfile("새 대상", class_name="2", provider="기관", telephone="031-1", custom_values={fields[0].definition_id: "010-222", fields[1].definition_id: 2.5, fields[2].definition_id: date(2026, 10, 5), fields[3].definition_id: "B"}))
    first = Driver(UUID("89000000-0000-0000-0000-000000000007"), "동명이인", "010-1")
    second = Driver(UUID("89000000-0000-0000-0000-000000000008"), "동명이인", "010-2")
    initial = RevisionRecord(
        RevisionMetadata(OLD, None, ARGS["recorded_at"], ARGS["effective_on"], ARGS["actor"], "초기 명단", RevisionSource.UI),
        (*roster_audit_changes(None, original),
         AuditChange(DRIVER_KIND, first.driver_id, ChangeOperation.INSERT, None, {"name": first.name, "telephone": first.telephone}),
         AuditChange(DRIVER_KIND, second.driver_id, ChangeOperation.INSERT, None, {"name": second.name, "telephone": second.telephone})),
    )
    added = build_roster_row_revision(
        original, add_person=person, enroll_meals=(Meal.DINNER, Meal.LUNCH),
        delivery_starts_on=date(2026, 10, 5),
        delivery_starts_by_meal={
            Meal.DINNER: date(2026, 10, 5),
            Meal.LUNCH: date(2026, 10, 6),
        },
        meal_assignments=(DeliveryAssignment(person.person_id, Meal.DINNER, sequence="001"), DeliveryAssignment(person.person_id, Meal.LUNCH, sequence="013")),
        household_id=HOUSEHOLD, driver_ids={Meal.DINNER: second.driver_id, Meal.LUNCH: first.driver_id},
        routing_state=RoutingState((first, second)),
        routing_event_ids={
            date(2026, 10, 5): UUID("89000000-0000-0000-0000-000000000009"),
            date(2026, 10, 6): UUID("89000000-0000-0000-0000-000000000015"),
        },
        initial_statuses={Meal.DINNER: (MealStatus.PAUSED, date(2026, 10, 7))},
        status_event_ids={Meal.DINNER: UUID("89000000-0000-0000-0000-000000000010")}, **ARGS,
    )
    with EncryptedDatabase.create(tmp_path / "rows.db", b"k" * 32) as database:
        database.append_revision(initial)
        database.append_revision(added.record)
        restored = database.current_roster()
        assert restored.person(person.person_id).profile == person.profile
        assert restored.resolve_assignment(person.person_id, Meal.DINNER).sequence == "001"
        assert restored.resolve_assignment(person.person_id, Meal.LUNCH).sequence == "013"
        assert any(item.person_id == person.person_id and item.household_id == HOUSEHOLD for item in restored.memberships)
        state = routing_state_from_snapshot(database.snapshot(NEW))
        assert state.resolve(restored, person.person_id, date(2026, 10, 4)).driver_ids[Meal.DINNER] is None
        assert state.resolve(restored, person.person_id, date(2026, 10, 5)).driver_ids == {Meal.DINNER: second.driver_id, Meal.BREAKFAST: None, Meal.LUNCH: None}
        assert state.resolve(restored, person.person_id, date(2026, 10, 6)).driver_ids == {Meal.DINNER: second.driver_id, Meal.BREAKFAST: None, Meal.LUNCH: first.driver_id}
        assert [(event.status, event.period.starts_on, event.period.ends_on) for event in database.status_events(person_id=person.person_id, meal=Meal.DINNER)] == [(MealStatus.PAUSED, date(2026, 10, 5), date(2026, 10, 7))]
        assert restored.meals_for(person.person_id, on=date(2026, 10, 4)) == ()
        assert restored.meals_for(person.person_id, on=date(2026, 10, 5)) == (Meal.DINNER,)


def test_delete_removes_one_person_and_current_dependents_only():
    original = _roster()
    result = build_roster_row_revision(original, delete_person_id=TARGET, **ARGS)
    with pytest.raises(KeyError):
        result.snapshot.person(TARGET)
    assert result.snapshot.person(KEEP) == original.person(KEEP)
    assert all(item.person_id != TARGET for collection in (result.snapshot.memberships, result.snapshot.enrollments, result.snapshot.assignments) for item in collection)
    assert result.snapshot.households == original.households
    assert any(change.entity_kind == PERSON_KIND and change.entity_id == TARGET and change.operation is ChangeOperation.DELETE for change in result.record.changes)
    assert result.record.metadata.decisions["roster_row_change"]["operation"] == "delete"


def test_add_rejects_empty_meals_and_delete_rejects_missing_person():
    with pytest.raises(ValueError, match="하나 이상의 식종"):
        build_roster_row_revision(_roster(), add_person=Person(UUID("89000000-0000-0000-0000-000000000006"), PersonProfile("새 대상")), **ARGS)
    with pytest.raises(KeyError):
        build_roster_row_revision(_roster(), delete_person_id=UUID("89000000-0000-0000-0000-000000000007"), **ARGS)


def test_add_rejects_one_invalid_meal_start_before_building_revision():
    person = Person(
        UUID("89000000-0000-0000-0000-000000000016"),
        PersonProfile("날짜 검증"),
    )
    with pytest.raises(ValueError, match="조식.*금요일"):
        build_roster_row_revision(
            _roster(),
            add_person=person,
            enroll_meals=(Meal.DINNER, Meal.BREAKFAST),
            delivery_starts_by_meal={
                Meal.DINNER: date(2026, 10, 5),
                Meal.BREAKFAST: date(2026, 10, 5),
            },
            **ARGS,
        )


def test_delete_persists_and_reloads_without_orphaned_roster_references(tmp_path):
    original = _roster()
    initial = RevisionRecord(
        RevisionMetadata(OLD, None, ARGS["recorded_at"], ARGS["effective_on"], ARGS["actor"], "초기 명단", RevisionSource.UI),
        roster_audit_changes(None, original),
    )
    deleted = build_roster_row_revision(original, delete_person_id=TARGET, **ARGS)
    with EncryptedDatabase.create(tmp_path / "rows.db", b"k" * 32) as database:
        database.append_revision(initial)
        database.append_revision(deleted.record)
        restored = database.current_roster()
        assert restored is not None
        assert restored.person(KEEP).profile.name == "유지 대상"
        with pytest.raises(KeyError):
            restored.person(TARGET)
        assert restored.memberships == deleted.snapshot.memberships
        assert restored.enrollments == deleted.snapshot.enrollments
        assert restored.assignments == deleted.snapshot.assignments
