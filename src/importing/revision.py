"""Build one confirmed Excel import as an immutable revision record."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from src.domain.projections import roster_audit_changes
from src.domain.revisions import (
    ImportBatch,
    RevisionActor,
    RevisionMetadata,
    RevisionRecord,
    RevisionSource,
)
from src.domain.roster import RosterSnapshot

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
