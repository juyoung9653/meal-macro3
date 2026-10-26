"""Build one confirmed Excel import as an immutable revision record."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum
from uuid import UUID, uuid4

from src.domain.projections import roster_audit_changes, status_event_audit_change
from src.domain.revisions import (
    ImportBatch,
    RevisionActor,
    RevisionMetadata,
    RevisionRecord,
    RevisionSource,
)
from src.domain.roster import DeliveryAssignment, Household, HouseholdMembership, InclusiveDateRange, Meal, MealEnrollment, RosterSnapshot
from src.domain.status import MealStatus, StatusEvent, resolve_status_history

from .application import (
    PROFILE_UPDATE_POLICY_VERSION,
    ReconciliationDecision,
    ReconciliationResult,
    apply_reconciliation,
)
from .households import (
    HouseholdGroupingDecision,
    HouseholdGroupingPlan,
    StoredHouseholdMatchingRule,
    apply_household_grouping,
    household_matching_rule_audit_change,
)
from .profile_history import StoredImportProfile, import_profile_audit_change
from .reconciliation import ReconciliationPlan
from .workbook import WorkbookImportPreview


@dataclass(frozen=True, slots=True)
class PreparedImportRevision:
    result: ReconciliationResult
    record: RevisionRecord
    stored_profile: StoredImportProfile
    stored_household_rule: StoredHouseholdMatchingRule | None


class ImportPurpose(StrEnum):
    DATABASE = "database"
    VACATION_LUNCH = "vacation_lunch"


def build_import_revision(
    current_roster: RosterSnapshot | None,
    preview: WorkbookImportPreview,
    plan: ReconciliationPlan,
    *,
    decisions: Mapping[UUID, ReconciliationDecision],
    actor: RevisionActor,
    recorded_at: datetime,
    revision_id: UUID,
    batch_id: UUID,
    reason: str = "Excel 명단 가져오기",
    existing_profile: StoredImportProfile | None = None,
    household_plan: HouseholdGroupingPlan | None = None,
    household_decisions: Mapping[UUID, HouseholdGroupingDecision] | None = None,
    existing_household_rule: StoredHouseholdMatchingRule | None = None,
    purpose: ImportPurpose = ImportPurpose.DATABASE,
    lunch_status_events: tuple[StatusEvent, ...] = (),
    lunch_value_choices: Mapping | None = None,
) -> PreparedImportRevision:
    """Apply reviewed identities, households, and rules in the same commit."""

    if current_roster is not None and not isinstance(current_roster, RosterSnapshot):
        raise TypeError("current roster는 RosterSnapshot 또는 None이어야 합니다.")
    if not isinstance(preview, WorkbookImportPreview):
        raise TypeError("preview는 WorkbookImportPreview이어야 합니다.")
    if not preview.can_continue:
        raise ValueError("오류가 남아 있거나 데이터가 없는 Excel은 반영할 수 없습니다.")
    if not isinstance(plan, ReconciliationPlan):
        raise TypeError("plan은 ReconciliationPlan이어야 합니다.")
    if not isinstance(actor, RevisionActor):
        raise TypeError("actor는 RevisionActor여야 합니다.")
    if not isinstance(recorded_at, datetime):
        raise TypeError("recorded_at은 datetime이어야 합니다.")
    if recorded_at.tzinfo is None or recorded_at.utcoffset() is None:
        raise ValueError("recorded_at에는 시간대가 있어야 합니다.")
    if not isinstance(revision_id, UUID) or not isinstance(batch_id, UUID):
        raise TypeError("revision ID와 batch ID는 UUID여야 합니다.")
    if not isinstance(purpose, ImportPurpose):
        raise TypeError("가져오기 목적이 올바르지 않습니다.")
    if purpose is ImportPurpose.VACATION_LUNCH:
        if current_roster is None:
            raise ValueError("방학 중식 명단은 기존 운영 명단이 있어야 적용할 수 있습니다.")
        if preview.notices:
            raise ValueError("방학 중식 명단에는 제외된 원본 행이 없어야 합니다.")
        if household_plan is not None:
            raise ValueError("방학 중식 명단에는 가구 묶기를 적용하지 않습니다.")
        if reason == "Excel 명단 가져오기":
            reason = "방학 중식 명단 갱신"
    elif lunch_status_events:
        raise ValueError("일반 명단 가져오기에 중식 상태 이력을 전달할 수 없습니다.")
    if household_plan is None:
        if household_decisions is not None or existing_household_rule is not None:
            raise ValueError("가구 plan 없이 가구 결정이나 기존 rule을 지정할 수 없습니다.")
    elif not isinstance(household_plan, HouseholdGroupingPlan):
        raise TypeError("household plan은 HouseholdGroupingPlan 또는 None이어야 합니다.")
    if existing_household_rule is not None and not isinstance(
        existing_household_rule,
        StoredHouseholdMatchingRule,
    ):
        raise TypeError(
            "existing household rule은 StoredHouseholdMatchingRule 또는 None이어야 합니다."
        )

    result = apply_reconciliation(
        current_roster,
        plan,
        new_revision_id=revision_id,
        decisions=decisions,
        custom_fields=preview.custom_fields,
    )
    lunch_events: tuple[StatusEvent, ...] = ()
    if purpose is ImportPurpose.VACATION_LUNCH:
        assert current_roster is not None
        result, lunch_events = _apply_vacation_lunch(
            current_roster, result, preview, plan, revision_id,
            recorded_at, tuple(lunch_status_events), lunch_value_choices,
        )
    if current_roster is None and not result.snapshot.persons:
        raise ValueError("최초 명단에서는 한 명 이상을 추가해야 합니다.")

    stored_household_rule = None
    household_rule_change = None
    if household_plan is not None:
        if household_plan.effective_on != plan.effective_on:
            raise ValueError("가구 plan과 사람 matching plan의 적용일이 다릅니다.")
        observation_ids = {
            observation.observation_id for observation in preview.observations
        }
        if set(household_plan.observation_ids) != observation_ids:
            raise ValueError("가구 plan의 observation이 가져오기 미리보기와 다릅니다.")
        result = apply_household_grouping(
            result,
            household_plan,
            decisions=household_decisions,
            previous_roster=current_roster,
        )
        stored_household_rule = StoredHouseholdMatchingRule(household_plan.rule)
        household_rule_change = _household_rule_change(
            existing_household_rule,
            stored_household_rule,
        )

    stored_profile = StoredImportProfile(
        preview.profile,
        preview.header_fingerprint,
    )
    profile_change = _profile_change(existing_profile, stored_profile)
    changes = roster_audit_changes(current_roster, result.snapshot)
    changes = (*changes, *(status_event_audit_change(event) for event in lunch_events))
    if profile_change is not None:
        changes = (*changes, profile_change)
    if household_rule_change is not None:
        changes = (*changes, household_rule_change)
    if not changes:
        raise ValueError("이번 가져오기로 변경되는 내용이 없습니다.")

    observation_sources = {
        str(observation.observation_id): {
            "rows": list(observation.source.row_numbers),
            "external_reference": observation.source.external_reference,
        }
        for observation in preview.observations
    }
    decision_manifest = {
        **dict(result.decision_manifest),
        "import_purpose": purpose.value,
        "vacation_lunch": {
            "present": [str(person_id) for person_id in sorted(
                (event.person_id for event in lunch_events if event.status is MealStatus.DELIVERY), key=str
            )],
            "stopped": [str(person_id) for person_id in sorted(
                (event.person_id for event in lunch_events if event.status is MealStatus.STOPPED), key=str
            )],
        } if purpose is ImportPurpose.VACATION_LUNCH else None,
        "observation_sources": observation_sources,
        "source_notices": [
            {
                "row": issue.row_number,
                "column": issue.column,
                "message": issue.message,
            }
            for issue in preview.notices
        ],
        "source_corrections": [
            {
                "row": row_number,
                "column": column,
            }
            for row_number, column in preview.corrected_cells
        ],
        "imported_delivery_history": [
            {
                **dict(resolution.manifest()),
                "person_id": (
                    None
                    if resolution.observation_id
                    not in result.person_ids_by_observation
                    else str(
                        result.person_ids_by_observation[
                            resolution.observation_id
                        ]
                    )
                ),
            }
            for resolution in preview.automatic_source_resolutions
        ],
    }
    parent_revision_id = (
        None if current_roster is None else current_roster.revision_id
    )
    rule_versions = {
        "import_profile_id": str(preview.profile.profile_id),
        "import_profile_version": preview.profile.version,
        "profile_update_policy_version": PROFILE_UPDATE_POLICY_VERSION,
    }
    if stored_household_rule is not None:
        rule_versions.update(
            {
                "household_matching_rule_id": str(stored_household_rule.rule_id),
                "household_matching_rule_version": stored_household_rule.version,
            }
        )
    metadata = RevisionMetadata(
        revision_id=revision_id,
        parent_revision_id=parent_revision_id,
        recorded_at=recorded_at,
        effective_on=plan.effective_on,
        actor=actor,
        reason=reason,
        source=RevisionSource.IMPORT,
        decisions=decision_manifest,
        rule_versions=rule_versions,
    )
    batch = ImportBatch(
        batch_id=batch_id,
        revision_id=revision_id,
        source_filename=preview.source_filename,
        sha256=preview.sha256,
        byte_size=preview.byte_size,
        sheet_name=preview.sheet_name,
        header_fingerprint=preview.header_fingerprint,
        mapping_manifest=preview.profile.manifest(),
        effective_on=plan.effective_on,
        decisions=decision_manifest,
    )
    return PreparedImportRevision(
        result,
        RevisionRecord(metadata, changes, batch),
        stored_profile,
        stored_household_rule,
    )


def _apply_vacation_lunch(
    current: RosterSnapshot,
    result: ReconciliationResult,
    preview: WorkbookImportPreview,
    plan: ReconciliationPlan,
    revision_id: UUID,
    recorded_at: datetime,
    historical_events: tuple[StatusEvent, ...],
    value_choices: Mapping | None = None,
) -> tuple[ReconciliationResult, tuple[StatusEvent, ...]]:
    if result.ignored_observation_ids:
        raise ValueError("방학 중식 명단에서는 행 제외를 허용하지 않습니다.")
    if not preview.observations:
        raise ValueError("방학 중식 명단에는 대상자가 한 명 이상 있어야 합니다.")
    if any(not isinstance(event, StatusEvent) or event.meal is not Meal.LUNCH for event in historical_events):
        raise ValueError("중식 상태 이력만 전달할 수 있습니다.")
    if any(event.period.starts_on is not None and event.period.starts_on > plan.effective_on for event in historical_events):
        raise ValueError("적용일 뒤에 이미 예약된 중식 상태 변경이 있습니다. 날짜를 다시 확인해주세요.")
    if any(event.recorded_at > recorded_at for event in historical_events):
        raise ValueError("새 명단보다 나중에 기록된 중식 상태 이력이 있습니다.")
    if len(result.person_ids_by_observation) != len(preview.observations):
        raise ValueError("모든 원본 행의 사람 연결을 확정해야 합니다.")
    existing_ids = {person.person_id for person in current.persons}
    present_ids = {
        result.person_ids_by_observation[item.observation_id]
        for item in preview.observations
    }
    enrolled_ids = {
        person.person_id for person in current.persons
        if Meal.LUNCH in current.meals_for(person.person_id, on=plan.effective_on)
    }
    delivering_ids = {
        person_id for person_id in enrolled_ids
        if resolve_status_history(person_id, Meal.LUNCH, tuple(
            event for event in historical_events if event.person_id == person_id
        )).status_on(plan.effective_on) is MealStatus.DELIVERY
    }
    stopped_ids = delivering_ids - present_ids
    from .lunch_values import apply_lunch_values
    persons, reviewed_assignments = apply_lunch_values(current, result, plan, value_choices)
    persons = tuple(sorted(persons, key=lambda person: str(person.person_id)))
    enrollments = list(current.enrollments)
    for person_id in sorted(present_ids, key=str):
        matching = [item for item in enrollments if item.person_id == person_id and item.meal is Meal.LUNCH]
        if any(item.period.contains(plan.effective_on) and item.period.ends_on is None for item in matching):
            continue
        for item in matching:
            if item.period.contains(plan.effective_on):
                enrollments.remove(item)
                future_starts = [other.period.starts_on for other in matching if other.period.starts_on is not None and other.period.starts_on > plan.effective_on]
                end = min(future_starts) - timedelta(days=1) if future_starts else None
                enrollments.append(MealEnrollment(person_id, Meal.LUNCH, InclusiveDateRange(item.period.starts_on, end)))
                break
        else:
            future_starts = [item.period.starts_on for item in matching if item.period.starts_on is not None and item.period.starts_on > plan.effective_on]
            end = min(future_starts) - timedelta(days=1) if future_starts else None
            enrollments.append(MealEnrollment(person_id, Meal.LUNCH, InclusiveDateRange(plan.effective_on, end)))
    new_person_ids = {person.person_id for person in persons} - existing_ids
    # The vacation-lunch path does not run household grouping. Give each new
    # person a standalone household from the import's effective date.
    new_household_ids = {person_id: uuid4() for person_id in new_person_ids}
    needed_custom_ids = {
        definition_id
        for person in persons
        for definition_id in person.profile.custom_values
    }
    needed_custom_ids.update(definition.definition_id for definition in preview.custom_fields)
    existing_custom_ids = {definition.definition_id for definition in current.custom_fields}
    custom_fields = (
        *current.custom_fields,
        *(definition for definition in result.snapshot.custom_fields
          if definition.definition_id in needed_custom_ids and definition.definition_id not in existing_custom_ids),
    )
    snapshot = RosterSnapshot(
        revision_id, persons,
        (*current.households, *(Household(household_id) for household_id in new_household_ids.values())),
        (*current.memberships, *(HouseholdMembership(person_id, household_id, InclusiveDateRange(plan.effective_on))
                                 for person_id, household_id in new_household_ids.items())),
        tuple(sorted(enrollments, key=lambda item: (str(item.person_id), item.meal.value, item.period.starts_on or date.min))),
        (*reviewed_assignments, *(
            DeliveryAssignment(person_id, Meal.LUNCH, assignment.driver, assignment.sequence)
            for person_id in sorted(present_ids - existing_ids, key=str)
            for assignment in (result.snapshot.resolve_assignment(person_id, Meal.LUNCH),)
            if assignment.driver is not None or assignment.sequence is not None
        )),
        custom_fields,
        delivery_defaults=current.delivery_defaults,
    )
    events = tuple(StatusEvent(
        uuid4(), revision_id, person_id, Meal.LUNCH, status,
        InclusiveDateRange(plan.effective_on, None), recorded_at,
        "방학 중식 명단 가져오기",
    ) for status, ids in (
        (MealStatus.DELIVERY, present_ids), (MealStatus.STOPPED, stopped_ids),
    ) for person_id in sorted(ids, key=str))
    return ReconciliationResult(
        snapshot, result.person_ids_by_observation,
        result.ignored_observation_ids, result.decision_manifest,
    ), events


def _profile_change(
    existing: StoredImportProfile | None,
    candidate: StoredImportProfile,
):
    if existing is None:
        return import_profile_audit_change(None, candidate)
    if not isinstance(existing, StoredImportProfile):
        raise TypeError("existing profile은 StoredImportProfile 또는 None이어야 합니다.")
    if existing.profile_id != candidate.profile_id:
        raise ValueError("기존 profile과 가져오기 profile의 ID가 다릅니다.")
    if existing == candidate:
        return None
    return import_profile_audit_change(existing, candidate)


def _household_rule_change(
    existing: StoredHouseholdMatchingRule | None,
    candidate: StoredHouseholdMatchingRule,
):
    if existing is None:
        return household_matching_rule_audit_change(None, candidate)
    if not isinstance(existing, StoredHouseholdMatchingRule):
        raise TypeError(
            "existing household rule은 StoredHouseholdMatchingRule 또는 None이어야 합니다."
        )
    if existing.rule_id != candidate.rule_id:
        raise ValueError("기존 가구 rule과 가져오기 가구 rule의 ID가 다릅니다.")
    if existing == candidate:
        return None
    return household_matching_rule_audit_change(existing, candidate)
