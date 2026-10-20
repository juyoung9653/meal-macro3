"""Personal meal-status rows for the desktop, independent of export tables."""

from __future__ import annotations

from collections import defaultdict
from uuid import UUID

from src.domain.projections import STATUS_EVENT_KIND, status_event_from_state
from src.domain.revisions import RevisionRecord, format_revision_timestamp
from src.domain.status import MealStatus, StatusEvent
from src.services.person_history import is_roster_import_history

MEAL_HISTORY_HEADINGS = ("식종", "구분", "기간", "변경자", "변경번호")
MealHistoryRow = tuple[str, str, str, str, str]


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


def build_meal_history_rows_by_person(
    revisions: tuple[RevisionRecord, ...],
) -> dict[UUID, tuple[MealHistoryRow, ...]]:
    """Project each person's status rows once for a shared revision chain."""
    rows: dict[UUID, list[MealHistoryRow]] = defaultdict(list)
    for record in sorted(revisions, key=lambda item: item.metadata.recorded_at):
        if is_roster_import_history(record):
            continue
        metadata = record.metadata
        for change in record.changes:
            if change.entity_kind != STATUS_EVENT_KIND or change.after is None:
                continue
            event = status_event_from_state(change.entity_id, change.after)
            rows[event.person_id].append((
                *meal_status_summary(event),
                metadata.actor.username,
                metadata.display_code or format_revision_timestamp(metadata.recorded_at),
            ))
    return {person_id: tuple(items) for person_id, items in rows.items()}
