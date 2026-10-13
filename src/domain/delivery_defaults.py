"""Revision-backed defaults inherited independently by unset delivery fields."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from uuid import UUID

from .revisions import AuditChange, ChangeOperation, JsonValue, RevisionActor, RevisionMetadata, RevisionRecord, RevisionSource
from .roster import HouseholdDeliverySetting, Meal, standard_delivery_defaults, validate_delivery_defaults

DELIVERY_DEFAULTS_KIND = "delivery_defaults"
DELIVERY_DEFAULTS_ID = UUID("fbdd0449-f159-4cb1-b29c-f786222cd377")


def delivery_defaults_from_state(state: Mapping[str, JsonValue] | None) -> tuple[HouseholdDeliverySetting, ...]:
    if state is None:
        return standard_delivery_defaults()
    return validate_delivery_defaults(
        HouseholdDeliverySetting(Meal(item["meal"]), tuple(item["weekdays"]), delivery_weekdays=tuple(item["delivery_weekdays"]))
        for item in state["meals"]
    )


def delivery_defaults_state(settings) -> dict:
    return {"meals": [
        {"meal": item.meal.value, "weekdays": list(item.weekdays), "delivery_weekdays": list(item.delivery_weekdays)}
        for item in validate_delivery_defaults(settings)
    ]}


def build_delivery_defaults_revision(
    current_revision_id: UUID,
    stored_state: Mapping[str, JsonValue] | None,
    settings: tuple[HouseholdDeliverySetting, ...],
    *, actor: RevisionActor, recorded_at: datetime, revision_id: UUID,
) -> RevisionRecord:
    settings = validate_delivery_defaults(settings)
    if settings == delivery_defaults_from_state(stored_state):
        raise ValueError("변경된 식종별 기본값이 없습니다.")
    if revision_id == current_revision_id:
        raise ValueError("새 revision ID는 현재 revision과 달라야 합니다.")
    after = delivery_defaults_state(settings)
    change = AuditChange(
        DELIVERY_DEFAULTS_KIND, DELIVERY_DEFAULTS_ID,
        ChangeOperation.INSERT if stored_state is None else ChangeOperation.UPDATE,
        stored_state, after,
    )
    metadata = RevisionMetadata(
        revision_id, current_revision_id, recorded_at, None, actor,
        "식종별 배송 기본값 변경", RevisionSource.UI,
        decisions={"delivery_defaults": after},
        rule_versions={"delivery_defaults_policy_version": 1},
    )
    return RevisionRecord(metadata, (change,))
