"""Reviewed household grouping rules for imported people."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import cast
from unicodedata import normalize
from uuid import UUID, uuid4

from src.domain.revisions import (
    AuditChange,
    ChangeOperation,
    HistoricalSnapshot,
    JsonValue,
)
from src.domain.roster import (
    Household,
    HouseholdMembership,
    InclusiveDateRange,
    PersonProfile,
    RosterSnapshot,
    normalize_phone,
)

from .application import ReconciliationResult
from .contracts import PersonObservation, ProfileField


HOUSEHOLD_MATCHING_RULE_KIND = "household_matching_rule"
_NORMALIZATION_VERSION = 1


class HouseholdMatchCriterion(StrEnum):
    MOBILE_PHONE = "mobile_phone"
    HOUSEHOLD_HEAD_NAME = "household_head_name"
    ADDRESS = "address"
    TELEPHONE = "telephone"


_CRITERION_ORDER = tuple(HouseholdMatchCriterion)


@dataclass(frozen=True, slots=True)
class HouseholdMatchingRule:
    rule_id: UUID
    version: int
    criteria: tuple[HouseholdMatchCriterion, ...] = (
        HouseholdMatchCriterion.MOBILE_PHONE,
    )

    def __post_init__(self) -> None:
        if not isinstance(self.rule_id, UUID):
            raise TypeError("가구 묶기 rule ID는 UUID여야 합니다.")
        if isinstance(self.version, bool) or not isinstance(self.version, int):
            raise TypeError("가구 묶기 rule version은 정수여야 합니다.")
        if self.version < 1:
            raise ValueError("가구 묶기 rule version은 1 이상이어야 합니다.")
        criteria = tuple(self.criteria)
        if not criteria:
            raise ValueError("가구 묶기 기준을 하나 이상 선택해야 합니다.")
        if any(not isinstance(item, HouseholdMatchCriterion) for item in criteria):
            raise TypeError("가구 묶기 기준은 HouseholdMatchCriterion이어야 합니다.")
        if len(set(criteria)) != len(criteria):
            raise ValueError("가구 묶기 기준을 중복 선택할 수 없습니다.")
        canonical = tuple(item for item in _CRITERION_ORDER if item in criteria)
        object.__setattr__(self, "criteria", canonical)

    def manifest(self) -> dict[str, JsonValue]:
        return {
            "rule_id": str(self.rule_id),
            "version": self.version,
            "criteria": [criterion.value for criterion in self.criteria],
            "match_mode": "all",
            "blank_values_match": False,
            "normalization_version": _NORMALIZATION_VERSION,
        }

    @classmethod
    def from_manifest(cls, manifest: Mapping[str, object]) -> HouseholdMatchingRule:
        if not isinstance(manifest, Mapping):
            raise TypeError("가구 묶기 rule manifest는 mapping이어야 합니다.")
        if manifest.get("match_mode") != "all":
            raise ValueError("지원하지 않는 가구 묶기 match mode입니다.")
        if manifest.get("blank_values_match") is not False:
            raise ValueError("빈 값은 가구 묶기 기준으로 사용할 수 없습니다.")
        if manifest.get("normalization_version") != _NORMALIZATION_VERSION:
            raise ValueError("지원하지 않는 가구 묶기 정규화 version입니다.")
        raw_criteria = manifest.get("criteria")
        if isinstance(raw_criteria, (str, bytes)):
            raise TypeError("가구 묶기 criteria는 sequence여야 합니다.")
        try:
            criteria = tuple(
                HouseholdMatchCriterion(str(value)) for value in raw_criteria
            )
        except TypeError as error:
            raise TypeError("가구 묶기 criteria는 sequence여야 합니다.") from error
        return cls(
            UUID(str(manifest["rule_id"])),
            manifest["version"],
            criteria,
        )


@dataclass(frozen=True, slots=True)
class StoredHouseholdMatchingRule:
    rule: HouseholdMatchingRule

    def __post_init__(self) -> None:
        if not isinstance(self.rule, HouseholdMatchingRule):
            raise TypeError("rule은 HouseholdMatchingRule이어야 합니다.")

    @property
    def rule_id(self) -> UUID:
        return self.rule.rule_id

    @property
    def version(self) -> int:
        return self.rule.version

    def state(self) -> dict[str, JsonValue]:
        return cast(dict[str, JsonValue], {"rule": self.rule.manifest()})


@dataclass(frozen=True, slots=True)
class HouseholdGroupProposal:
    observation_ids: tuple[UUID, ...]
    shared_household_id: UUID
    separate_household_ids: Mapping[UUID, UUID]
    match_values: Mapping[HouseholdMatchCriterion, str]

    def __post_init__(self) -> None:
        observation_ids = _ordered_uuids(self.observation_ids, "가구 후보 observation")
        if len(observation_ids) < 2:
            raise ValueError("가구 묶음 후보에는 두 명 이상이 필요합니다.")
        if not isinstance(self.shared_household_id, UUID):
            raise TypeError("공유 household ID는 UUID여야 합니다.")
        separate = dict(self.separate_household_ids)
        if set(separate) != set(observation_ids):
            raise ValueError("개별 household ID는 모든 후보 observation에 필요합니다.")
        if any(not isinstance(value, UUID) for value in separate.values()):
            raise TypeError("개별 household ID는 UUID여야 합니다.")
        if len(set(separate.values())) != len(separate):
            raise ValueError("개별 household ID는 서로 달라야 합니다.")
        if self.shared_household_id in separate.values():
            raise ValueError("공유 household ID와 개별 household ID는 달라야 합니다.")
        values = dict(self.match_values)
        if not values or any(
            not isinstance(key, HouseholdMatchCriterion)
            or not isinstance(value, str)
            or not value.strip()
            for key, value in values.items()
        ):
            raise ValueError("가구 후보의 일치 기준값이 올바르지 않습니다.")
        object.__setattr__(self, "observation_ids", observation_ids)
        object.__setattr__(self, "separate_household_ids", MappingProxyType(separate))
        object.__setattr__(self, "match_values", MappingProxyType(values))


@dataclass(frozen=True, slots=True)
class ExistingHouseholdProposal:
    observation_id: UUID
    candidate_household_ids: tuple[UUID, ...]
    match_values: Mapping[HouseholdMatchCriterion, str]

    def __post_init__(self) -> None:
        if not isinstance(self.observation_id, UUID):
            raise TypeError("기존 가구 후보 observation ID는 UUID여야 합니다.")
        candidates = tuple(self.candidate_household_ids)
        if not candidates or any(not isinstance(item, UUID) for item in candidates):
            raise ValueError("기존 가구 후보에는 하나 이상의 household UUID가 필요합니다.")
        if tuple(sorted(set(candidates), key=str)) != candidates:
            raise ValueError("기존 가구 후보는 중복 없이 UUID순이어야 합니다.")
        values = dict(self.match_values)
        if not values or any(
            not isinstance(key, HouseholdMatchCriterion)
            or not isinstance(value, str)
            or not value.strip()
            for key, value in values.items()
        ):
            raise ValueError("기존 가구 후보의 일치 기준값이 올바르지 않습니다.")
        object.__setattr__(self, "candidate_household_ids", candidates)
        object.__setattr__(self, "match_values", MappingProxyType(values))


@dataclass(frozen=True, slots=True)
class HouseholdGroupingPlan:
    rule: HouseholdMatchingRule
    effective_on: date
    observation_ids: tuple[UUID, ...]
    default_household_ids: Mapping[UUID, UUID]
    separate_household_ids: Mapping[UUID, UUID]
    proposals: tuple[HouseholdGroupProposal, ...]
    existing_proposals: tuple[ExistingHouseholdProposal, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.rule, HouseholdMatchingRule):
            raise TypeError("rule은 HouseholdMatchingRule이어야 합니다.")
        _validate_date(self.effective_on, "가구 소속 적용일")
        observation_ids = _ordered_uuids(self.observation_ids, "가구 plan observation")
        defaults = _uuid_mapping(
            self.default_household_ids,
            observation_ids,
            "기본 household ID",
        )
        separate = _uuid_mapping(
            self.separate_household_ids,
            observation_ids,
            "개별 household ID",
        )
        if len(set(separate.values())) != len(separate):
            raise ValueError("개별 household ID는 observation마다 달라야 합니다.")
        proposals = tuple(self.proposals)
        if any(not isinstance(item, HouseholdGroupProposal) for item in proposals):
            raise TypeError("proposals에는 HouseholdGroupProposal만 허용됩니다.")
        proposed_ids = tuple(
            observation_id
            for proposal in proposals
            for observation_id in proposal.observation_ids
        )
        if len(set(proposed_ids)) != len(proposed_ids):
            raise ValueError("한 observation을 둘 이상의 가구 묶음으로 제안할 수 없습니다.")
        if not set(proposed_ids) <= set(observation_ids):
            raise ValueError("가구 proposal에 plan 밖 observation이 있습니다.")
        shared_ids = tuple(proposal.shared_household_id for proposal in proposals)
        if len(set(shared_ids)) != len(shared_ids):
            raise ValueError("가구 proposal의 공유 household ID는 서로 달라야 합니다.")
        if set(shared_ids) & set(separate.values()):
            raise ValueError("공유 household ID와 개별 household ID는 겹칠 수 없습니다.")
        for proposal in proposals:
            if set(proposal.match_values) != set(self.rule.criteria):
                raise ValueError("가구 proposal의 일치 기준이 rule과 다릅니다.")
            for observation_id in proposal.observation_ids:
                if defaults[observation_id] != proposal.shared_household_id:
                    raise ValueError("proposal의 공유 household ID가 기본 결정과 다릅니다.")
                if separate[observation_id] != proposal.separate_household_ids[observation_id]:
                    raise ValueError("proposal의 개별 household ID가 plan과 다릅니다.")
        proposed_set = set(proposed_ids)
        existing_proposals = tuple(self.existing_proposals)
        if any(
            not isinstance(item, ExistingHouseholdProposal)
            for item in existing_proposals
        ):
            raise TypeError(
                "existing_proposals에는 ExistingHouseholdProposal만 허용됩니다."
            )
        existing_observation_ids = tuple(
            proposal.observation_id for proposal in existing_proposals
        )
        if len(set(existing_observation_ids)) != len(existing_observation_ids):
            raise ValueError("한 observation에 기존 가구 후보를 두 번 지정할 수 없습니다.")
        if not set(existing_observation_ids) <= set(observation_ids):
            raise ValueError("기존 가구 proposal에 plan 밖 observation이 있습니다.")
        if set(existing_observation_ids) & proposed_set:
            raise ValueError("한 observation에 신규·기존 가구 제안을 함께 둘 수 없습니다.")
        existing_household_ids = {
            household_id
            for proposal in existing_proposals
            for household_id in proposal.candidate_household_ids
        }
        if existing_household_ids & set(separate.values()):
            raise ValueError("기존 household ID와 개별 household ID는 겹칠 수 없습니다.")
        if existing_household_ids & set(shared_ids):
            raise ValueError("기존 household ID와 신규 공유 household ID는 겹칠 수 없습니다.")
        for proposal in existing_proposals:
            if set(proposal.match_values) != set(self.rule.criteria):
                raise ValueError("기존 가구 proposal의 일치 기준이 rule과 다릅니다.")
            candidates = proposal.candidate_household_ids
            expected_default = (
                candidates[0]
                if len(candidates) == 1
                else separate[proposal.observation_id]
            )
            if defaults[proposal.observation_id] != expected_default:
                raise ValueError("기존 가구 후보 수에 맞는 기본 결정이 아닙니다.")
        all_proposed = proposed_set | set(existing_observation_ids)
        for observation_id in set(observation_ids) - all_proposed:
            if defaults[observation_id] != separate[observation_id]:
                raise ValueError("제안이 없는 observation은 별도 가구여야 합니다.")
        object.__setattr__(self, "observation_ids", observation_ids)
        object.__setattr__(self, "default_household_ids", MappingProxyType(defaults))
        object.__setattr__(self, "separate_household_ids", MappingProxyType(separate))
        object.__setattr__(self, "proposals", proposals)
        object.__setattr__(self, "existing_proposals", existing_proposals)

    def proposal_for(self, observation_id: UUID) -> HouseholdGroupProposal | None:
        return next(
            (
                proposal
                for proposal in self.proposals
                if observation_id in proposal.observation_ids
            ),
            None,
        )

    def existing_proposal_for(
        self,
        observation_id: UUID,
    ) -> ExistingHouseholdProposal | None:
        return next(
            (
                proposal
                for proposal in self.existing_proposals
                if proposal.observation_id == observation_id
            ),
            None,
        )

    def allowed_household_ids(self, observation_id: UUID) -> frozenset[UUID]:
        if observation_id not in self.default_household_ids:
            raise KeyError(f"가구 plan에 없는 observation입니다: {observation_id}")
        existing = self.existing_proposal_for(observation_id)
        return frozenset(
            (
                self.default_household_ids[observation_id],
                self.separate_household_ids[observation_id],
                *(
                    ()
                    if existing is None
                    else existing.candidate_household_ids
                ),
            )
        )

    @property
    def required_decision_ids(self) -> tuple[UUID, ...]:
        return tuple(
            sorted(
                (
                    proposal.observation_id
                    for proposal in self.existing_proposals
                    if len(proposal.candidate_household_ids) > 1
                ),
                key=str,
            )
        )

    def default_decisions(self) -> dict[UUID, HouseholdGroupingDecision]:
        return {
            observation_id: HouseholdGroupingDecision(
                observation_id,
                household_id,
            )
            for observation_id, household_id in self.default_household_ids.items()
        }


@dataclass(frozen=True, slots=True)
class HouseholdGroupingDecision:
    observation_id: UUID
    household_id: UUID

    def __post_init__(self) -> None:
        if not isinstance(self.observation_id, UUID):
            raise TypeError("observation ID는 UUID여야 합니다.")
        if not isinstance(self.household_id, UUID):
            raise TypeError("household ID는 UUID여야 합니다.")


def stage_household_grouping(
    observations: tuple[PersonObservation, ...],
    rule: HouseholdMatchingRule,
    *,
    effective_on: date,
    roster: RosterSnapshot | None = None,
    household_id_factory: Callable[[], UUID] = uuid4,
) -> HouseholdGroupingPlan:
    """Compare imports with each other and active existing household members."""

    observations = tuple(observations)
    if any(not isinstance(item, PersonObservation) for item in observations):
        raise TypeError("observations에는 PersonObservation만 허용됩니다.")
    if not isinstance(rule, HouseholdMatchingRule):
        raise TypeError("rule은 HouseholdMatchingRule이어야 합니다.")
    if roster is not None and not isinstance(roster, RosterSnapshot):
        raise TypeError("roster는 RosterSnapshot 또는 None이어야 합니다.")
    _validate_date(effective_on, "가구 소속 적용일")
    observation_ids = tuple(item.observation_id for item in observations)
    if len(set(observation_ids)) != len(observation_ids):
        raise ValueError("observation ID가 중복되었습니다.")

    grouped: defaultdict[tuple[str, ...], list[PersonObservation]] = defaultdict(list)
    ungrouped: list[PersonObservation] = []
    for observation in observations:
        key = tuple(
            _criterion_key(observation, criterion) for criterion in rule.criteria
        )
        if any(not value for value in key):
            ungrouped.append(observation)
        else:
            grouped[key].append(observation)

    default_ids: dict[UUID, UUID] = {}
    separate_ids: dict[UUID, UUID] = {}
    proposals: list[HouseholdGroupProposal] = []
    existing_proposals: list[ExistingHouseholdProposal] = []
    allocated: set[UUID] = set()
    reserved_household_ids = {
        household.household_id
        for household in (() if roster is None else roster.households)
    }
    existing_by_key: defaultdict[tuple[str, ...], set[UUID]] = defaultdict(set)
    if roster is not None:
        for membership in roster.memberships:
            if not membership.period.contains(effective_on):
                continue
            profile = roster.person(membership.person_id).profile
            key = tuple(
                _profile_criterion_key(profile, criterion)
                for criterion in rule.criteria
            )
            if all(key):
                existing_by_key[key].add(membership.household_id)

    def allocate() -> UUID:
        value = household_id_factory()
        if not isinstance(value, UUID):
            raise TypeError("household ID factory는 UUID를 반환해야 합니다.")
        if value in allocated:
            raise ValueError("household ID factory가 중복 UUID를 반환했습니다.")
        if value in reserved_household_ids:
            raise ValueError("household ID factory가 기존 가구 UUID를 반환했습니다.")
        allocated.add(value)
        return value

    for key, candidates in grouped.items():
        existing_household_ids = tuple(
            sorted(existing_by_key.get(key, ()), key=str)
        )
        if existing_household_ids:
            for item in candidates:
                separate_id = allocate()
                separate_ids[item.observation_id] = separate_id
                default_ids[item.observation_id] = (
                    existing_household_ids[0]
                    if len(existing_household_ids) == 1
                    else separate_id
                )
                existing_proposals.append(
                    ExistingHouseholdProposal(
                        item.observation_id,
                        existing_household_ids,
                        {
                            criterion: _criterion_display(item, criterion)
                            for criterion in rule.criteria
                        },
                    )
                )
            continue
        if len(candidates) < 2:
            ungrouped.extend(candidates)
            continue
        shared_id = allocate()
        individual = {
            item.observation_id: allocate() for item in candidates
        }
        for item in candidates:
            default_ids[item.observation_id] = shared_id
            separate_ids[item.observation_id] = individual[item.observation_id]
        first = candidates[0]
        proposals.append(
            HouseholdGroupProposal(
                tuple(item.observation_id for item in candidates),
                shared_id,
                individual,
                {
                    criterion: _criterion_display(first, criterion)
                    for criterion in rule.criteria
                },
            )
        )
    for observation in ungrouped:
        household_id = allocate()
        default_ids[observation.observation_id] = household_id
        separate_ids[observation.observation_id] = household_id

    return HouseholdGroupingPlan(
        rule,
        effective_on,
        observation_ids,
        default_ids,
        separate_ids,
        tuple(proposals),
        tuple(existing_proposals),
    )


def apply_household_grouping(
    result: ReconciliationResult,
    plan: HouseholdGroupingPlan,
    *,
    decisions: Mapping[UUID, HouseholdGroupingDecision] | None = None,
    previous_roster: RosterSnapshot | None = None,
) -> ReconciliationResult:
    """Assign only newly created people; existing identities keep their household."""

    if not isinstance(result, ReconciliationResult):
        raise TypeError("result는 ReconciliationResult여야 합니다.")
    if not isinstance(plan, HouseholdGroupingPlan):
        raise TypeError("plan은 HouseholdGroupingPlan이어야 합니다.")
    if previous_roster is not None and not isinstance(previous_roster, RosterSnapshot):
        raise TypeError("previous roster는 RosterSnapshot 또는 None이어야 합니다.")
    result_observation_ids = set(result.person_ids_by_observation) | set(
        result.ignored_observation_ids
    )
    if result_observation_ids != set(plan.observation_ids):
        raise ValueError("가구 plan과 사람 matching 결과의 observation이 다릅니다.")
    provided_decisions = dict(decisions or {})
    decision_by_id = plan.default_decisions()
    for observation_id, decision in provided_decisions.items():
        if not isinstance(observation_id, UUID):
            raise TypeError("household decision key는 observation UUID여야 합니다.")
        if not isinstance(decision, HouseholdGroupingDecision):
            raise TypeError("household decisions에는 HouseholdGroupingDecision만 허용됩니다.")
        if decision.observation_id != observation_id:
            raise ValueError("household decision key와 observation ID가 다릅니다.")
        if observation_id not in plan.default_household_ids:
            raise ValueError("household plan에 없는 observation 결정입니다.")
        if decision.household_id not in plan.allowed_household_ids(observation_id):
            raise ValueError("기존·신규 가구 제안 또는 개별 가구 ID만 선택할 수 있습니다.")
        decision_by_id[observation_id] = decision

    existing_person_ids = {
        person.person_id
        for person in (() if previous_roster is None else previous_roster.persons)
    }
    newly_created_observation_ids = {
        observation_id
        for observation_id, person_id in result.person_ids_by_observation.items()
        if person_id not in existing_person_ids
    }
    missing_required = (
        set(plan.required_decision_ids) & newly_created_observation_ids
    ) - set(provided_decisions)
    if missing_required:
        raise ValueError("여러 기존 가구가 일치한 행의 가구 선택이 필요합니다.")
    used_household_ids = {household.household_id for household in result.snapshot.households}
    existing_candidate_ids = {
        household_id
        for proposal in plan.existing_proposals
        for household_id in proposal.candidate_household_ids
    }
    if not existing_candidate_ids <= used_household_ids:
        raise ValueError("가구 plan의 기존 household 후보가 현재 roster에 없습니다.")
    planned_new_household_ids = set(plan.separate_household_ids.values()) | {
        proposal.shared_household_id for proposal in plan.proposals
    }
    if planned_new_household_ids & used_household_ids:
        raise ValueError("새 가구 plan의 household ID가 기존 가구 ID와 겹칩니다.")
    households = list(result.snapshot.households)
    memberships = list(result.snapshot.memberships)
    applied_manifest: dict[str, object] = {}
    for observation_id, person_id in result.person_ids_by_observation.items():
        if person_id in existing_person_ids:
            continue
        decision = decision_by_id.get(observation_id)
        if decision is None:
            raise ValueError("새 사람의 household 결정이 없습니다.")
        household_id = decision.household_id
        if household_id not in used_household_ids:
            households.append(Household(household_id))
            used_household_ids.add(household_id)
        memberships.append(
            HouseholdMembership(
                person_id,
                household_id,
                InclusiveDateRange(plan.effective_on, None),
            )
        )
        applied_manifest[str(observation_id)] = {
            "household_id": str(household_id),
            "kept_suggestion": (
                household_id == plan.default_household_ids[observation_id]
            ),
            "selection_kind": (
                "existing_household"
                if household_id in existing_candidate_ids
                else "separate_household"
                if household_id == plan.separate_household_ids[observation_id]
                else "new_shared_household"
            ),
        }

    snapshot = RosterSnapshot(
        result.snapshot.revision_id,
        persons=result.snapshot.persons,
        households=tuple(sorted(households, key=lambda item: str(item.household_id))),
        memberships=tuple(
            sorted(
                memberships,
                key=lambda item: (
                    str(item.person_id),
                    item.period.starts_on or date.min,
                    item.period.ends_on or date.max,
                    str(item.household_id),
                ),
            )
        ),
        enrollments=result.snapshot.enrollments,
        assignments=result.snapshot.assignments,
        custom_fields=result.snapshot.custom_fields,
        delivery_defaults=result.snapshot.delivery_defaults,
    )
    manifest = {
        **dict(result.decision_manifest),
        "household_rule": plan.rule.manifest(),
        "households": applied_manifest,
    }
    return ReconciliationResult(
        snapshot,
        result.person_ids_by_observation,
        result.ignored_observation_ids,
        manifest,
    )


def household_matching_rule_audit_change(
    before: StoredHouseholdMatchingRule | None,
    after: StoredHouseholdMatchingRule,
) -> AuditChange:
    if before is not None and not isinstance(before, StoredHouseholdMatchingRule):
        raise TypeError("before는 StoredHouseholdMatchingRule 또는 None이어야 합니다.")
    if not isinstance(after, StoredHouseholdMatchingRule):
        raise TypeError("after는 StoredHouseholdMatchingRule이어야 합니다.")
    if before is None:
        if after.version != 1:
            raise ValueError("새 가구 묶기 rule의 첫 version은 1이어야 합니다.")
        return AuditChange(
            HOUSEHOLD_MATCHING_RULE_KIND,
            after.rule_id,
            ChangeOperation.INSERT,
            None,
            after.state(),
        )
    if before.rule_id != after.rule_id:
        raise ValueError("가구 묶기 rule update의 ID가 다릅니다.")
    if after.version != before.version + 1:
        raise ValueError("가구 묶기 rule version은 정확히 1씩 증가해야 합니다.")
    return AuditChange(
        HOUSEHOLD_MATCHING_RULE_KIND,
        after.rule_id,
        ChangeOperation.UPDATE,
        before.state(),
        after.state(),
    )


def household_matching_rules_from_snapshot(
    snapshot: HistoricalSnapshot | None,
) -> tuple[StoredHouseholdMatchingRule, ...]:
    if snapshot is None:
        return ()
    if not isinstance(snapshot, HistoricalSnapshot):
        raise TypeError("snapshot은 HistoricalSnapshot 또는 None이어야 합니다.")
    rules = []
    for reference, state in snapshot.entities.items():
        if reference.entity_kind != HOUSEHOLD_MATCHING_RULE_KIND:
            continue
        values = dict(state)
        if set(values) != {"rule"}:
            raise ValueError("가구 묶기 rule state 필드가 올바르지 않습니다.")
        manifest = values["rule"]
        if not isinstance(manifest, Mapping):
            raise TypeError("가구 묶기 rule manifest는 mapping이어야 합니다.")
        rule = HouseholdMatchingRule.from_manifest(manifest)
        if rule.rule_id != reference.entity_id:
            raise ValueError("가구 묶기 rule entity ID와 manifest ID가 다릅니다.")
        rules.append(StoredHouseholdMatchingRule(rule))
    return tuple(sorted(rules, key=lambda item: str(item.rule_id)))


def match_existing_household_id(
    profile: PersonProfile,
    rule: HouseholdMatchingRule | None,
    roster: RosterSnapshot,
    *,
    effective_on: date,
) -> UUID | None:
    """Return the one active household matching the saved import rule.

    A missing rule keeps the established mobile-phone default. Blank criteria do
    not match, and multiple household matches require the caller to stop rather
    than selecting an arbitrary household.
    """

    if not isinstance(profile, PersonProfile):
        raise TypeError("profile은 PersonProfile이어야 합니다.")
    if rule is not None and not isinstance(rule, HouseholdMatchingRule):
        raise TypeError("rule은 HouseholdMatchingRule 또는 None이어야 합니다.")
    if not isinstance(roster, RosterSnapshot):
        raise TypeError("roster는 RosterSnapshot이어야 합니다.")
    _validate_date(effective_on, "가구 연결 적용일")
    criteria = (
        (HouseholdMatchCriterion.MOBILE_PHONE,)
        if rule is None
        else rule.criteria
    )
    def row_key(candidate: PersonProfile) -> tuple[str, ...]:
        # Row creation has one address field, like the roster. Match it to
        # imported profiles whose displayed address still has two stored parts.
        return tuple(
            _normalized_text(" ".join((candidate.road_address or "", candidate.detail_address or "")))
            if item is HouseholdMatchCriterion.ADDRESS
            else _profile_criterion_key(candidate, item)
            for item in criteria
        )

    key = row_key(profile)
    if any(not value for value in key):
        return None

    matches = {
        membership.household_id
        for membership in roster.memberships
        if membership.period.contains(effective_on)
        and row_key(roster.person(membership.person_id).profile) == key
    }
    if len(matches) > 1:
        raise ValueError(
            "입력한 정보와 일치하는 기존 가구가 둘 이상입니다. "
            "연락처와 주소를 확인한 뒤 다시 시도해주세요."
        )
    return next(iter(matches), None)


def _criterion_key(
    observation: PersonObservation,
    criterion: HouseholdMatchCriterion,
) -> str:
    if criterion is HouseholdMatchCriterion.MOBILE_PHONE:
        value = observation.profile_values.get(ProfileField.MOBILE_PHONE)
        return normalize_phone(value if isinstance(value, str) else None)
    if criterion is HouseholdMatchCriterion.TELEPHONE:
        value = observation.profile_values.get(ProfileField.TELEPHONE)
        return normalize_phone(value if isinstance(value, str) else None)
    if criterion is HouseholdMatchCriterion.HOUSEHOLD_HEAD_NAME:
        value = observation.profile_values.get(ProfileField.HOUSEHOLD_HEAD_NAME)
        return _normalized_text(value if isinstance(value, str) else None)
    road = observation.profile_values.get(ProfileField.ROAD_ADDRESS)
    detail = observation.profile_values.get(ProfileField.DETAIL_ADDRESS)
    if not isinstance(road, str) or not road.strip():
        return ""
    return "|".join(
        (
            _normalized_text(road),
            _normalized_text(detail if isinstance(detail, str) else None),
        )
    )


def _profile_criterion_key(
    profile: PersonProfile,
    criterion: HouseholdMatchCriterion,
) -> str:
    if criterion is HouseholdMatchCriterion.MOBILE_PHONE:
        return normalize_phone(profile.mobile_phone)
    if criterion is HouseholdMatchCriterion.TELEPHONE:
        return normalize_phone(profile.telephone)
    if criterion is HouseholdMatchCriterion.HOUSEHOLD_HEAD_NAME:
        return _normalized_text(profile.household_head_name)
    if profile.road_address is None:
        return ""
    return "|".join(
        (
            _normalized_text(profile.road_address),
            _normalized_text(profile.detail_address),
        )
    )


def _criterion_display(
    observation: PersonObservation,
    criterion: HouseholdMatchCriterion,
) -> str:
    if criterion is HouseholdMatchCriterion.MOBILE_PHONE:
        value = observation.profile_values.get(ProfileField.MOBILE_PHONE)
        return str(value).strip()
    if criterion is HouseholdMatchCriterion.TELEPHONE:
        value = observation.profile_values.get(ProfileField.TELEPHONE)
        return str(value).strip()
    if criterion is HouseholdMatchCriterion.HOUSEHOLD_HEAD_NAME:
        value = observation.profile_values.get(ProfileField.HOUSEHOLD_HEAD_NAME)
        return str(value).strip()
    road = observation.profile_values.get(ProfileField.ROAD_ADDRESS)
    detail = observation.profile_values.get(ProfileField.DETAIL_ADDRESS)
    return " ".join(
        value.strip()
        for value in (road, detail)
        if isinstance(value, str) and value.strip()
    )


def _normalized_text(value: str | None) -> str:
    if value is None:
        return ""
    return "".join(normalize("NFKC", value).split()).casefold()


def _ordered_uuids(values: tuple[UUID, ...], label: str) -> tuple[UUID, ...]:
    result = tuple(values)
    if any(not isinstance(value, UUID) for value in result):
        raise TypeError(f"{label} ID는 UUID여야 합니다.")
    if len(set(result)) != len(result):
        raise ValueError(f"{label} ID가 중복되었습니다.")
    return result


def _uuid_mapping(
    values: Mapping[UUID, UUID],
    observation_ids: tuple[UUID, ...],
    label: str,
) -> dict[UUID, UUID]:
    result = dict(values)
    if set(result) != set(observation_ids):
        raise ValueError(f"{label}는 모든 observation에 필요합니다.")
    if any(not isinstance(value, UUID) for value in result.values()):
        raise TypeError(f"{label}는 UUID여야 합니다.")
    return result


def _validate_date(value: object, label: str) -> None:
    if isinstance(value, datetime) or not isinstance(value, date):
        raise TypeError(f"{label}은 date여야 합니다.")
