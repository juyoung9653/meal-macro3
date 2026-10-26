"""Read-only import reconciliation that prepares an explicit staging review."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from uuid import UUID

from src.domain.roster import Meal, Person, RosterSnapshot, normalize_phone

from .contracts import PersonObservation, ProfileField


class ReconciliationDisposition(StrEnum):
    MATCHED = "matched"
    NEW = "new"
    REVIEW = "review"


class ReviewReason(StrEnum):
    MISSING_MOBILE_PHONE = "missing_mobile_phone"
    MISSING_BIRTH_DATE = "missing_birth_date"
    DUPLICATE_SOURCE_IDENTITY = "duplicate_source_identity"
    DUPLICATE_ROSTER_IDENTITY = "duplicate_roster_identity"
    NAME_COLLISION = "name_collision"
    BIRTH_DATE_COLLISION = "birth_date_collision"


@dataclass(frozen=True, slots=True)
class ProfileDifference:
    field: ProfileField
    before: str | date | None
    after: str | date | None

    def __post_init__(self) -> None:
        if not isinstance(self.field, ProfileField):
            raise TypeError("field는 ProfileField여야 합니다.")


@dataclass(frozen=True, slots=True)
class StagedPerson:
    observation: PersonObservation
    disposition: ReconciliationDisposition
    matched_person_id: UUID | None
    candidate_person_ids: tuple[UUID, ...]
    review_reasons: tuple[ReviewReason, ...]
    profile_differences: tuple[ProfileDifference, ...]
    meals_added: tuple[Meal, ...]
    meals_removed: tuple[Meal, ...]
    household_candidate_ids: tuple[UUID, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.observation, PersonObservation):
            raise TypeError("observation은 PersonObservation이어야 합니다.")
        if not isinstance(self.disposition, ReconciliationDisposition):
            raise TypeError("disposition은 ReconciliationDisposition이어야 합니다.")
        if self.matched_person_id is not None and not isinstance(
            self.matched_person_id,
            UUID,
        ):
            raise TypeError("matched person ID는 UUID 또는 None이어야 합니다.")
        candidates = _uuid_tuple(self.candidate_person_ids, "candidate person ID")
        reasons = tuple(self.review_reasons)
        if any(not isinstance(reason, ReviewReason) for reason in reasons):
            raise TypeError("review_reasons에는 ReviewReason만 허용됩니다.")
        if len(set(reasons)) != len(reasons):
            raise ValueError("검토 사유는 중복될 수 없습니다.")
        differences = tuple(self.profile_differences)
        if any(not isinstance(item, ProfileDifference) for item in differences):
            raise TypeError("profile_differences에는 ProfileDifference만 허용됩니다.")
        meals_added = _meal_tuple(self.meals_added, "추가 식종")
        meals_removed = _meal_tuple(self.meals_removed, "제거 식종")
        household_candidates = _uuid_tuple(
            self.household_candidate_ids,
            "가구 candidate ID",
        )
        if self.disposition is ReconciliationDisposition.MATCHED:
            if self.matched_person_id is None or reasons:
                raise ValueError("자동 matching 항목에는 확정된 사람 ID만 필요합니다.")
        elif self.matched_person_id is not None:
            raise ValueError("검토·신규 항목은 사람 ID를 미리 확정할 수 없습니다.")
        if self.disposition is ReconciliationDisposition.REVIEW and not reasons:
            raise ValueError("검토 항목에는 하나 이상의 검토 사유가 필요합니다.")
        if self.disposition is ReconciliationDisposition.NEW and (candidates or reasons):
            raise ValueError("신규 항목에는 identity 후보나 검토 사유가 없어야 합니다.")
        object.__setattr__(self, "candidate_person_ids", candidates)
        object.__setattr__(self, "review_reasons", reasons)
        object.__setattr__(self, "profile_differences", differences)
        object.__setattr__(self, "meals_added", meals_added)
        object.__setattr__(self, "meals_removed", meals_removed)
        object.__setattr__(self, "household_candidate_ids", household_candidates)

    @property
    def requires_decision(self) -> bool:
        return self.disposition is not ReconciliationDisposition.MATCHED

    @property
    def has_changes(self) -> bool:
        return bool(
            self.profile_differences or self.meals_added or self.meals_removed
        )


@dataclass(frozen=True, slots=True)
class ReconciliationPlan:
    roster_revision_id: UUID | None
    effective_on: date
    staged_people: tuple[StagedPerson, ...]

    def __post_init__(self) -> None:
        if self.roster_revision_id is not None and not isinstance(
            self.roster_revision_id,
            UUID,
        ):
            raise TypeError("roster revision ID는 UUID 또는 None이어야 합니다.")
        _validate_date(self.effective_on, "적용일")
        staged = tuple(self.staged_people)
        if any(not isinstance(item, StagedPerson) for item in staged):
            raise TypeError("staged_people에는 StagedPerson만 허용됩니다.")
        observation_ids = tuple(item.observation.observation_id for item in staged)
        if len(set(observation_ids)) != len(observation_ids):
            raise ValueError("staging plan의 observation ID가 중복되었습니다.")
        object.__setattr__(self, "staged_people", staged)

    @property
    def requires_decision(self) -> bool:
        return any(item.requires_decision for item in self.staged_people)

    def by_disposition(
        self,
        disposition: ReconciliationDisposition,
    ) -> tuple[StagedPerson, ...]:
        if not isinstance(disposition, ReconciliationDisposition):
            raise TypeError("disposition은 ReconciliationDisposition이어야 합니다.")
        return tuple(
            item for item in self.staged_people if item.disposition is disposition
        )


def stage_reconciliation(
    roster: RosterSnapshot | None,
    observations: tuple[PersonObservation, ...],
    *,
    effective_on: date,
) -> ReconciliationPlan:
    """Match only exact name+birth identities and surface every ambiguity."""

    if roster is not None and not isinstance(roster, RosterSnapshot):
        raise TypeError("roster는 RosterSnapshot 또는 None이어야 합니다.")
    _validate_date(effective_on, "적용일")
    observations = tuple(observations)
    if any(not isinstance(item, PersonObservation) for item in observations):
        raise TypeError("observations에는 PersonObservation만 허용됩니다.")
    observation_ids = tuple(item.observation_id for item in observations)
    if len(set(observation_ids)) != len(observation_ids):
        raise ValueError("observation ID가 중복되었습니다.")

    people_by_identity: defaultdict[tuple[str, date], list[Person]] = defaultdict(list)
    people_by_name: defaultdict[str, list[Person]] = defaultdict(list)
    people_by_birth: defaultdict[date, list[Person]] = defaultdict(list)
    for person in () if roster is None else roster.persons:
        name_key = _name_key(person.profile.name)
        people_by_name[name_key].append(person)
        if person.profile.birth_date is not None:
            identity = (name_key, person.profile.birth_date)
            people_by_identity[identity].append(person)
            people_by_birth[person.profile.birth_date].append(person)

    source_by_identity: defaultdict[
        tuple[str, date],
        list[PersonObservation],
    ] = defaultdict(list)
    for observation in observations:
        if observation.birth_date is not None:
            source_by_identity[
                (_name_key(observation.name), observation.birth_date)
            ].append(observation)

    staged = tuple(
        _stage_person(
            roster,
            observation,
            effective_on,
            people_by_identity,
            people_by_name,
            people_by_birth,
            source_by_identity,
        )
        for observation in observations
    )
    return ReconciliationPlan(
        None if roster is None else roster.revision_id,
        effective_on,
        staged,
    )


def stage_lunch_reconciliation(
    roster: RosterSnapshot | None,
    observations: tuple[PersonObservation, ...],
    *,
    effective_on: date,
) -> ReconciliationPlan:
    """Vacation lunch identities use name and the primary mobile number only."""
    if roster is not None and not isinstance(roster, RosterSnapshot):
        raise TypeError("roster는 RosterSnapshot 또는 None이어야 합니다.")
    _validate_date(effective_on, "적용일")
    observations = tuple(observations)
    if any(not isinstance(item, PersonObservation) for item in observations):
        raise TypeError("observations에는 PersonObservation만 허용됩니다.")
    if len({item.observation_id for item in observations}) != len(observations):
        raise ValueError("observation ID가 중복되었습니다.")
    by_identity = defaultdict(list)
    by_name = defaultdict(list)
    source_counts = defaultdict(int)
    for person in (() if roster is None else roster.persons):
        name = _name_key(person.profile.name)
        by_name[name].append(person)
        phone = normalize_phone(person.profile.mobile_phone)
        if phone:
            by_identity[name, phone].append(person)
    for observation in observations:
        source_counts[_name_key(observation.name), normalize_phone(
            observation.profile_values.get(ProfileField.MOBILE_PHONE)
        )] += 1
    staged = []
    for observation in observations:
        name = _name_key(observation.name)
        phone = normalize_phone(observation.profile_values.get(ProfileField.MOBILE_PHONE))
        exact = by_identity.get((name, phone), ()) if phone else ()
        reasons = []
        if not phone:
            reasons.append(ReviewReason.MISSING_MOBILE_PHONE)
        if source_counts[name, phone] > 1:
            reasons.append(ReviewReason.DUPLICATE_SOURCE_IDENTITY)
        if len(exact) > 1:
            reasons.append(ReviewReason.DUPLICATE_ROSTER_IDENTITY)
        if len(exact) == 1 and not reasons:
            person = exact[0]
            staged.append(StagedPerson(
                observation, ReconciliationDisposition.MATCHED, person.person_id,
                (), (), _profile_differences(person, observation), (), (), (),
            ))
            continue
        candidates = exact or by_name.get(name, ())
        if candidates and not exact:
            reasons.append(ReviewReason.NAME_COLLISION)
        staged.append(StagedPerson(
            observation,
            ReconciliationDisposition.REVIEW if reasons else ReconciliationDisposition.NEW,
            None, tuple(sorted((p.person_id for p in candidates), key=str)),
            tuple(reasons), (), tuple(observation.meals), (), (),
        ))
    return ReconciliationPlan(None if roster is None else roster.revision_id, effective_on, tuple(staged))


def _stage_person(
    roster: RosterSnapshot | None,
    observation: PersonObservation,
    effective_on: date,
    people_by_identity: dict[tuple[str, date], list[Person]],
    people_by_name: dict[str, list[Person]],
    people_by_birth: dict[date, list[Person]],
    source_by_identity: dict[tuple[str, date], list[PersonObservation]],
) -> StagedPerson:
    name_key = _name_key(observation.name)
    birth = observation.birth_date
    exact: tuple[Person, ...] = ()
    reasons: list[ReviewReason] = []
    if birth is None:
        if roster is None:
            return StagedPerson(
                observation,
                ReconciliationDisposition.NEW,
                None,
                (),
                (),
                (),
                tuple(observation.meals),
                (),
                (),
            )
        reasons.append(ReviewReason.MISSING_BIRTH_DATE)
    else:
        identity = (name_key, birth)
        exact = tuple(people_by_identity.get(identity, ()))
        if len(source_by_identity.get(identity, ())) > 1:
            reasons.append(ReviewReason.DUPLICATE_SOURCE_IDENTITY)
        if len(exact) > 1:
            reasons.append(ReviewReason.DUPLICATE_ROSTER_IDENTITY)

    if len(exact) == 1 and not reasons:
        matched = exact[0]
        assert roster is not None
        current_meals = roster.meals_for(matched.person_id, on=effective_on)
        observed_meals = set(observation.meals)
        return StagedPerson(
            observation,
            ReconciliationDisposition.MATCHED,
            matched.person_id,
            (),
            (),
            _profile_differences(matched, observation),
            tuple(meal for meal in Meal if meal in observed_meals and meal not in current_meals),
            tuple(meal for meal in Meal if meal in current_meals and meal not in observed_meals),
            _household_candidates(roster, observation, effective_on),
        )

    candidates = {person.person_id: person for person in exact}
    name_matches = tuple(people_by_name.get(name_key, ()))
    if name_matches and not exact:
        reasons.append(ReviewReason.NAME_COLLISION)
        candidates.update((person.person_id, person) for person in name_matches)
    if birth is not None:
        birth_matches = tuple(people_by_birth.get(birth, ()))
        if birth_matches and not exact:
            reasons.append(ReviewReason.BIRTH_DATE_COLLISION)
            candidates.update((person.person_id, person) for person in birth_matches)

    if not reasons and not candidates:
        disposition = ReconciliationDisposition.NEW
    else:
        disposition = ReconciliationDisposition.REVIEW
    return StagedPerson(
        observation,
        disposition,
        None,
        tuple(sorted(candidates, key=str)),
        tuple(dict.fromkeys(reasons)),
        (),
        tuple(observation.meals),
        (),
        _household_candidates(roster, observation, effective_on),
    )


def _profile_differences(
    person: Person,
    observation: PersonObservation,
) -> tuple[ProfileDifference, ...]:
    differences: list[ProfileDifference] = []
    for field, after in observation.profile_values.items():
        if after is None:
            continue
        before = getattr(person.profile, field.value)
        if not _profile_values_equal(field, before, after):
            differences.append(ProfileDifference(field, before, after))
    return tuple(differences)


def _profile_values_equal(
    field: ProfileField,
    before: str | date | None,
    after: str | date | None,
) -> bool:
    if field in (ProfileField.TELEPHONE, ProfileField.MOBILE_PHONE):
        return normalize_phone(before) == normalize_phone(after)
    if isinstance(before, str) and isinstance(after, str):
        return "".join(before.split()).casefold() == "".join(
            after.split()
        ).casefold()
    return before == after


def _household_candidates(
    roster: RosterSnapshot | None,
    observation: PersonObservation,
    effective_on: date,
) -> tuple[UUID, ...]:
    if roster is None:
        return ()
    phone_value = observation.profile_values.get(ProfileField.MOBILE_PHONE)
    phone_key = normalize_phone(phone_value if isinstance(phone_value, str) else None)
    if not phone_key:
        return ()
    person_ids = {
        person.person_id
        for person in roster.persons
        if person.profile.household_phone_key == phone_key
    }
    household_ids = {
        membership.household_id
        for membership in roster.memberships
        if membership.person_id in person_ids
        and membership.period.contains(effective_on)
    }
    return tuple(sorted(household_ids, key=str))


def _name_key(value: str) -> str:
    return "".join(value.split()).casefold()


def _uuid_tuple(values: tuple[UUID, ...], label: str) -> tuple[UUID, ...]:
    result = tuple(values)
    if any(not isinstance(value, UUID) for value in result):
        raise TypeError(f"{label}는 UUID여야 합니다.")
    if tuple(sorted(set(result), key=str)) != result:
        raise ValueError(f"{label}는 중복 없이 UUID순이어야 합니다.")
    return result


def _meal_tuple(values: tuple[Meal, ...], label: str) -> tuple[Meal, ...]:
    result = tuple(values)
    if any(not isinstance(value, Meal) for value in result):
        raise TypeError(f"{label}에는 Meal만 허용됩니다.")
    canonical = tuple(meal for meal in Meal if meal in result)
    if len(set(result)) != len(result) or canonical != result:
        raise ValueError(f"{label}은 중복 없이 표준 식종순이어야 합니다.")
    return result


def _validate_date(value: object, label: str) -> None:
    if isinstance(value, datetime) or not isinstance(value, date):
        raise TypeError(f"{label}은 date여야 합니다.")
