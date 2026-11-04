"""Persist household route numbers with the same ordering as the route editor."""
from collections import defaultdict
from dataclasses import replace

from src.domain.delivery_routing import routing_state_from_snapshot
from src.domain.import_shared import import_candidate_snapshot
from src.domain.projections import roster_from_historical_snapshot, status_events_from_historical_snapshot
from src.domain.roster import Meal
from src.domain.meal_status_projection import effective_meal_status
from src.domain.pause_conversion import pause_conversion_from_snapshot
from src.domain.school_calendar import school_lunch_calendar_from_snapshot
from src.domain.sequence_edits import build_scoped_sequence_revision
from src.domain.status import MealStatus, resolve_status_history
from src.services.driver_households import build_driver_household_rows, sequence_sort_key


def synchronize_household_sequences(snapshot, candidate, record, on, household_ids):
    """Renumber affected driver/meal routes; None explicitly reapplies all routes.

    Apply the route editor's enrollment, status and school-calendar eligibility.
    Missing/ineligible meals are cleared in scope, including legacy common values.
    Search and column filters never change stored route positions.
    """
    roster = roster_from_historical_snapshot(candidate)
    events = defaultdict(list)
    for event in status_events_from_historical_snapshot(candidate):
        events[(event.person_id, event.meal)].append(event)
    conversion = pause_conversion_from_snapshot(candidate)
    histories = {key: resolve_status_history(*key, tuple(items), same_month_pause=conversion.same_month)
                 for key, items in events.items()}
    calendar = school_lunch_calendar_from_snapshot(candidate)
    statuses = {(p.person_id, meal): effective_meal_status(
        roster, histories, calendar, p.person_id, meal, on,
    ) for p in roster.persons for meal in Meal}
    included = {
        person.person_id: tuple(meal for meal in Meal
            if statuses[(person.person_id, meal)] in (MealStatus.DELIVERY, MealStatus.PAUSED))
        for person in roster.persons
    }
    routing = routing_state_from_snapshot(candidate)
    rows = build_driver_household_rows(roster, routing, on,
                                      included_meals=included)
    routes = defaultdict(list)
    affected = set()
    for row in rows:
        for part in row.meal_rows:
            identity = (part.driver_id, part.driver_name, part.meals[0])
            routes[identity].append(part)
            if household_ids is None or part.household_key in household_ids:
                affected.add(identity)
    values = {}
    for person in roster.persons:
        pid = person.person_id
        view = routing.resolve(roster, pid, on)
        household = roster.current_membership(pid).household_id
        for meal in Meal:
            in_scope = (household_ids is None or household in household_ids
                        or (view.driver_ids[meal], view.drivers[meal], meal) in affected)
            if in_scope and statuses[(pid, meal)] is None and roster.resolve_assignment(pid, meal).sequence:
                values[(pid, meal)] = ""
    for identity in affected:
        route = sorted(routes[identity], key=lambda row: sequence_sort_key(row.sequences))
        for number, row in enumerate(route, 1):
            for pid in row.member_ids:
                meal = row.meals[0]
                if (roster.resolve_assignment(pid, meal).sequence or "") != str(number):
                    values[(pid, meal)] = str(number)
    if not values:
        return candidate
    prepared = build_scoped_sequence_revision(
        replace(roster, revision_id=snapshot.revision_id), values,
        actor=record.metadata.actor, recorded_at=record.metadata.recorded_at,
        revision_id=record.metadata.revision_id,
    )
    return import_candidate_snapshot(candidate, prepared.record)
