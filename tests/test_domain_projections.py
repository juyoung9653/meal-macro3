"""Canonical roster/status audit projection contract tests."""

from datetime import UTC, date, datetime
from uuid import UUID

from src.domain.projections import (
    ASSIGNMENT_KIND,
    CUSTOM_FIELD_KIND,
    ENROLLMENT_KIND,
    PERSON_KIND,
    STATUS_EVENT_KIND,
    roster_audit_changes,
    roster_from_historical_snapshot,
    status_event_audit_change,
)
from src.domain.revisions import (
    ChangeOperation,
    EntityRef,
    HistoricalSnapshot,
)
from src.domain.roster import (
    CustomFieldDefinition,
    CustomFieldType,
    DeliveryAssignment,
    Household,
    HouseholdMembership,
    InclusiveDateRange,
    Meal,
    MealEnrollment,
    Person,
    PersonProfile,
    RosterSnapshot,
)
from src.domain.status import (
    MealStatus,
    StatusEvent,
)


FIRST_REVISION_ID = UUID("50000000-0000-0000-0000-000000000001")
SECOND_REVISION_ID = UUID("50000000-0000-0000-0000-000000000002")
PERSON_ID = UUID("50000000-0000-0000-0000-000000000003")
HOUSEHOLD_ID = UUID("50000000-0000-0000-0000-000000000004")
CUSTOM_FIELD_ID = UUID("50000000-0000-0000-0000-000000000005")
EVENT_ID = UUID("50000000-0000-0000-0000-000000000006")


def _snapshot(
    revision_id: UUID,
    *,
    name: str = "홍길동",
    enrolled: bool = True,
    assigned: bool = True,
) -> RosterSnapshot:
    person = Person(
        PERSON_ID,
        PersonProfile(
            name=name,
            birth_date=date(2015, 3, 2),
            mobile_phone="010-1234-5678",
            custom_values={CUSTOM_FIELD_ID: date(2026, 8, 17)},
        ),
    )
    return RosterSnapshot(
        revision_id,
        persons=(person,),
        households=(Household(HOUSEHOLD_ID),),
        memberships=(
            HouseholdMembership(
                PERSON_ID,
                HOUSEHOLD_ID,
                InclusiveDateRange(date(2026, 1, 1), None),
            ),
        ),
        enrollments=(
            (
                MealEnrollment(
                    PERSON_ID,
                    Meal.DINNER,
                    InclusiveDateRange(date(2026, 1, 1), None),
                ),
            )
            if enrolled
            else ()
        ),
        assignments=(
            (DeliveryAssignment(PERSON_ID, Meal.DINNER, "기사A", "001"),)
            if assigned
            else ()
        ),
        custom_fields=(
            CustomFieldDefinition(
                CUSTOM_FIELD_ID,
                "등록일",
                CustomFieldType.DATE,
                0,
            ),
        ),
    )


def test_roster_changes_rehydrate_all_typed_entities():
    source = _snapshot(FIRST_REVISION_ID)
    changes = roster_audit_changes(None, source)
    entities = {
        EntityRef(change.entity_kind, change.entity_id): change.after
        for change in changes
    }

    restored = roster_from_historical_snapshot(
        HistoricalSnapshot(FIRST_REVISION_ID, entities)
    )

    assert restored.person(PERSON_ID).profile == source.person(PERSON_ID).profile
    assert restored.households == source.households
    assert restored.memberships == source.memberships
    assert restored.enrollments == source.enrollments
    assert restored.assignments == source.assignments
    assert {change.entity_kind for change in changes} == {
        PERSON_KIND,
        "roster.household",
        "roster.household_memberships",
        ENROLLMENT_KIND,
        ASSIGNMENT_KIND,
        CUSTOM_FIELD_KIND,
    }


def test_household_delivery_settings_survive_historical_projection():
    from dataclasses import replace
    from src.domain.roster import HouseholdDeliverySetting

    source = _snapshot(FIRST_REVISION_ID)
    settings = (
        HouseholdDeliverySetting(Meal.DINNER, (0, 1, 2, 3, 4), "식재료 박스"),
        HouseholdDeliverySetting(Meal.BREAKFAST, (0, 1, 2, 3, 4, 5, 6), delivery_weekdays=(0, 1, 2, 3, 4, 3, 3)),
    )
    source = replace(source, households=(Household(HOUSEHOLD_ID, settings),))
    entities = {EntityRef(change.entity_kind, change.entity_id): change.after
                for change in roster_audit_changes(None, source)}
    restored = roster_from_historical_snapshot(HistoricalSnapshot(FIRST_REVISION_ID, entities))
    assert restored.households == source.households
    assert tuple(item.meal for item in restored.households[0].delivery_settings) == (Meal.BREAKFAST, Meal.DINNER)


def test_optional_meal_and_assignment_entities_update_instead_of_reusing_ids():
    before = _snapshot(FIRST_REVISION_ID)
    after = _snapshot(
        SECOND_REVISION_ID,
        enrolled=False,
        assigned=False,
    )

    changes = roster_audit_changes(before, after)

    assert len(changes) == 2
    assert {change.entity_kind for change in changes} == {
        ENROLLMENT_KIND,
        ASSIGNMENT_KIND,
    }
    assert all(change.operation is ChangeOperation.UPDATE for change in changes)
    assert all(change.before is not None and change.after is not None for change in changes)


def test_status_event_change_preserves_raw_wording_and_revision():
    event = StatusEvent(
        event_id=EVENT_ID,
        revision_id=SECOND_REVISION_ID,
        person_id=PERSON_ID,
        meal=Meal.DINNER,
        status=MealStatus.STOPPED,
        period=InclusiveDateRange(date(2026, 8, 17), None),
        recorded_at=datetime(2026, 8, 24, 16, 0, tzinfo=UTC),
        source_text="17일 중지",
    )

    change = status_event_audit_change(event)

    assert change.entity_kind == STATUS_EVENT_KIND
    assert change.operation is ChangeOperation.INSERT
    assert change.after["revision_id"] == str(SECOND_REVISION_ID)
    assert change.after["source_text"] == "17일 중지"
