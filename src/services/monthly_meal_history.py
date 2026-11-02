"""Read-only meal actions selected by start month or a finite period overlap."""

from __future__ import annotations

from calendar import monthrange
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from uuid import UUID

from src.domain.revision_visibility import effective_history_revisions
from src.domain.meal_history_cancellation import MealHistoryTarget, cancelled_meal_history_targets
from src.domain.projections import (
    ENROLLMENT_KIND, STATUS_EVENT_KIND, enrollments_from_state,
    roster_from_historical_snapshot, status_event_from_state, status_events_from_historical_snapshot,
)
from src.domain.revisions import ChangeOperation, HistoricalSnapshot, RevisionRecord, RevisionSource, format_revision_timestamp
from src.domain.roster import InclusiveDateRange, Meal
from src.domain.status import MealStatus
from src.services.person_history import is_roster_import_history, meal_change_details


@dataclass(frozen=True, slots=True)
class MonthlyMealHistoryEntry:
    person_id: UUID
    name: str
    birth_date: date | None
    school_name: str | None
    meal: Meal
    status: MealStatus
    period: InclusiveDateRange
    actor: str
    revision_code: str
    intake_channel: str = "-"
    note: str = ""
    source_period: InclusiveDateRange | None = None


@dataclass(frozen=True, slots=True)
class MonthlyMealHistory:
    revision_id: UUID
    year: int
    month: int
    entries: tuple[MonthlyMealHistoryEntry, ...]


def build_monthly_meal_history(
    snapshot: HistoricalSnapshot, revisions: tuple[RevisionRecord, ...], year: int, month: int,
    *, include_all_periods: bool = False,
) -> MonthlyMealHistory:
    """Keep raw action labels, but trim superseded and unenrolled dates.

    Effective status intervals coalesce distinct source actions and may relabel a
    stopped action as a pause. Subtracting newer raw event periods retains the
    original action identity and follows the same later-recorded-wins rule.
    """
    if not isinstance(snapshot, HistoricalSnapshot):
        raise TypeError("명단 변경 이력을 읽을 수 없습니다.")
    if any(isinstance(value, bool) or not isinstance(value, int) for value in (year, month)):
        raise ValueError("조회 연월이 올바르지 않습니다.")
    selected = InclusiveDateRange(date(year, month, 1), date(year, month, monthrange(year, month)[1]))
    if not revisions or revisions[-1].metadata.revision_id != snapshot.revision_id:
        raise ValueError("조회 원본과 변경 이력의 변경번호가 다릅니다.")
    roster = roster_from_historical_snapshot(snapshot)
    people = {person.person_id: person for person in roster.persons}
    enrollments = defaultdict(list)
    for enrollment in roster.enrollments:
        enrollments[enrollment.person_id, enrollment.meal].append(enrollment.period)
    events = defaultdict(list)
    for event in status_events_from_historical_snapshot(snapshot):
        events[event.person_id, event.meal].append(event)
    # Stable sorting preserves the projection's original ledger insertion order
    # for timestamp ties, as resolve_status_history does.
    for items in events.values():
        items.sort(key=lambda event: event.recorded_at)
    revisions = effective_history_revisions(revisions)
    cancelled = cancelled_meal_history_targets(revisions)
    candidates = {}
    for record in revisions:
        metadata = record.metadata
        if (is_roster_import_history(record)
                or "meal_history_cancellation" in metadata.decisions
                or "snapshot_restore_target" in metadata.decisions):
            continue
        explicit = set()
        for change in record.changes:
            if change.entity_kind != STATUS_EVENT_KIND or change.after is None:
                continue
            event = status_event_from_state(change.entity_id, change.after)
            target = MealHistoryTarget(metadata.revision_id, STATUS_EVENT_KIND, event.event_id)
            if target in cancelled:
                continue
            explicit.add((event.person_id, event.meal))
            if snapshot.entity(STATUS_EVENT_KIND, event.event_id) == change.after:
                candidates[(STATUS_EVENT_KIND, event.event_id)] = (
                    event.person_id, event.meal, event.status, event.period, metadata, event.event_id,
                )
        added = metadata.decisions.get("roster_row_change")
        if (metadata.source is not RevisionSource.UI or not isinstance(added, Mapping)
                or added.get("operation") != "add"):
            continue
        for change in record.changes:
            if (change.entity_kind != ENROLLMENT_KIND or change.operation is not ChangeOperation.INSERT
                    or change.after is None or change.after.get("person_id") != added.get("person_id")):
                continue
            for enrollment in enrollments_from_state(change.entity_id, change.after):
                target = MealHistoryTarget(metadata.revision_id, ENROLLMENT_KIND, change.entity_id, enrollment.period)
                if target in cancelled or (enrollment.person_id, enrollment.meal) in explicit:
                    continue
                candidates[(ENROLLMENT_KIND, change.entity_id, enrollment.period)] = (
                    enrollment.person_id, enrollment.meal, MealStatus.DELIVERY,
                    enrollment.period, metadata, None,
                )
    rows = []
    for person_id, meal, status, period, metadata, event_id in candidates.values():
        # An ongoing action belongs to its original start month, even if later
        # overrides split its effective period into newly starting segments.
        if not include_all_periods and period.ends_on is None and (
            period.starts_on is None or not selected.contains(period.starts_on)
        ):
            continue
        person = people.get(person_id)
        if person is None:
            continue
        items = events[person_id, meal]
        newer = items if event_id is None else items[next(index for index, item in enumerate(items) if item.event_id == event_id) + 1:]
        spans = [period]
        for event in newer:
            spans = [part for span in spans for part in _subtract(span, event.period)]
        for span in spans:
            for enrollment_period in enrollments[person_id, meal]:
                applicable = _intersection(span, enrollment_period)
                if applicable is None or (not include_all_periods and not applicable.overlaps(selected)):
                    continue
                profile = person.profile
                rows.append(MonthlyMealHistoryEntry(
                    person_id, profile.name, profile.birth_date, profile.school_name,
                    meal, status, applicable, metadata.actor.username,
                    metadata.display_code or format_revision_timestamp(metadata.recorded_at),
                    *meal_change_details(metadata.decisions),
                    source_period=period,
                ))
    meal_order = {Meal.DINNER: 0, Meal.BREAKFAST: 1, Meal.LUNCH: 2}
    rows.sort(key=lambda row: (
        -(row.period.starts_on or date.min).toordinal(), row.name,
        str(row.person_id), meal_order[row.meal], row.status.value,
    ))
    return MonthlyMealHistory(snapshot.revision_id, year, month, tuple(rows))


def _intersection(first: InclusiveDateRange, second: InclusiveDateRange) -> InclusiveDateRange | None:
    start = max(first.starts_on or date.min, second.starts_on or date.min)
    end = min(first.ends_on or date.max, second.ends_on or date.max)
    if start > end:
        return None
    return InclusiveDateRange(
        start if first.starts_on is not None or second.starts_on is not None else None,
        end if first.ends_on is not None or second.ends_on is not None else None,
    )


def _subtract(period: InclusiveDateRange, removed: InclusiveDateRange) -> tuple[InclusiveDateRange, ...]:
    overlap = _intersection(period, removed)
    if overlap is None:
        return (period,)
    parts = []
    if overlap.starts_on is not None and (period.starts_on or date.min) < overlap.starts_on:
        parts.append(InclusiveDateRange(period.starts_on, date.fromordinal(overlap.starts_on.toordinal() - 1)))
    if overlap.ends_on is not None and overlap.ends_on < (period.ends_on or date.max):
        parts.append(InclusiveDateRange(date.fromordinal(overlap.ends_on.toordinal() + 1), period.ends_on))
    return tuple(parts)
