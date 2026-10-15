"""Sequence meal groups combine with AND while values within a meal use OR."""

from dataclasses import replace
from uuid import uuid4

from src.desktop_v2.column_search import (
    ALL_SEQUENCES_EMPTY,
    sequence_exclusive_token,
    sequence_pair_token,
    sequence_present_token,
)
from src.desktop_v2.roster_window import RosterTableModel
from src.domain.roster import (
    DeliveryAssignment,
    Meal,
    MealEnrollment,
    Person,
    PersonProfile,
    RosterSnapshot,
)


def sequence_roster():
    names = (
        "중식12",
        "중식12석식9",
        "중식001",
        "중식1",
        "순서없음",
        "미가입빈값",
        "석식보존",
        "공통미지정",
        "중식12조식3",
        "공통잔존",
    )
    people = tuple(Person(uuid4(), PersonProfile(name)) for name in names)
    enrollments = (
        MealEnrollment(people[0].person_id, Meal.LUNCH),
        MealEnrollment(people[1].person_id, Meal.LUNCH),
        MealEnrollment(people[1].person_id, Meal.DINNER),
        MealEnrollment(people[2].person_id, Meal.LUNCH),
        MealEnrollment(people[3].person_id, Meal.LUNCH),
        MealEnrollment(people[4].person_id, Meal.LUNCH),
        MealEnrollment(people[6].person_id, Meal.LUNCH),
        MealEnrollment(people[8].person_id, Meal.LUNCH),
        MealEnrollment(people[8].person_id, Meal.BREAKFAST),
        MealEnrollment(people[9].person_id, Meal.LUNCH),
    )
    assignments = (
        DeliveryAssignment(people[0].person_id, Meal.LUNCH, sequence="12"),
        DeliveryAssignment(people[1].person_id, Meal.LUNCH, sequence="12"),
        DeliveryAssignment(people[1].person_id, Meal.DINNER, sequence="9"),
        DeliveryAssignment(people[2].person_id, Meal.LUNCH, sequence="001"),
        DeliveryAssignment(people[3].person_id, Meal.LUNCH, sequence="1"),
        DeliveryAssignment(people[6].person_id, Meal.LUNCH, sequence="12"),
        DeliveryAssignment(people[6].person_id, Meal.DINNER, sequence="8"),
        DeliveryAssignment(people[7].person_id, None, sequence="공통"),
        DeliveryAssignment(people[8].person_id, Meal.LUNCH, sequence="12"),
        DeliveryAssignment(people[8].person_id, Meal.BREAKFAST, sequence="3"),
        DeliveryAssignment(people[9].person_id, None, sequence="잔존"),
        DeliveryAssignment(people[9].person_id, Meal.LUNCH, driver="기사", sequence=None),
    )
    return RosterSnapshot(
        uuid4(),
        persons=people,
        enrollments=enrollments,
        assignments=assignments,
    )


def names(model):
    return {model.person_at(index).profile.name for index in range(model.rowCount())}


def test_sequence_filter_three_meal_truth_table_and_exact_values():
    model = RosterTableModel(sequence_roster())
    pair = sequence_pair_token

    model.set_column_values("sequence", {pair(Meal.LUNCH.value, "12")})
    assert names(model) == {"중식12"}

    model.set_column_values("sequence", {
        pair(Meal.LUNCH.value, "12"),
        pair(Meal.DINNER.value, "9"),
    })
    assert names(model) == {"중식12석식9"}

    model.set_column_values("sequence", {
        pair(Meal.LUNCH.value, "12"),
        pair(Meal.BREAKFAST.value, "3"),
    })
    assert names(model) == {"중식12조식3"}

    model.set_column_values("sequence", {pair(Meal.LUNCH.value, "001")})
    assert names(model) == {"중식001"}
    model.set_column_values("sequence", {pair(Meal.LUNCH.value, "1")})
    assert names(model) == {"중식1"}

    model.set_column_values("sequence", {
        pair(Meal.LUNCH.value, ""),
        pair(Meal.LUNCH.value, "12"),
    })
    assert names(model) == {"중식12", "순서없음", "미가입빈값"}


def test_blank_none_common_retained_and_legacy_contracts():
    roster = sequence_roster()
    model = RosterTableModel(roster)
    pair = sequence_pair_token

    model.set_column_values("sequence", frozenset())
    assert names(model) == {"순서없음", "미가입빈값"}

    model.set_column_values("sequence", {
        pair(Meal.DINNER.value, ""),
        pair(Meal.BREAKFAST.value, ""),
        pair(Meal.LUNCH.value, ""),
    })
    assert names(model) == {"순서없음", "미가입빈값"}

    model.set_column_values("sequence", None)
    assert model.rowCount() == len(roster.persons)

    common = pair("공통/식종 미지정", "공통")
    assert common in model.column_filter_values("sequence")
    model.set_column_values("sequence", {common})
    assert names(model) == {"공통미지정"}

    model.set_column_values("sequence", {"12"})
    assert names(model) == {"중식12"}
    model.set_column_values("sequence", {"없는 예전 값"})
    assert model.rowCount() == 0


def test_old_structured_tokens_use_and_semantics_and_do_not_bypass_constraints():
    model = RosterTableModel(sequence_roster())

    model.set_column_values("sequence", {sequence_present_token(Meal.LUNCH.value)})
    assert names(model) == {"중식12", "중식001", "중식1", "공통잔존"}

    model.set_column_values("sequence", {
        sequence_present_token(Meal.LUNCH.value),
        sequence_present_token(Meal.DINNER.value),
    })
    assert names(model) == {"중식12석식9", "석식보존"}

    model.set_column_values("sequence", {sequence_exclusive_token(Meal.LUNCH.value)})
    assert names(model) == {"중식12", "중식001", "중식1", "공통잔존"}

    model.set_column_values("sequence", {
        ALL_SEQUENCES_EMPTY,
        sequence_pair_token(Meal.LUNCH.value, "12"),
    })
    assert model.rowCount() == 0


def test_saved_exact_filter_survives_refresh_without_new_value_broadening():
    roster = sequence_roster()
    model = RosterTableModel(roster)
    selected = frozenset((sequence_pair_token(Meal.LUNCH.value, "12"),))
    model.set_column_values("sequence", selected)

    target = roster.persons[4]
    updated = replace(
        roster,
        revision_id=uuid4(),
        assignments=(
            *roster.assignments,
            DeliveryAssignment(target.person_id, Meal.LUNCH, sequence="13"),
        ),
    )
    model.replace_roster(updated)

    assert model.selected_column_values("sequence") == selected
    assert names(model) == {"중식12"}
