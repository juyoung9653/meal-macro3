"""Revision-backed corrections to one meal action's intake channel and note."""
from __future__ import annotations

from .intake import IntakeChannel
from .meal_history_cancellation import MealHistoryTarget, cancelled_meal_history_targets
from .projections import ENROLLMENT_KIND, STATUS_EVENT_KIND, roster_from_historical_snapshot
from .revision_visibility import effective_history_revisions
from .revisions import AuditChange, ChangeOperation, RevisionMetadata, RevisionRecord, RevisionSource


DETAILS_KIND = "meal.history_details"


def meal_history_details(revisions):
    """Replay corrections, including snapshot restore deletions."""
    states = {}
    for record in effective_history_revisions(revisions):
        if "workspace_branch" in record.metadata.decisions:
            continue
        for change in record.changes:
            if change.entity_kind == DETAILS_KIND:
                if change.after is None:
                    states.pop(change.entity_id, None)
                else:
                    states[change.entity_id] = change.after
    return {MealHistoryTarget.from_state(state): (state["intake_channel"] or "-", state["note"])
            for state in states.values()}


def build_meal_history_details_revision(current, revisions, target, intake_channel, note, *,
                                        actor, recorded_at, revision_id):
    from src.services.person_history import meal_change_details, is_roster_import_history

    if intake_channel not in (None, "", "-", *(channel.value for channel in IntakeChannel)):
        raise ValueError("접수 경로는 전화·메일·기타 중에서 선택해주세요.")
    if not isinstance(note, str):
        raise ValueError("비고를 글자로 입력해주세요.")
    if not revisions or revisions[-1].metadata.revision_id != current.revision_id:
        raise ValueError("이력 조회 내용이 달라졌습니다. 창을 다시 열어주세요.")
    visible = effective_history_revisions(revisions)
    source = next((record for record in visible if record.metadata.revision_id == target.revision_id), None)
    if (source is None or is_roster_import_history(source)
            or target in cancelled_meal_history_targets(visible)):
        raise ValueError("선택한 식종 변경 이력이 현재 적용되지 않습니다. 창을 다시 열어주세요.")
    change = next((change for change in source.changes
                   if change.entity_kind == target.entity_kind and change.entity_id == target.entity_id), None)
    if change is None or change.after is None or target.entity_kind not in (STATUS_EVENT_KIND, ENROLLMENT_KIND):
        raise ValueError("수정할 식종 변경 이력을 찾을 수 없습니다. 창을 다시 열어주세요.")
    if target.entity_kind == STATUS_EVENT_KIND:
        valid = current.entity(target.entity_kind, target.entity_id) == change.after
    else:
        from .projections import enrollments_from_state
        added = source.metadata.decisions.get("roster_row_change", {})
        valid = (added.get("operation") == "add" and added.get("person_id") == change.after.get("person_id")
                 and any(item.period == target.period for item in enrollments_from_state(change.entity_id, change.after))
                 and current.entity(target.entity_kind, target.entity_id) is not None)
    if not valid:
        raise ValueError("선택한 식종 변경 이력이 달라졌습니다. 창을 다시 열어주세요.")
    from uuid import UUID
    person = roster_from_historical_snapshot(current).person(UUID(change.after["person_id"]))
    channel, note = intake_channel if intake_channel not in (None, "-") else "", note.strip()
    previous = meal_history_details(revisions).get(target, meal_change_details(source.metadata.decisions))
    if previous == (channel or "-", note):
        return None
    # Each correction is a fresh marker: editing again after a restore must not
    # reinsert an identity that existed in an earlier, now reverted revision.
    entity_id = revision_id
    after = {**target.state(), "person_id": str(person.person_id), "meal": change.after["meal"],
             "intake_channel": channel, "note": note,
             "original_intake_channel": previous[0],
             "original_note": previous[1]}
    return RevisionRecord(RevisionMetadata(
        revision_id, current.revision_id, recorded_at, None, actor,
        f"식종 이력 접수 경로·비고 수정 · {person.profile.name} · {change.after['meal']}",
        RevisionSource.UI, decisions={"meal_history_details": target.state()},
    ), (AuditChange(DETAILS_KIND, entity_id, ChangeOperation.INSERT, None, after),))
