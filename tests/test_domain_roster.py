"""Canonical roster contract tests, independent of Excel."""

from datetime import date
from uuid import UUID

import pytest

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


REVISION_ID = UUID("00000000-0000-0000-0000-000000000001")
PERSON_ID = UUID("00000000-0000-0000-0000-000000000002")
HOUSEHOLD_ID = UUID("00000000-0000-0000-0000-000000000003")
CUSTOM_FIELD_ID = UUID("00000000-0000-0000-0000-000000000004")


def _person() -> Person:
    return Person(
        PERSON_ID,
        PersonProfile(
            name="  홍   길동 ",
            mobile_phone="010-1234-5678",
            school_name="샘플초",
        ),
    )


def test_profile_is_canonical_and_phone_is_only_a_household_candidate():
    person = _person()

    assert person.profile.name == "홍 길동"
    assert person.profile.household_phone_key == "01012345678"
    assert person.profile.custom_values == {}

    full_width = PersonProfile(name="전각", mobile_phone="０１０－１２３４－５６７８")
    assert full_width.household_phone_key == "01012345678"


def test_snapshot_queries_household_and_meal_state_by_inclusive_date():
    person = _person()
    snapshot = RosterSnapshot(
        REVISION_ID,
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
            MealEnrollment(
                PERSON_ID,
                Meal.BREAKFAST,
                InclusiveDateRange(date(2026, 1, 1), None),
            ),
            MealEnrollment(
                PERSON_ID,
                Meal.LUNCH,
                InclusiveDateRange(date(2026, 7, 20), date(2026, 8, 20)),
            ),
        ),
    )

    assert snapshot.household_members(HOUSEHOLD_ID, on=date(2026, 8, 1)) == (
        person,
    )
    assert snapshot.meals_for(PERSON_ID, on=date(2026, 8, 1)) == (
        Meal.BREAKFAST,
        Meal.LUNCH,
    )
    assert snapshot.meals_for(PERSON_ID, on=date(2026, 9, 1)) == (
        Meal.BREAKFAST,
    )


def test_meal_assignment_overrides_individual_fields_and_preserves_zeroes():
    person = _person()
    snapshot = RosterSnapshot(
        REVISION_ID,
        persons=(person,),
        assignments=(
            DeliveryAssignment(
                PERSON_ID,
                driver="기사A",
                sequence="001",
            ),
            DeliveryAssignment(
                PERSON_ID,
                Meal.LUNCH,
                driver="기사B",
            ),
        ),
    )

    assert snapshot.resolve_assignment(PERSON_ID, Meal.BREAKFAST).driver == "기사A"
    assert snapshot.resolve_assignment(PERSON_ID, Meal.BREAKFAST).sequence == "001"
    assert snapshot.resolve_assignment(PERSON_ID, Meal.LUNCH).driver == "기사B"
    assert snapshot.resolve_assignment(PERSON_ID, Meal.LUNCH).sequence == "001"


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (
            InclusiveDateRange(None, date(2026, 6, 30)),
            InclusiveDateRange(date(2026, 6, 30), None),
        ),
        (
            InclusiveDateRange(date(2026, 1, 1), date(2026, 12, 31)),
            InclusiveDateRange(date(2026, 2, 1), date(2026, 2, 2)),
        ),
    ],
)
def test_snapshot_rejects_overlapping_enrollment_periods(first, second):
    person = _person()

    with pytest.raises(ValueError, match="가입 기간.*겹칠"):
        RosterSnapshot(
            REVISION_ID,
            persons=(person,),
            enrollments=(
                MealEnrollment(PERSON_ID, Meal.DINNER, first),
                MealEnrollment(PERSON_ID, Meal.DINNER, second),
            ),
        )


def test_snapshot_rejects_dangling_household_reference():
    person = _person()

    with pytest.raises(ValueError, match="존재하지 않는 가구 ID"):
        RosterSnapshot(
            REVISION_ID,
            persons=(person,),
            memberships=(HouseholdMembership(PERSON_ID, HOUSEHOLD_ID),),
        )


def test_custom_field_definition_validates_person_value_type_and_choice():
    favorite = CustomFieldDefinition(
        CUSTOM_FIELD_ID,
        "선호 반찬",
        CustomFieldType.CHOICE,
        0,
        ("김", "멸치"),
    )
    person = Person(
        PERSON_ID,
        PersonProfile("홍길동", custom_values={CUSTOM_FIELD_ID: "김"}),
    )

    snapshot = RosterSnapshot(
        REVISION_ID,
        persons=(person,),
        custom_fields=(favorite,),
    )

    assert snapshot.person(PERSON_ID).profile.custom_values[CUSTOM_FIELD_ID] == "김"


def test_snapshot_rejects_unknown_or_wrong_typed_custom_value():
    wrong_type = Person(
        PERSON_ID,
        PersonProfile("홍길동", custom_values={CUSTOM_FIELD_ID: "숫자 아님"}),
    )
    number = CustomFieldDefinition(
        CUSTOM_FIELD_ID,
        "지원 횟수",
        CustomFieldType.NUMBER,
        0,
    )

    with pytest.raises(TypeError, match="숫자"):
        RosterSnapshot(
            REVISION_ID,
            persons=(wrong_type,),
            custom_fields=(number,),
        )

    with pytest.raises(ValueError, match="정의되지 않은"):
        RosterSnapshot(REVISION_ID, persons=(wrong_type,))


def test_meal_selection_orders_without_modifying_the_input():
    from src.domain.roster import canonical_meals

    selected = [Meal.DINNER, Meal.BREAKFAST]
    assert canonical_meals(selected) == (Meal.BREAKFAST, Meal.DINNER)
    assert selected == [Meal.DINNER, Meal.BREAKFAST]


@pytest.mark.parametrize("selected,error", [([], ValueError), ([Meal.LUNCH, Meal.LUNCH], ValueError), (["조식"], TypeError)])
def test_meal_selection_rejects_empty_duplicate_or_untyped_values(selected, error):
    from src.domain.roster import canonical_meals

    with pytest.raises(error):
        canonical_meals(selected)


def test_indexed_roster_queries_do_not_scan_unrelated_people():
    class CountedUUID(UUID):
        comparisons = 0
        __hash__ = UUID.__hash__

        def __eq__(self, other):
            type(self).comparisons += 1
            return super().__eq__(other)

    people = tuple(Person(CountedUUID(int=i + 100), PersonProfile(str(i))) for i in range(500))
    snapshot = RosterSnapshot(
        REVISION_ID, people, (Household(HOUSEHOLD_ID),),
        tuple(HouseholdMembership(person.person_id, HOUSEHOLD_ID) for person in people),
        tuple(MealEnrollment(person.person_id, meal) for person in people for meal in Meal),
    )
    CountedUUID.comparisons = 0
    # A distinct equal UUID exercises dictionary equality rather than identity.
    person_id = UUID(int=people[-1].person_id.int)
    assert snapshot.meals_for(person_id, on=date(2026, 10, 19)) == tuple(Meal)
    assert snapshot.household_for(person_id, on=date(2026, 10, 19)).household_id == HOUSEHOLD_ID
    assert CountedUUID.comparisons <= 8


def test_roster_indexes_preserve_boundaries_gaps_and_rebuild_after_replace():
    from dataclasses import replace

    second = UUID(int=99)
    snapshot = RosterSnapshot(
        REVISION_ID, (_person(),), (Household(HOUSEHOLD_ID), Household(second)),
        (
            HouseholdMembership(PERSON_ID, HOUSEHOLD_ID, InclusiveDateRange(None, date(2026, 12, 31))),
            HouseholdMembership(PERSON_ID, second, InclusiveDateRange(date(2027, 1, 2), None)),
        ),
        (
            MealEnrollment(PERSON_ID, Meal.DINNER, InclusiveDateRange(None, date(2026, 12, 31))),
            MealEnrollment(PERSON_ID, Meal.LUNCH, InclusiveDateRange(date(2027, 1, 2), None)),
            MealEnrollment(PERSON_ID, Meal.BREAKFAST),
        ),
    )
    for day in (date(2026, 12, 31), date(2027, 1, 1), date(2027, 1, 2)):
        expected_household = next((m.household_id for m in snapshot.memberships if m.period.contains(day)), None)
        actual_household = snapshot.household_for(PERSON_ID, on=day)
        assert (actual_household.household_id if actual_household else None) == expected_household
        active = {e.meal for e in snapshot.enrollments if e.period.contains(day)}
        assert snapshot.meals_for(PERSON_ID, on=day) == tuple(m for m in Meal if m in active)
    changed = replace(snapshot, memberships=(), enrollments=())
    assert changed.meals_for(PERSON_ID, on=date(2027, 1, 2)) == ()
    assert changed.household_for(PERSON_ID, on=date(2027, 1, 2)) is None
    assert snapshot.meals_for(PERSON_ID, on=date(2027, 1, 2)) == (Meal.BREAKFAST, Meal.LUNCH)
    with pytest.raises(KeyError):
        changed.meals_for(UUID(int=999), on=date(2027, 1, 2))
    with pytest.raises(TypeError):
        changed.household_for(PERSON_ID, on='2027-01-02')
