"""Keep household delivery routes contiguous when a meal starts or stops."""

from collections import defaultdict
from dataclasses import replace

from src.domain.delivery_routing import routing_state_from_snapshot
from src.domain.meal_status_projection import effective_meal_status
from src.domain.school_calendar import school_lunch_calendar_from_snapshot
from src.domain.projections import (
    ASSIGNMENT_KIND, STATUS_EVENT_KIND, roster_audit_changes,
    roster_from_historical_snapshot, status_event_from_state,
    status_events_from_historical_snapshot,
)
from src.domain.revisions import EntityRef, HistoricalSnapshot
from src.domain.sequence_edits import build_scoped_sequence_revision
from src.domain.status import MealStatus, resolve_status_history
from src.services.driver_households import build_driver_household_rows, sequence_sort_key as _sequence_key


DECISION = "meal_status_sequence"


def needs_meal_status_sequence(record):
    return (
        DECISION not in record.metadata.decisions
        and record.metadata.rule_versions.get("meal_status_sequence_policy_version") == 1
        and any(key in record.metadata.decisions for key in (
            "meal_status_change", "batch_meal_status_change",
        ))
        and any(
            change.entity_kind == STATUS_EVENT_KIND and change.after is not None
            and change.after.get("status") in (MealStatus.DELIVERY.value, MealStatus.STOPPED.value)
            for change in record.changes
        )
    )


def prepare_meal_status_sequence(snapshot, record):
    """Run after household status propagation, inside the status transaction.

    Sequences follow the existing undated assignment contract. Each changed
    meal uses its own start date to resolve household membership and driver.
    School-calendar eligibility matches the route editor; view filters do not apply.
    """
    entities = dict(snapshot.entities)
    changed = []
    for change in record.changes:
        ref = EntityRef(change.entity_kind, change.entity_id)
        if change.after is None:
            entities.pop(ref, None)
        else:
            entities[ref] = change.after
            if change.entity_kind == STATUS_EVENT_KIND:
                event = status_event_from_state(change.entity_id, change.after)
                if event.status in (MealStatus.DELIVERY, MealStatus.STOPPED):
                    changed.append(event)
    after = HistoricalSnapshot(record.metadata.revision_id, entities)
    roster = roster_from_historical_snapshot(after)
    routing = routing_state_from_snapshot(after)
    calendar = school_lunch_calendar_from_snapshot(after)
    events_by_target = defaultdict(list)
    for event in status_events_from_historical_snapshot(after):
        events_by_target[(event.person_id, event.meal)].append(event)
    # Automatic same-month pause interpretation must not put explicitly stopped
    # recipients back into the route while saving another status change.
    histories = {
        target: resolve_status_history(*target, tuple(events), same_month_pause=False)
        for target, events in events_by_target.items()
    }
    groups = defaultdict(list)
    for event in changed:
        groups[(event.period.starts_on, event.meal)].append(event)
    values = {}
    for (day, meal), changes in sorted(groups.items(), key=lambda item: (item[0][0], item[0][1].value)):
        delivery = {event.person_id for event in changes if event.status is MealStatus.DELIVERY}
        stopped = {event.person_id for event in changes if event.status is MealStatus.STOPPED}
        affected_drivers = set()
        for event in changes:
            view = routing.resolve(roster, event.person_id, day)
            affected_drivers.add((view.driver_ids[meal], view.drivers[meal]))
        included = {}
        for person in roster.persons:
            pid = person.person_id
            status = effective_meal_status(roster, histories, calendar, pid, meal, day)
            if pid not in stopped and status in (MealStatus.DELIVERY, MealStatus.PAUSED):
                included[pid] = (meal,)
            elif status is None:
                view = routing.resolve(roster, pid, day)
                if (view.driver_ids[meal], view.drivers[meal]) in affected_drivers:
                    values[(pid, meal)] = ""
        rows = build_driver_household_rows(roster, routing, day, included_meals=included)
        for identity in affected_drivers:
            route = sorted(
                (row for row in rows if (row.driver_id, row.driver_name) == identity),
                key=lambda row: (bool(delivery.intersection(row.member_ids)), _sequence_key(row.sequences)),
            )
            for number, row in enumerate(route, 1):
                for pid in row.member_ids:
                    values[(pid, meal)] = str(number)
        for pid in stopped:
            values[(pid, meal)] = ""
    values = {
        target: value for target, value in values.items()
        if (roster.resolve_assignment(*target).sequence or "") != value
    }
    if values:
        prepared = build_scoped_sequence_revision(
            replace(roster, revision_id=snapshot.revision_id), values,
            actor=record.metadata.actor, recorded_at=record.metadata.recorded_at,
            revision_id=record.metadata.revision_id,
        )
        before = roster_from_historical_snapshot(snapshot)
        assignments = tuple(
            change for change in roster_audit_changes(before, prepared.snapshot)
            if change.entity_kind == ASSIGNMENT_KIND
        )
        record = replace(record, changes=(
            *(change for change in record.changes if change.entity_kind != ASSIGNMENT_KIND),
            *assignments,
        ))
    return replace(record, metadata=replace(
        record.metadata,
        decisions={**record.metadata.decisions, DECISION: {"policy_version": 1}},
    ))
