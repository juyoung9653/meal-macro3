"""Household proposals never merge people and require every selected value."""

from datetime import date
from uuid import UUID

import pytest

from src.domain import (
    EntityRef,
    HistoricalSnapshot,
    Household,
    HouseholdMembership,
    InclusiveDateRange,
    Meal,
    Person,
    PersonProfile,
    RosterSnapshot,
)
from src.importing import (
    HOUSEHOLD_MATCHING_RULE_KIND,
    HouseholdGroupingDecision,
    HouseholdMatchCriterion,
    HouseholdMatchingRule,
    IdentityResolution,
    PersonObservation,
    ProfileField,
    ReconciliationDecision,
    SourceReference,
    StoredHouseholdMatchingRule,
    apply_household_grouping,
    apply_reconciliation,
    household_matching_rule_audit_change,
    household_matching_rules_from_snapshot,
    stage_household_grouping,
    stage_reconciliation,
)


EFFECTIVE_ON = date(2026, 8, 26)
RULE_ID = UUID("83000000-0000-0000-0000-000000000001")
REVISION_ID = UUID("83000000-0000-0000-0000-000000000002")
OBSERVATION_1 = UUID("83000000-0000-0000-0000-000000000011")
OBSERVATION_2 = UUID("83000000-0000-0000-0000-000000000012")
PERSON_1 = UUID("83000000-0000-0000-0000-000000000021")
PERSON_2 = UUID("83000000-0000-0000-0000-000000000022")
EXISTING_REVISION = UUID("83000000-0000-0000-0000-000000000071")
EXISTING_PERSON_1 = UUID("83000000-0000-0000-0000-000000000072")
EXISTING_PERSON_2 = UUID("83000000-0000-0000-0000-000000000073")
EXISTING_HOUSEHOLD_1 = UUID("83000000-0000-0000-0000-000000000074")
EXISTING_HOUSEHOLD_2 = UUID("83000000-0000-0000-0000-000000000075")


def _observation(
    observation_id: UUID,
    row: int,
    name: str,
    *,
    mobile_phone: str | None = None,
    household_head_name: str | None = None,
    road_address: str | None = None,
    detail_address: str | None = None,
    telephone: str | None = None,
) -> PersonObservation:
    return PersonObservation(
        observation_id,
        SourceReference("synthetic.xlsx", "명단", (row,)),
        {
            ProfileField.NAME: name,
            ProfileField.BIRTH_DATE: date(2015, 3, row),
            ProfileField.MOBILE_PHONE: mobile_phone,
            ProfileField.HOUSEHOLD_HEAD_NAME: household_head_name,
            ProfileField.ROAD_ADDRESS: road_address,
            ProfileField.DETAIL_ADDRESS: detail_address,
            ProfileField.TELEPHONE: telephone,
        },
        (Meal.LUNCH,),
    )


def _uuid_factory(*values: str):
    iterator = iter(UUID(value) for value in values)
    return lambda: next(iterator)


def _existing_roster(
    *,
    duplicate_phone_household: bool = False,
    first_membership_period: InclusiveDateRange | None = None,
) -> RosterSnapshot:
    persons = [
        Person(
            EXISTING_PERSON_1,
            PersonProfile(
                "기존 첫째",
                birth_date=date(2012, 1, 2),
                mobile_phone="010-1234-5678",
                household_head_name="홍 보호자",
                road_address="성남대로 1",
                detail_address="101호",
            ),
        )
    ]
    households = [Household(EXISTING_HOUSEHOLD_1)]
    memberships = [
        HouseholdMembership(
            EXISTING_PERSON_1,
            EXISTING_HOUSEHOLD_1,
            first_membership_period or InclusiveDateRange(),
        )
    ]
    if duplicate_phone_household:
        persons.append(
            Person(
                EXISTING_PERSON_2,
                PersonProfile(
                    "다른 가구",
                    birth_date=date(2011, 2, 3),
                    mobile_phone="01012345678",
                    household_head_name="다른 보호자",
                ),
            )
        )
        households.append(Household(EXISTING_HOUSEHOLD_2))
        memberships.append(
            HouseholdMembership(EXISTING_PERSON_2, EXISTING_HOUSEHOLD_2)
        )
    return RosterSnapshot(
        EXISTING_REVISION,
        persons=tuple(persons),
        households=tuple(households),
        memberships=tuple(memberships),
    )


def test_default_mobile_rule_normalizes_phone_and_proposes_one_household():
    observations = (
        _observation(OBSERVATION_1, 2, "첫째", mobile_phone="010-1234-5678"),
        _observation(OBSERVATION_2, 3, "둘째", mobile_phone="01012345678"),
    )
    rule = HouseholdMatchingRule(RULE_ID, 1)
    shared = "83000000-0000-0000-0000-000000000031"

    plan = stage_household_grouping(
        observations,
        rule,
        effective_on=EFFECTIVE_ON,
        household_id_factory=_uuid_factory(
            shared,
            "83000000-0000-0000-0000-000000000032",
            "83000000-0000-0000-0000-000000000033",
        ),
    )

    assert rule.criteria == (HouseholdMatchCriterion.MOBILE_PHONE,)
    assert len(plan.proposals) == 1
    assert plan.proposals[0].observation_ids == (OBSERVATION_1, OBSERVATION_2)
    assert set(plan.default_household_ids.values()) == {UUID(shared)}


def test_blank_selected_value_never_groups_people():
    observations = (
        _observation(OBSERVATION_1, 2, "첫째", mobile_phone=None),
        _observation(OBSERVATION_2, 3, "둘째", mobile_phone=None),
    )

    plan = stage_household_grouping(
        observations,
        HouseholdMatchingRule(RULE_ID, 1),
        effective_on=EFFECTIVE_ON,
        household_id_factory=_uuid_factory(
            "83000000-0000-0000-0000-000000000041",
            "83000000-0000-0000-0000-000000000042",
        ),
    )

    assert plan.proposals == ()
    assert len(set(plan.default_household_ids.values())) == 2


def test_multiple_selected_criteria_use_and_not_or():
    rule = HouseholdMatchingRule(
        RULE_ID,
        1,
        (
            HouseholdMatchCriterion.HOUSEHOLD_HEAD_NAME,
            HouseholdMatchCriterion.MOBILE_PHONE,
        ),
    )
    observations = (
        _observation(
            OBSERVATION_1,
            2,
            "첫째",
            mobile_phone="010-1234-5678",
            household_head_name="홍 보호자",
        ),
        _observation(
            OBSERVATION_2,
            3,
            "둘째",
            mobile_phone="01012345678",
            household_head_name="김 보호자",
        ),
    )

    different_head = stage_household_grouping(
        observations,
        rule,
        effective_on=EFFECTIVE_ON,
        household_id_factory=_uuid_factory(
            "83000000-0000-0000-0000-000000000051",
            "83000000-0000-0000-0000-000000000052",
        ),
    )
    same_head_observations = (
        observations[0],
        _observation(
            OBSERVATION_2,
            3,
            "둘째",
            mobile_phone="01012345678",
            household_head_name="  홍   보호자 ",
        ),
    )
    same_head = stage_household_grouping(
        same_head_observations,
        rule,
        effective_on=EFFECTIVE_ON,
        household_id_factory=_uuid_factory(
            "83000000-0000-0000-0000-000000000053",
            "83000000-0000-0000-0000-000000000054",
            "83000000-0000-0000-0000-000000000055",
        ),
    )

    assert rule.criteria == (
        HouseholdMatchCriterion.MOBILE_PHONE,
        HouseholdMatchCriterion.HOUSEHOLD_HEAD_NAME,
    )
    assert different_head.proposals == ()
    assert len(same_head.proposals) == 1


def test_applying_group_keeps_people_distinct_and_split_is_reversible_decision():
    observations = (
        _observation(OBSERVATION_1, 2, "첫째", mobile_phone="010-1234-5678"),
        _observation(OBSERVATION_2, 3, "둘째", mobile_phone="01012345678"),
    )
    reconciliation_plan = stage_reconciliation(
        None,
        observations,
        effective_on=EFFECTIVE_ON,
    )
    reconciliation = apply_reconciliation(
        None,
        reconciliation_plan,
        new_revision_id=REVISION_ID,
        decisions={
            OBSERVATION_1: ReconciliationDecision(
                OBSERVATION_1,
                IdentityResolution.CREATE_NEW,
                PERSON_1,
            ),
            OBSERVATION_2: ReconciliationDecision(
                OBSERVATION_2,
                IdentityResolution.CREATE_NEW,
                PERSON_2,
            ),
        },
    )
    grouping_plan = stage_household_grouping(
        observations,
        HouseholdMatchingRule(RULE_ID, 1),
        effective_on=EFFECTIVE_ON,
        household_id_factory=_uuid_factory(
            "83000000-0000-0000-0000-000000000061",
            "83000000-0000-0000-0000-000000000062",
            "83000000-0000-0000-0000-000000000063",
        ),
    )

    grouped = apply_household_grouping(reconciliation, grouping_plan)

    assert {person.person_id for person in grouped.snapshot.persons} == {
        PERSON_1,
        PERSON_2,
    }
    assert len(grouped.snapshot.households) == 1
    household_id = grouped.snapshot.households[0].household_id
    assert {person.person_id for person in grouped.snapshot.household_members(
        household_id,
        on=EFFECTIVE_ON,
    )} == {PERSON_1, PERSON_2}
    assert grouped.snapshot.household_members(
        household_id,
        on=date(2026, 8, 25),
    ) == ()

    split_household_id = grouping_plan.separate_household_ids[OBSERVATION_2]
    split = apply_household_grouping(
        reconciliation,
        grouping_plan,
        decisions={
            OBSERVATION_2: HouseholdGroupingDecision(
                OBSERVATION_2,
                split_household_id,
            )
        },
    )

    assert len(split.snapshot.households) == 2
    memberships = {item.person_id: item.household_id for item in split.snapshot.memberships}
    assert memberships[PERSON_1] != memberships[PERSON_2]
    assert split.decision_manifest["households"][str(OBSERVATION_2)][
        "kept_suggestion"
    ] is False


def test_later_new_person_defaults_to_one_matching_existing_household():
    roster = _existing_roster()
    observation = _observation(
        OBSERVATION_1,
        2,
        "나중에 추가된 아동",
        mobile_phone="01012345678",
    )
    grouping_plan = stage_household_grouping(
        (observation,),
        HouseholdMatchingRule(RULE_ID, 1),
        effective_on=EFFECTIVE_ON,
        roster=roster,
        household_id_factory=_uuid_factory(
            "83000000-0000-0000-0000-000000000081",
        ),
    )

    existing = grouping_plan.existing_proposal_for(OBSERVATION_1)
    assert existing is not None
    assert existing.candidate_household_ids == (EXISTING_HOUSEHOLD_1,)
    assert grouping_plan.default_household_ids[OBSERVATION_1] == (
        EXISTING_HOUSEHOLD_1
    )

    reconciliation_plan = stage_reconciliation(
        roster,
        (observation,),
        effective_on=EFFECTIVE_ON,
    )
    reconciliation = apply_reconciliation(
        roster,
        reconciliation_plan,
        new_revision_id=REVISION_ID,
        decisions={
            OBSERVATION_1: ReconciliationDecision(
                OBSERVATION_1,
                IdentityResolution.CREATE_NEW,
                PERSON_1,
            )
        },
    )
    joined = apply_household_grouping(
        reconciliation,
        grouping_plan,
        previous_roster=roster,
    )

    assert len(joined.snapshot.households) == 1
    assert {person.person_id for person in joined.snapshot.household_members(
        EXISTING_HOUSEHOLD_1,
        on=EFFECTIVE_ON,
    )} == {EXISTING_PERSON_1, PERSON_1}
    assert joined.decision_manifest["households"][str(OBSERVATION_1)][
        "selection_kind"
    ] == "existing_household"

    separate_id = grouping_plan.separate_household_ids[OBSERVATION_1]
    separated = apply_household_grouping(
        reconciliation,
        grouping_plan,
        decisions={
            OBSERVATION_1: HouseholdGroupingDecision(
                OBSERVATION_1,
                separate_id,
            )
        },
        previous_roster=roster,
    )
    assert len(separated.snapshot.households) == 2
    assert separated.snapshot.household_members(
        separate_id,
        on=EFFECTIVE_ON,
    )[0].person_id == PERSON_1


def test_existing_household_match_also_requires_every_selected_criterion():
    roster = _existing_roster()
    rule = HouseholdMatchingRule(
        RULE_ID,
        1,
        (
            HouseholdMatchCriterion.MOBILE_PHONE,
            HouseholdMatchCriterion.HOUSEHOLD_HEAD_NAME,
        ),
    )
    different_head = _observation(
        OBSERVATION_1,
        2,
        "다른 세대",
        mobile_phone="01012345678",
        household_head_name="김 보호자",
    )
    same_head = _observation(
        OBSERVATION_2,
        3,
        "같은 세대",
        mobile_phone="01012345678",
        household_head_name="  홍 보호자 ",
    )

    plan = stage_household_grouping(
        (different_head, same_head),
        rule,
        effective_on=EFFECTIVE_ON,
        roster=roster,
        household_id_factory=_uuid_factory(
            "83000000-0000-0000-0000-000000000082",
            "83000000-0000-0000-0000-000000000083",
        ),
    )

    assert plan.existing_proposal_for(OBSERVATION_1) is None
    assert plan.existing_proposal_for(OBSERVATION_2).candidate_household_ids == (
        EXISTING_HOUSEHOLD_1,
    )


def test_multiple_matching_existing_households_require_explicit_choice():
    roster = _existing_roster(duplicate_phone_household=True)
    observation = _observation(
        OBSERVATION_1,
        2,
        "후보가 둘인 아동",
        mobile_phone="01012345678",
    )
    grouping_plan = stage_household_grouping(
        (observation,),
        HouseholdMatchingRule(RULE_ID, 1),
        effective_on=EFFECTIVE_ON,
        roster=roster,
        household_id_factory=_uuid_factory(
            "83000000-0000-0000-0000-000000000084",
        ),
    )
    existing = grouping_plan.existing_proposal_for(OBSERVATION_1)
    assert existing.candidate_household_ids == (
        EXISTING_HOUSEHOLD_1,
        EXISTING_HOUSEHOLD_2,
    )
    assert grouping_plan.required_decision_ids == (OBSERVATION_1,)

    reconciliation = apply_reconciliation(
        roster,
        stage_reconciliation(roster, (observation,), effective_on=EFFECTIVE_ON),
        new_revision_id=REVISION_ID,
        decisions={
            OBSERVATION_1: ReconciliationDecision(
                OBSERVATION_1,
                IdentityResolution.CREATE_NEW,
                PERSON_1,
            )
        },
    )
    with pytest.raises(ValueError, match="가구 선택"):
        apply_household_grouping(
            reconciliation,
            grouping_plan,
            previous_roster=roster,
        )

    chosen = apply_household_grouping(
        reconciliation,
        grouping_plan,
        decisions={
            OBSERVATION_1: HouseholdGroupingDecision(
                OBSERVATION_1,
                EXISTING_HOUSEHOLD_2,
            )
        },
        previous_roster=roster,
    )
    membership = next(
        item for item in chosen.snapshot.memberships if item.person_id == PERSON_1
    )
    assert membership.household_id == EXISTING_HOUSEHOLD_2


def test_household_membership_inactive_on_import_date_is_not_a_candidate():
    roster = _existing_roster(
        first_membership_period=InclusiveDateRange(None, date(2026, 8, 25))
    )
    observation = _observation(
        OBSERVATION_1,
        2,
        "나중에 추가된 아동",
        mobile_phone="01012345678",
    )

    plan = stage_household_grouping(
        (observation,),
        HouseholdMatchingRule(RULE_ID, 1),
        effective_on=EFFECTIVE_ON,
        roster=roster,
        household_id_factory=_uuid_factory(
            "83000000-0000-0000-0000-000000000085",
        ),
    )

    assert plan.existing_proposals == ()
    assert plan.default_household_ids[OBSERVATION_1] == plan.separate_household_ids[
        OBSERVATION_1
    ]


def test_rule_manifest_and_audit_snapshot_round_trip():
    rule = HouseholdMatchingRule(
        RULE_ID,
        1,
        (
            HouseholdMatchCriterion.MOBILE_PHONE,
            HouseholdMatchCriterion.ADDRESS,
        ),
    )
    stored = StoredHouseholdMatchingRule(rule)
    change = household_matching_rule_audit_change(None, stored)
    snapshot = HistoricalSnapshot(
        REVISION_ID,
        {EntityRef(HOUSEHOLD_MATCHING_RULE_KIND, RULE_ID): change.after},
    )

    assert HouseholdMatchingRule.from_manifest(rule.manifest()) == rule
    assert household_matching_rules_from_snapshot(snapshot) == (stored,)
