"""Personal meal-status rows for the desktop, independent of export tables."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID

from src.domain.revision_visibility import effective_history_revisions
from src.domain.meal_history_cancellation import MealHistoryTarget, cancelled_meal_history_targets
from src.domain.projections import ENROLLMENT_KIND, STATUS_EVENT_KIND, enrollments_from_state, status_event_from_state
from src.domain.revisions import ChangeOperation, RevisionRecord, RevisionSource, format_revision_timestamp
from src.domain.status import MealStatus, StatusEvent
from src.services.person_history import is_roster_import_history, meal_change_details

MEAL_HISTORY_HEADINGS = ("식종", "구분", "접수 경로", "기간", "비고", "변경자", "변경번호")
MealHistoryRow = tuple[str, str, str, str, str, str, str]


def meal_status_summary(event: StatusEvent) -> tuple[str, str, str]:
    """Format the three status fields shared by personal history surfaces."""
    category = {
        MealStatus.DELIVERY: "시작",
        MealStatus.PAUSED: "일시중지",
        MealStatus.STOPPED: "중지",
    }[event.status]
    starts_on = event.period.starts_on
    ends_on = event.period.ends_on
    start = starts_on.isoformat() if starts_on is not None else "시작일 미지정"
    end = (
        ends_on.isoformat() if ends_on is not None else "종료일 미지정"
    ) if event.status is MealStatus.PAUSED else ""
    return event.meal.value, category, f"{start} ~ {end}"


def build_person_meal_history_rows(
    revisions: tuple[RevisionRecord, ...], person_id: UUID,
) -> tuple[MealHistoryRow, ...]:
    """Project affirmative status audit entries, oldest first, without writes."""
    return build_meal_history_rows_by_person(revisions).get(person_id, ())


@dataclass(frozen=True)
class MealHistoryEntry:
    row: MealHistoryRow
    target: MealHistoryTarget


def build_meal_history_rows_by_person(revisions):
    return {person_id: tuple(entry.row for entry in entries)
            for person_id, entries in build_meal_history_entries_by_person(revisions).items()}


def build_meal_history_entries_by_person(revisions):
    """Keep stable audit identities separate from user-facing table values."""
    revisions = effective_history_revisions(revisions)
    rows = defaultdict(list)
    cancelled = cancelled_meal_history_targets(revisions)
    for record in sorted(revisions, key=lambda item: item.metadata.recorded_at):
        if (is_roster_import_history(record)
                or "meal_history_cancellation" in record.metadata.decisions
                or "snapshot_restore_target" in record.metadata.decisions):
            continue
        metadata = record.metadata
        intake, note = meal_change_details(metadata.decisions)
        details = (
            metadata.actor.username,
            metadata.display_code or format_revision_timestamp(metadata.recorded_at),
        )
        events = [
            status_event_from_state(change.entity_id, change.after)
            for change in record.changes
            if change.entity_kind == STATUS_EVENT_KIND and change.after is not None
        ]
        for event in events:
            target = MealHistoryTarget(metadata.revision_id, STATUS_EVENT_KIND, event.event_id)
            if target not in cancelled:
                meal, category, period = meal_status_summary(event)
                rows[event.person_id].append(MealHistoryEntry((meal, category, intake, period, note, *details), target))
        added = metadata.decisions.get("roster_row_change")
        if (metadata.source is not RevisionSource.UI
                or not isinstance(added, Mapping) or added.get("operation") != "add"):
            continue
        explicit_statuses = {(event.person_id, event.meal) for event in events
                             if MealHistoryTarget(metadata.revision_id, STATUS_EVENT_KIND, event.event_id) not in cancelled}
        for change in record.changes:
            if (change.entity_kind != ENROLLMENT_KIND
                    or change.operation is not ChangeOperation.INSERT
                    or change.after is None
                    or change.after.get("person_id") != added.get("person_id")):
                continue
            for enrollment in enrollments_from_state(change.entity_id, change.after):
                if (enrollment.person_id, enrollment.meal) in explicit_statuses:
                    continue
                target = MealHistoryTarget(metadata.revision_id, ENROLLMENT_KIND, change.entity_id, enrollment.period)
                if target in cancelled:
                    continue
                start = enrollment.period.starts_on
                period = f"{start.isoformat() if start is not None else '시작일 미지정'} ~ "
                rows[enrollment.person_id].append(MealHistoryEntry(
                    (enrollment.meal.value, "시작", intake, period, note, *details), target))
    return {person_id: tuple(items) for person_id, items in rows.items()}
