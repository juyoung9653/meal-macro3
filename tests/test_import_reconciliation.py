"""Versioned import profile and identity staging tests."""

from datetime import date
from uuid import UUID

import pytest

from src.domain import (
    EntityRef,
    HistoricalSnapshot,
    Household,
    HouseholdMembership,
    Meal,
    MealEnrollment,
    Person,
    PersonProfile,
    RosterSnapshot,
)
from src.importing import (
    DateNormalization,
    IdentityResolution,
    ImportMappingProfile,
    ObservedDeliveryAssignment,
    PersonObservation,
    PhoneNormalization,
    ProfileField,
    ReconciliationDisposition,
    ReconciliationDecision,
    ReviewReason,
    SourceReference,
    StoredImportProfile,
    apply_reconciliation,
    import_profile_audit_change,
    import_profiles_from_snapshot,
    stage_reconciliation,
)


REVISION_ID = UUID("70000000-0000-0000-0000-000000000001")
PERSON_ID = UUID("70000000-0000-0000-0000-000000000002")
HOUSEHOLD_ID = UUID("70000000-0000-0000-0000-000000000003")
PROFILE_ID = UUID("70000000-0000-0000-0000-000000000004")
OBSERVATION_ID = UUID("70000000-0000-0000-0000-000000000005")
SECOND_OBSERVATION_ID = UUID("70000000-0000-0000-0000-000000000006")
NEW_REVISION_ID = UUID("70000000-0000-0000-0000-000000000007")
NEW_PERSON_ID = UUID("70000000-0000-0000-0000-000000000008")
EFFECTIVE_ON = date(2026, 8, 17)


def _roster() -> RosterSnapshot:
    return RosterSnapshot(
        REVISION_ID,
        persons=(
            Person(
                PERSON_ID,
                PersonProfile(
                    "홍길동",
                    birth_date=date(2015, 3, 2),
                    school_name="이전초",
                    mobile_phone="010-1234-5678",
                    provider="분당구",
                ),
            ),
        ),
        households=(Household(HOUSEHOLD_ID),),
        memberships=(HouseholdMembership(PERSON_ID, HOUSEHOLD_ID),),
        enrollments=(MealEnrollment(PERSON_ID, Meal.BREAKFAST),),
    )


def _observation(
    observation_id: UUID = OBSERVATION_ID,
    *,
    name: str = "홍길동",
    birth_date: date | None = date(2015, 3, 2),
    mobile_phone: str | None = "01012345678",
) -> PersonObservation:
    return PersonObservation(
        observation_id,
        SourceReference("latest.xlsx", "명단", (2,)),
        {
            ProfileField.NAME: name,
            ProfileField.BIRTH_DATE: birth_date,
            ProfileField.SCHOOL_NAME: "새학교",
            ProfileField.MOBILE_PHONE: mobile_phone,
        },
        (Meal.LUNCH,),
    )


def test_mapping_profile_is_versioned_and_manifest_is_storage_safe():
    profile = ImportMappingProfile(
        PROFILE_ID,
        3,
        "분당 조사표",
        "survey-xlsx",
        {
            ProfileField.NAME: "성명",
            ProfileField.BIRTH_DATE: "생년월일",
            ProfileField.SCHOOL_NAME: "학교명",
        },
        combined_meals_column="급식 지원",
        driver_column="담당 기사",
    )

    assert profile.manifest() == {
        "profile_id": str(PROFILE_ID),
        "version": 3,
        "name": "분당 조사표",
        "source_kind": "survey-xlsx",
        "profile_columns": {
            "birth_date": "생년월일",
            "name": "성명",
            "school_name": "학교명",
        },
        "combined_meals_column": "급식 지원",
        "meal_flag_columns": {},
        "driver_column": "담당 기사",
        "sequence_column": None,
        "source_reference_column": None,
        "custom_field_columns": {},
        "header_row": 1,
        "data_start_row": 2,
        "fallback_profile_columns": {},
        "meal_true_values": ["1"],
        "meal_false_values": ["", "0"],
        "date_normalization": "auto",
        "phone_normalization": "korean",
        "ignored_columns": [],
        "provider_value": None,
    }


def test_mapping_profile_round_trips_extended_layout_rules():
    profile = ImportMappingProfile(
        PROFILE_ID,
        1,
        "기존 명단",
        "roster-xlsx",
        {
            ProfileField.NAME: "G",
            ProfileField.BIRTH_DATE: "H",
            ProfileField.ROAD_ADDRESS: "N",
        },
        meal_flag_columns={Meal.BREAKFAST: "S", Meal.LUNCH: "V"},
        header_row=2,
        data_start_row=3,
        fallback_profile_columns={ProfileField.ROAD_ADDRESS: ("M",)},
        meal_true_values=("1", "Y"),
        meal_false_values=("", "0", "N"),
        date_normalization=DateNormalization.YYYYMMDD,
        phone_normalization=PhoneNormalization.DIGITS,
        ignored_columns=("U", "AI"),
        provider_value="분당구 급식센터",
    )

    restored = ImportMappingProfile.from_manifest(profile.manifest())

    assert restored == profile
    assert restored.fallback_profile_columns[ProfileField.ROAD_ADDRESS] == ("M",)
    assert restored.ignored_columns == ("U", "AI")
    assert restored.provider_value == "분당구 급식센터"


def test_mapping_profile_rejects_provider_column_and_manual_value_together():
    with pytest.raises(ValueError, match="동시에"):
        ImportMappingProfile(
            PROFILE_ID,
            1,
            "잘못된 제공기관 설정",
            "roster-xlsx",
            {
                ProfileField.NAME: "A",
                ProfileField.BIRTH_DATE: "B",
                ProfileField.PROVIDER: "C",
            },
            combined_meals_column="D",
            provider_value="분당구 급식센터",
        )


def test_import_profile_is_an_immutable_versioned_ledger_entity():
    profile = ImportMappingProfile(
        PROFILE_ID,
        1,
        "기존 명단",
        "roster-xlsx",
        {
            ProfileField.NAME: "G",
            ProfileField.BIRTH_DATE: "H",
        },
        combined_meals_column="S",
    )
    stored = StoredImportProfile(profile, "a" * 64)
    change = import_profile_audit_change(None, stored)
    snapshot = HistoricalSnapshot(
        REVISION_ID,
        {EntityRef(change.entity_kind, change.entity_id): change.after},
    )

    assert import_profiles_from_snapshot(snapshot) == (stored,)

    next_profile = ImportMappingProfile.from_manifest(
        {**profile.manifest(), "version": 2, "data_start_row": 3}
    )
    updated = StoredImportProfile(next_profile, "b" * 64)
    update = import_profile_audit_change(stored, updated)
    assert update.before == change.after
    assert update.after["profile"]["version"] == 2


def test_mapping_profile_rejects_reusing_one_source_column():
    with pytest.raises(ValueError, match="둘 이상의 import 역할"):
        ImportMappingProfile(
            PROFILE_ID,
            1,
            "잘못된 profile",
            "survey-xlsx",
            {
                ProfileField.NAME: "A열",
                ProfileField.BIRTH_DATE: "A열",
            },
            combined_meals_column="식종",
        )


def test_exact_name_and_full_birth_match_is_staged_without_identity_prompt():
    plan = stage_reconciliation(
        _roster(),
        (_observation(),),
        effective_on=EFFECTIVE_ON,
    )

    staged = plan.staged_people[0]
    assert staged.disposition is ReconciliationDisposition.MATCHED
    assert staged.matched_person_id == PERSON_ID
    assert tuple(item.field for item in staged.profile_differences) == (
        ProfileField.SCHOOL_NAME,
    )
    assert staged.meals_added == (Meal.LUNCH,)
    assert staged.meals_removed == (Meal.BREAKFAST,)
    assert staged.household_candidate_ids == (HOUSEHOLD_ID,)
    assert not plan.requires_decision


def test_identity_and_profile_comparison_ignore_whitespace():
    observation = PersonObservation(
        OBSERVATION_ID,
        SourceReference("latest.xlsx", "명단", (2,)),
        {
            ProfileField.NAME: "홍 길 동",
            ProfileField.BIRTH_DATE: date(2015, 3, 2),
            ProfileField.SCHOOL_NAME: "이 전 초",
            ProfileField.MOBILE_PHONE: "010 1234 5678",
        },
        (Meal.BREAKFAST,),
    )

    staged = stage_reconciliation(
        _roster(),
        (observation,),
        effective_on=EFFECTIVE_ON,
    ).staged_people[0]

    assert staged.disposition is ReconciliationDisposition.MATCHED
    assert staged.matched_person_id == PERSON_ID
    assert staged.profile_differences == ()


def test_same_name_different_birth_requires_review_instead_of_false_new():
    observation = _observation(birth_date=date(2016, 3, 2))

    staged = stage_reconciliation(
        _roster(),
        (observation,),
        effective_on=EFFECTIVE_ON,
    ).staged_people[0]

    assert staged.disposition is ReconciliationDisposition.REVIEW
    assert staged.candidate_person_ids == (PERSON_ID,)
    assert staged.review_reasons == (ReviewReason.NAME_COLLISION,)


def test_missing_birth_and_duplicate_source_identity_are_never_auto_matched():
    missing_birth = _observation(birth_date=None)
    duplicate_first = _observation()
    duplicate_second = _observation(SECOND_OBSERVATION_ID)

    missing = stage_reconciliation(
        _roster(),
        (missing_birth,),
        effective_on=EFFECTIVE_ON,
    ).staged_people[0]
    duplicates = stage_reconciliation(
        _roster(),
        (duplicate_first, duplicate_second),
        effective_on=EFFECTIVE_ON,
    ).staged_people

    assert missing.disposition is ReconciliationDisposition.REVIEW
    assert missing.review_reasons == (
        ReviewReason.MISSING_BIRTH_DATE,
        ReviewReason.NAME_COLLISION,
    )
    assert all(
        item.review_reasons == (ReviewReason.DUPLICATE_SOURCE_IDENTITY,)
        for item in duplicates
    )


def test_initial_import_defaults_missing_birth_date_to_new_person():
    observation = _observation(birth_date=None)

    staged = stage_reconciliation(
        None,
        (observation,),
        effective_on=EFFECTIVE_ON,
    ).staged_people[0]

    assert staged.disposition is ReconciliationDisposition.NEW
    assert staged.review_reasons == ()
    assert staged.meals_added == observation.meals


def test_phone_only_proposes_household_and_never_person_identity():
    observation = _observation(
        name="새아동",
        birth_date=date(2017, 5, 4),
        mobile_phone="010-1234-5678",
    )

    staged = stage_reconciliation(
        _roster(),
        (observation,),
        effective_on=EFFECTIVE_ON,
    ).staged_people[0]

    assert staged.disposition is ReconciliationDisposition.NEW
    assert staged.candidate_person_ids == ()
    assert staged.household_candidate_ids == (HOUSEHOLD_ID,)


def test_observation_merges_only_mapped_fields_into_existing_profile():
    observation = _observation()

    merged = observation.merged_profile(_roster().person(PERSON_ID).profile)

    assert merged.school_name == "새학교"
    assert merged.provider == "분당구"


def test_mapped_blanks_preserve_existing_profile_and_are_audited():
    roster = _roster()
    observation = PersonObservation(
        OBSERVATION_ID,
        SourceReference("latest.xlsx", "명단", (2,)),
        {
            ProfileField.NAME: "홍길동",
            ProfileField.BIRTH_DATE: date(2015, 3, 2),
            ProfileField.SCHOOL_NAME: None,
            ProfileField.PROVIDER: None,
            ProfileField.ROAD_ADDRESS: None,
            ProfileField.MOBILE_PHONE: None,
        },
        (Meal.LUNCH,),
    )
    plan = stage_reconciliation(
        roster,
        (observation,),
        effective_on=EFFECTIVE_ON,
    )

    assert plan.staged_people[0].profile_differences == ()

    result = apply_reconciliation(
        roster,
        plan,
        new_revision_id=NEW_REVISION_ID,
    )

    profile = result.snapshot.person(PERSON_ID).profile
    assert profile.school_name == "이전초"
    assert profile.provider == "분당구"
    assert profile.road_address is None
    assert profile.mobile_phone == "010-1234-5678"
    assert result.decision_manifest["profile_update_policy"] == {
        "version": 1,
        "mapped_blank_values": "preserve_existing",
    }
    assert result.decision_manifest["preserved_blank_profile_fields"] == {
        str(OBSERVATION_ID): [
            "school_name",
            "provider",
            "mobile_phone",
        ]
    }


def test_mapped_blanks_remain_blank_for_a_new_person():
    observation = PersonObservation(
        OBSERVATION_ID,
        SourceReference("latest.xlsx", "명단", (2,)),
        {
            ProfileField.NAME: "새아동",
            ProfileField.BIRTH_DATE: date(2017, 5, 4),
            ProfileField.SCHOOL_NAME: None,
            ProfileField.MOBILE_PHONE: None,
        },
        (Meal.BREAKFAST,),
    )
    plan = stage_reconciliation(
        None,
        (observation,),
        effective_on=EFFECTIVE_ON,
    )

    result = apply_reconciliation(
        None,
        plan,
        new_revision_id=NEW_REVISION_ID,
        decisions={
            OBSERVATION_ID: ReconciliationDecision(
                OBSERVATION_ID,
                IdentityResolution.CREATE_NEW,
                NEW_PERSON_ID,
            )
        },
    )

    profile = result.snapshot.person(NEW_PERSON_ID).profile
    assert profile.school_name is None
    assert profile.mobile_phone is None
    assert result.decision_manifest["preserved_blank_profile_fields"] == {}


def test_confirmed_exact_match_applies_profile_and_effective_meal_periods():
    roster = _roster()
    plan = stage_reconciliation(
        roster,
        (_observation(),),
        effective_on=EFFECTIVE_ON,
    )

    result = apply_reconciliation(
        roster,
        plan,
        new_revision_id=NEW_REVISION_ID,
    )

    updated = result.snapshot
    assert updated.person(PERSON_ID).profile.school_name == "새학교"
    assert updated.person(PERSON_ID).profile.provider == "분당구"
    assert updated.meals_for(PERSON_ID, on=date(2026, 8, 16)) == (
        Meal.BREAKFAST,
    )
    assert updated.meals_for(PERSON_ID, on=EFFECTIVE_ON) == (Meal.LUNCH,)
    assert updated.memberships == roster.memberships
    assert result.decision_manifest["applied"] == {
        str(OBSERVATION_ID): str(PERSON_ID)
    }


def test_new_or_review_identity_requires_explicit_decision():
    roster = _roster()
    observation = _observation(
        name="새아동",
        birth_date=date(2017, 5, 4),
    )
    plan = stage_reconciliation(
        roster,
        (observation,),
        effective_on=EFFECTIVE_ON,
    )

    with pytest.raises(ValueError, match="identity 결정"):
        apply_reconciliation(
            roster,
            plan,
            new_revision_id=NEW_REVISION_ID,
        )


def test_create_new_person_keeps_phone_as_household_candidate_only():
    roster = _roster()
    observation = PersonObservation(
        OBSERVATION_ID,
        SourceReference("latest.xlsx", "명단", (2,)),
        {
            ProfileField.NAME: "새아동",
            ProfileField.BIRTH_DATE: date(2017, 5, 4),
            ProfileField.MOBILE_PHONE: "010-1234-5678",
        },
        (Meal.BREAKFAST,),
        (ObservedDeliveryAssignment(Meal.BREAKFAST, "기사B", "002"),),
    )
    plan = stage_reconciliation(
        roster,
        (observation,),
        effective_on=EFFECTIVE_ON,
    )
    decision = ReconciliationDecision(
        OBSERVATION_ID,
        IdentityResolution.CREATE_NEW,
        NEW_PERSON_ID,
    )

    result = apply_reconciliation(
        roster,
        plan,
        new_revision_id=NEW_REVISION_ID,
        decisions={OBSERVATION_ID: decision},
    )

    assert result.snapshot.person(NEW_PERSON_ID).profile.name == "새아동"
    assert result.snapshot.meals_for(NEW_PERSON_ID, on=EFFECTIVE_ON) == (
        Meal.BREAKFAST,
    )
    assert result.snapshot.resolve_assignment(
        NEW_PERSON_ID,
        Meal.BREAKFAST,
    ).driver == "기사B"
    assert all(
        membership.person_id != NEW_PERSON_ID
        for membership in result.snapshot.memberships
    )


def test_initial_import_uses_no_fake_base_revision_and_creates_first_snapshot():
    observation = _observation(
        name="첫아동",
        birth_date=date(2018, 6, 7),
        mobile_phone="010-5555-7777",
    )
    plan = stage_reconciliation(
        None,
        (observation,),
        effective_on=EFFECTIVE_ON,
    )

    assert plan.roster_revision_id is None
    staged = plan.staged_people[0]
    assert staged.disposition is ReconciliationDisposition.NEW
    assert staged.candidate_person_ids == ()
    assert staged.household_candidate_ids == ()

    result = apply_reconciliation(
        None,
        plan,
        new_revision_id=NEW_REVISION_ID,
        decisions={
            OBSERVATION_ID: ReconciliationDecision(
                OBSERVATION_ID,
                IdentityResolution.CREATE_NEW,
                NEW_PERSON_ID,
            )
        },
    )

    assert result.snapshot.revision_id == NEW_REVISION_ID
    assert result.snapshot.person(NEW_PERSON_ID).profile.name == "첫아동"
    assert result.snapshot.meals_for(NEW_PERSON_ID, on=EFFECTIVE_ON) == (
        Meal.LUNCH,
    )
