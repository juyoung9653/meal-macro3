"""Immutable person-profile edit revision contract tests."""

from datetime import UTC, date, datetime
from uuid import UUID

import pytest

from src.domain import (
    PERSON_KIND,
    ChangeOperation,
    CustomFieldDefinition,
    CustomFieldType,
    Household,
    HouseholdMembership,
    Meal,
    MealEnrollment,
    Person,
    PersonProfile,
    RevisionActor,
    RevisionSource,
    RosterSnapshot,
    build_person_profile_revision,
)

CURRENT_REVISION_ID = UUID("85000000-0000-0000-0000-000000000001")
NEW_REVISION_ID = UUID("85000000-0000-0000-0000-000000000002")
PERSON_ID = UUID("85000000-0000-0000-0000-000000000003")
HOUSEHOLD_ID = UUID("85000000-0000-0000-0000-000000000004")
CUSTOM_FIELD_ID = UUID("85000000-0000-0000-0000-000000000005")
EFFECTIVE_ON = date(2026, 8, 26)
RECORDED_AT = datetime(2026, 8, 26, 12, 30, tzinfo=UTC)


def _roster() -> RosterSnapshot:
    return RosterSnapshot(
        CURRENT_REVISION_ID,
        persons=(
            Person(
                PERSON_ID,
                PersonProfile(
                    "홍길동",
                    birth_date=date(2015, 3, 2),
                    school_name="이전초",
                    road_address="성남대로 1",
                    mobile_phone="010-1234-5678",
                    custom_values={CUSTOM_FIELD_ID: "A구역"},
                ),
            ),
        ),
        households=(Household(HOUSEHOLD_ID),),
        memberships=(HouseholdMembership(PERSON_ID, HOUSEHOLD_ID),),
        enrollments=(MealEnrollment(PERSON_ID, Meal.LUNCH),),
        custom_fields=(
            CustomFieldDefinition(
                CUSTOM_FIELD_ID,
                "배송 구역",
                CustomFieldType.TEXT,
                0,
            ),
        ),
    )


def test_profile_edit_builds_one_ui_revision_and_records_cleared_fields():
    roster = _roster()
    updated = PersonProfile(
        "홍길동",
        birth_date=date(2015, 3, 2),
        school_name="새학교",
        road_address="성남대로 1",
        mobile_phone=None,
        custom_values={CUSTOM_FIELD_ID: "A구역"},
    )

    prepared = build_person_profile_revision(
        roster,
        PERSON_ID,
        updated,
        actor=RevisionActor("tester", "S-1-5-21-test"),
        recorded_at=RECORDED_AT,
        effective_on=EFFECTIVE_ON,
        revision_id=NEW_REVISION_ID,
    )

    assert prepared.snapshot.person(PERSON_ID).profile.school_name == "새학교"
    assert prepared.snapshot.person(PERSON_ID).profile.mobile_phone is None
    assert prepared.snapshot.person(PERSON_ID).profile.custom_values == {
        CUSTOM_FIELD_ID: "A구역"
    }
    assert prepared.snapshot.memberships == roster.memberships
    assert prepared.snapshot.enrollments == roster.enrollments
    assert prepared.changed_fields == ("school_name", "mobile_phone")
    assert prepared.cleared_fields == ("mobile_phone",)

    record = prepared.record
    assert record.metadata.parent_revision_id == CURRENT_REVISION_ID
    assert record.metadata.source is RevisionSource.UI
    assert record.metadata.effective_on == EFFECTIVE_ON
    assert record.metadata.decisions["person_profile_edit"] == {
        "person_id": str(PERSON_ID),
        "changed_fields": ("school_name", "mobile_phone"),
        "cleared_fields": ("mobile_phone",),
        "changed_custom_fields": (),
        "cleared_custom_fields": (),
    }
    assert record.metadata.rule_versions["person_profile_edit_policy_version"] == 1
    assert record.import_batch is None
    assert len(record.changes) == 1
    assert record.changes[0].entity_kind == PERSON_KIND
    assert record.changes[0].entity_id == PERSON_ID
    assert record.changes[0].operation is ChangeOperation.UPDATE


def test_profile_edit_rejects_no_change_and_unknown_person():
    roster = _roster()
    arguments = {
        "actor": RevisionActor("tester", "S-1-5-21-test"),
        "recorded_at": RECORDED_AT,
        "effective_on": EFFECTIVE_ON,
        "revision_id": NEW_REVISION_ID,
    }

    with pytest.raises(ValueError, match="변경된 사람 정보"):
        build_person_profile_revision(
            roster,
            PERSON_ID,
            roster.person(PERSON_ID).profile,
            **arguments,
        )
    with pytest.raises(KeyError, match="snapshot에 없는 사람"):
        build_person_profile_revision(
            roster,
            UUID("85000000-0000-0000-0000-000000000099"),
            roster.person(PERSON_ID).profile,
            **arguments,
        )


def test_profile_edit_persists_a_custom_value_only_change():
    roster = _roster()
    original = roster.person(PERSON_ID).profile
    updated = PersonProfile(
        name=original.name,
        birth_date=original.birth_date,
        school_name=original.school_name,
        road_address=original.road_address,
        mobile_phone=original.mobile_phone,
        custom_values={CUSTOM_FIELD_ID: "B구역"},
    )

    prepared = build_person_profile_revision(
        roster,
        PERSON_ID,
        updated,
        actor=RevisionActor("tester", "S-1-5-21-test"),
        recorded_at=RECORDED_AT,
        effective_on=EFFECTIVE_ON,
        revision_id=NEW_REVISION_ID,
    )

    assert prepared.changed_fields == ()
    assert prepared.changed_custom_fields == (str(CUSTOM_FIELD_ID),)
    assert (
        prepared.snapshot.person(PERSON_ID).profile.custom_values[CUSTOM_FIELD_ID]
        == "B구역"
    )


@pytest.mark.parametrize("whole_household", [False, True])
@pytest.mark.parametrize("number", ["010-9999-9999", None])
def test_phone_scope_changes_only_mobile_of_household_members(whole_household, number):
    from dataclasses import replace
    from uuid import uuid4

    roster = _roster()
    sibling = Person(uuid4(), PersonProfile("가족", school_name="가족학교", mobile_phone="010-2222-2222"))
    outsider = Person(uuid4(), PersonProfile("다른가구", mobile_phone=roster.person(PERSON_ID).profile.mobile_phone))
    roster = replace(roster, persons=(*roster.persons, sibling, outsider),
                     memberships=(*roster.memberships, HouseholdMembership(sibling.person_id, HOUSEHOLD_ID)))
    updated = replace(roster.person(PERSON_ID).profile, mobile_phone=number, school_name="새학교")
    result = build_person_profile_revision(
        roster, PERSON_ID, updated, actor=RevisionActor("tester", "test"),
        recorded_at=RECORDED_AT, effective_on=EFFECTIVE_ON, revision_id=NEW_REVISION_ID,
        household_mobile_phone=whole_household,
    )
    assert result.snapshot.person(PERSON_ID).profile == updated
    assert result.snapshot.person(sibling.person_id).profile == replace(
        sibling.profile, mobile_phone=number if whole_household else sibling.profile.mobile_phone)
    assert result.snapshot.person(outsider.person_id) == outsider
    assert result.snapshot.memberships == roster.memberships
    assert len(result.record.changes) == (2 if whole_household else 1)


def test_phone_can_copy_unchanged_subject_number_to_family():
    from dataclasses import replace
    from uuid import uuid4

    roster = _roster()
    sibling = Person(uuid4(), PersonProfile("가족", mobile_phone="010-2222-2222"))
    roster = replace(roster, persons=(*roster.persons, sibling),
                     memberships=(*roster.memberships, HouseholdMembership(sibling.person_id, HOUSEHOLD_ID)))
    result = build_person_profile_revision(
        roster, PERSON_ID, roster.person(PERSON_ID).profile, actor=RevisionActor("tester", "test"),
        recorded_at=RECORDED_AT, effective_on=EFFECTIVE_ON, revision_id=NEW_REVISION_ID,
        household_mobile_phone=True,
    )
    assert len(result.record.changes) == 1
    assert result.snapshot.person(sibling.person_id).profile.mobile_phone == roster.person(PERSON_ID).profile.mobile_phone


@pytest.mark.parametrize("day, expected_count", [(date(2026, 8, 25), 1), (date(2026, 8, 26), 2)])
def test_household_phone_respects_membership_effective_date(day, expected_count):
    from dataclasses import replace
    from uuid import uuid4

    from src.domain.roster import InclusiveDateRange

    roster = _roster()
    sibling = Person(uuid4(), PersonProfile("가족", mobile_phone="010-2222-2222"))
    roster = replace(roster, persons=(*roster.persons, sibling),
                     memberships=(*roster.memberships, HouseholdMembership(
                         sibling.person_id, HOUSEHOLD_ID, InclusiveDateRange(date(2026, 8, 26), None))))
    result = build_person_profile_revision(
        roster, PERSON_ID, replace(roster.person(PERSON_ID).profile, mobile_phone="010-9999-9999"),
        actor=RevisionActor("tester", "test"), recorded_at=RECORDED_AT,
        effective_on=day, revision_id=NEW_REVISION_ID, household_mobile_phone=True,
    )
    assert len(result.record.changes) == expected_count
