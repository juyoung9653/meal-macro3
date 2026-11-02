from datetime import date
from uuid import UUID

from src.domain.roster import (
    DeliveryAssignment,
    CustomFieldDefinition,
    CustomFieldType,
    Household,
    HouseholdMembership,
    InclusiveDateRange,
    Meal,
    MealEnrollment,
    Person,
    PersonProfile,
    RosterSnapshot,
)
from src.domain.delivery_routing import Driver, RoutingEvent, RoutingState
from src.services.driver_households import build_driver_household_rows


DAY = date(2026, 10, 1)


def uid(value: int) -> UUID:
    return UUID(f"10000000-0000-0000-0000-{value:012d}")


def person(
    value: int,
    name: str,
    *,
    address: str | None = "성남대로 1",
    phone: str | None = None,
    telephone: str | None = None,
    custom_values=None,
):
    return Person(
        uid(value),
        PersonProfile(
            name,
            road_address=address,
            mobile_phone=phone,
            telephone=telephone,
            custom_values=custom_values or {},
        ),
    )


def roster(*people, memberships=(), meals=None, assignments=(), custom_fields=()):
    meals = meals or {item.person_id: tuple(Meal) for item in people}
    household_ids = {membership.household_id for membership in memberships}
    return RosterSnapshot(
        uid(999),
        persons=people,
        households=tuple(Household(value) for value in household_ids),
        memberships=memberships,
        enrollments=tuple(
            MealEnrollment(person_id, meal)
            for person_id, person_meals in meals.items()
            for meal in person_meals
        ),
        assignments=assignments,
        custom_fields=custom_fields,
    )


def test_same_household_same_driver_merges_members_and_meals():
    first, second, household = person(1, "첫째", phone="010-1"), person(2, "둘째", phone="010-1"), uid(50)
    state = RoutingState((Driver(uid(10), "김기사"),))
    snapshot = roster(
        first, second,
        memberships=(HouseholdMembership(first.person_id, household), HouseholdMembership(second.person_id, household)),
        meals={first.person_id: (Meal.BREAKFAST,), second.person_id: (Meal.LUNCH,)},
        assignments=(DeliveryAssignment(first.person_id, None, "김기사", "002"), DeliveryAssignment(second.person_id, None, "김기사", "001")),
    )

    rows = build_driver_household_rows(snapshot, state, DAY)

    assert len(rows) == 1
    assert rows[0].member_ids == (first.person_id, second.person_id)
    assert rows[0].member_names == ("첫째", "둘째")
    assert rows[0].meals == (Meal.BREAKFAST, Meal.LUNCH)
    assert rows[0].contacts == ("010-1",)
    assert rows[0].sequences == ("001", "002")
    assert [row.meals for row in rows[0].meal_rows] == [
        (Meal.BREAKFAST,), (Meal.LUNCH,)
    ]
    assert [row.member_ids for row in rows[0].meal_rows] == [
        (first.person_id,), (second.person_id,)
    ]
    assert [row.sequences for row in rows[0].meal_rows] == [("002",), ("001",)]


def test_meal_rows_keep_only_the_members_contacts_and_sequences_for_that_meal():
    first = person(1, "첫째", phone="010-1")
    second = person(2, "둘째", phone="010-2")
    household = uid(50)
    snapshot = roster(
        first, second,
        memberships=(
            HouseholdMembership(first.person_id, household),
            HouseholdMembership(second.person_id, household),
        ),
        meals={
            first.person_id: (Meal.DINNER, Meal.BREAKFAST),
            second.person_id: (Meal.BREAKFAST, Meal.LUNCH),
        },
        assignments=(
            DeliveryAssignment(first.person_id, Meal.DINNER, "기사", "003"),
            DeliveryAssignment(first.person_id, Meal.BREAKFAST, "기사", "002"),
            DeliveryAssignment(second.person_id, Meal.BREAKFAST, "기사", "001"),
            DeliveryAssignment(second.person_id, Meal.LUNCH, "기사", "004"),
        ),
    )
    (merged,) = build_driver_household_rows(snapshot, RoutingState(()), DAY)
    dinner, breakfast, lunch = merged.meal_rows
    assert merged.meals == (Meal.DINNER, Meal.BREAKFAST, Meal.LUNCH)
    assert dinner.member_ids == (first.person_id,)
    assert dinner.contacts == ("010-1",)
    assert dinner.sequences == ("003",)
    assert breakfast.member_ids == (first.person_id, second.person_id)
    assert breakfast.contacts == ("010-1", "010-2")
    assert breakfast.sequences == ("001", "002")
    assert lunch.member_ids == (second.person_id,)
    assert lunch.contacts == ("010-2",)
    assert lunch.sequences == ("004",)


def test_explicit_household_merges_empty_addresses_and_additional_contacts():
    telephone2, mobile2 = uid(70), uid(71)
    first, second, household = (
        person(
            1,
            "첫째",
            address=None,
            telephone="031-1",
            phone="010-1",
            custom_values={telephone2: "031-2", mobile2: "010-2"},
        ),
        person(2, "둘째", address=None, phone="010-1"),
        uid(50),
    )
    state = RoutingState((Driver(uid(10), "김기사"),))
    snapshot = roster(
        first,
        second,
        memberships=(HouseholdMembership(first.person_id, household), HouseholdMembership(second.person_id, household)),
        meals={first.person_id: (Meal.BREAKFAST,), second.person_id: (Meal.LUNCH,)},
        assignments=(DeliveryAssignment(first.person_id, None, "김기사", "1"), DeliveryAssignment(second.person_id, None, "김기사", "2")),
        custom_fields=(
            CustomFieldDefinition(telephone2, "전화번호2", CustomFieldType.TEXT, 0),
            CustomFieldDefinition(mobile2, "핸드폰2", CustomFieldType.TEXT, 1),
        ),
    )

    rows = build_driver_household_rows(snapshot, state, DAY)

    assert len(rows) == 1
    assert rows[0].address == ""
    assert rows[0].contacts == ("031-1", "010-1", "031-2", "010-2")


def test_mixed_driver_meals_and_homonymous_driver_ids_stay_separate():
    first, second, household = person(1, "첫째"), person(2, "둘째"), uid(50)
    left, right = Driver(uid(10), "동명이인"), Driver(uid(11), "동명이인")
    snapshot = roster(
        first, second,
        memberships=(HouseholdMembership(first.person_id, household), HouseholdMembership(second.person_id, household)),
        meals={first.person_id: (Meal.BREAKFAST,), second.person_id: (Meal.LUNCH,)},
    )
    state = RoutingState((left, right), (
        RoutingEvent(uid(60), first.person_id, DAY, 1, {Meal.BREAKFAST: left.driver_id}),
        RoutingEvent(uid(61), second.person_id, DAY, 2, {Meal.LUNCH: right.driver_id}),
    ))

    rows = build_driver_household_rows(snapshot, state, DAY)

    assert {(row.driver_id, row.meals) for row in rows} == {
        (left.driver_id, (Meal.BREAKFAST,)),
        (right.driver_id, (Meal.LUNCH,)),
    }


def test_routing_is_dated_but_household_links_are_current():
    first, second = person(1, "첫째"), person(2, "둘째")
    household = uid(50)
    old, new = Driver(uid(10), "이전"), Driver(uid(11), "새기사")
    snapshot = roster(
        first, second,
        memberships=(HouseholdMembership(first.person_id, household, InclusiveDateRange(DAY)),),
        meals={first.person_id: (Meal.LUNCH,), second.person_id: (Meal.LUNCH,)},
        assignments=(DeliveryAssignment(first.person_id, None, "이전", "9"), DeliveryAssignment(second.person_id, None, "이전", "1")),
    )
    state = RoutingState((old, new), (RoutingEvent(uid(60), first.person_id, DAY, 1, {Meal.LUNCH: new.driver_id}),))

    before = build_driver_household_rows(snapshot, state, date(2026, 9, 30))
    active = build_driver_household_rows(snapshot, state, DAY)

    assert {row.household_key for row in before} == {household, snapshot.current_membership(second.person_id).household_id}
    active_by_household = {row.household_key: row for row in active}
    assert active_by_household[snapshot.current_membership(second.person_id).household_id].driver_id == old.driver_id
    assert active_by_household[household].driver_id == new.driver_id


def test_ambiguous_legacy_name_is_not_merged_with_registered_driver():
    first, second, household = person(1, "첫째"), person(2, "둘째"), uid(50)
    first_duplicate = Driver(uid(10), "동명이인")
    second_duplicate = Driver(uid(11), "동명이인")
    snapshot = roster(
        first, second,
        memberships=(HouseholdMembership(first.person_id, household), HouseholdMembership(second.person_id, household)),
        meals={first.person_id: (Meal.BREAKFAST,), second.person_id: (Meal.LUNCH,)},
        assignments=(DeliveryAssignment(first.person_id, None, "동명이인", "1"),),
    )
    state = RoutingState((first_duplicate, second_duplicate), (
        RoutingEvent(uid(60), second.person_id, DAY, 1, {Meal.LUNCH: first_duplicate.driver_id}),
    ))

    rows = build_driver_household_rows(snapshot, state, DAY)

    assert len(rows) == 2
    assert {row.driver_id for row in rows} == {None, first_duplicate.driver_id}


def test_included_meals_filters_members_before_aggregating_different_addresses():
    first, second = person(1, "첫째", address="성남 1"), person(2, "둘째", address="성남 2")
    household = uid(50)
    driver = Driver(uid(10), "김기사")
    snapshot = roster(
        first, second,
        memberships=(HouseholdMembership(first.person_id, household), HouseholdMembership(second.person_id, household)),
        meals={first.person_id: (Meal.BREAKFAST, Meal.LUNCH), second.person_id: (Meal.LUNCH,)},
        assignments=(DeliveryAssignment(first.person_id, None, "김기사", "2"), DeliveryAssignment(second.person_id, None, "김기사", "1")),
    )

    rows = build_driver_household_rows(
        snapshot, RoutingState((driver,)), DAY,
        included_meals={first.person_id: (Meal.LUNCH, Meal.DINNER), uid(999): (Meal.LUNCH,)},
    )

    assert len(rows) == 1
    assert rows[0].member_ids == (first.person_id,)
    assert rows[0].meals == (Meal.LUNCH,)

    all_rows = build_driver_household_rows(snapshot, RoutingState((driver,)), DAY)
    assert len(all_rows) == 1
    assert all_rows[0].address == "성남 1\n성남 2"
