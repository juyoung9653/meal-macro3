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
    }
    assert record.metadata.rule_versions[
        "person_profile_edit_policy_version"
    ] == 1
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
