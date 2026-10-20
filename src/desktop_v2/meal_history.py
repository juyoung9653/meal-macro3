"""Personal meal-status rows for the desktop, independent of export tables."""

from __future__ import annotations

from uuid import UUID

from src.domain.projections import STATUS_EVENT_KIND, status_event_from_state
from src.domain.revisions import RevisionRecord, format_revision_timestamp
from src.domain.status import MealStatus
from src.services.person_history import is_roster_import_history

MEAL_HISTORY_HEADINGS = ("식종", "구분", "기간", "변경자", "변경번호")
MealHistoryRow = tuple[str, str, str, str, str]


def build_person_meal_history_rows(
    revisions: tuple[RevisionRecord, ...], person_id: UUID,
) -> tuple[MealHistoryRow, ...]:
    """Project affirmative status audit entries, oldest first, without writes."""
    rows: list[MealHistoryRow] = []
    categories = {
        MealStatus.DELIVERY: "시작",
        MealStatus.PAUSED: "일시중지",
        MealStatus.STOPPED: "중지",
    }
    for record in sorted(revisions, key=lambda item: item.metadata.recorded_at):
        if is_roster_import_history(record):
            continue
        metadata = record.metadata
        for change in record.changes:
            if change.entity_kind != STATUS_EVENT_KIND or change.after is None:
                continue
            event = status_event_from_state(change.entity_id, change.after)
            if event.person_id != person_id:
                continue
            starts_on = event.period.starts_on
            ends_on = event.period.ends_on
            start = starts_on.isoformat() if starts_on is not None else "시작일 미지정"
            end = (
                ends_on.isoformat() if ends_on is not None else "종료일 미지정"
            ) if event.status is MealStatus.PAUSED else ""
            rows.append((
                event.meal.value,
                categories[event.status],
                f"{start} ~ {end}",
                metadata.actor.username,
                metadata.display_code or format_revision_timestamp(metadata.recorded_at),
            ))
    return tuple(rows)
