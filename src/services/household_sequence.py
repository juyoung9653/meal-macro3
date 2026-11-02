"""Persist household route numbers with the same ordering as the route editor."""
from collections import defaultdict
from dataclasses import replace

from src.domain.delivery_routing import routing_state_from_snapshot
from src.domain.import_shared import import_candidate_snapshot
from src.domain.projections import roster_from_historical_snapshot, status_events_from_historical_snapshot
from src.domain.roster import Meal
from src.domain.sequence_edits import build_scoped_sequence_revision
from src.domain.status import MealStatus, resolve_status_history
from src.services.driver_households import build_driver_household_rows, sequence_sort_key


def synchronize_household_sequences(snapshot, candidate, record, on, household_ids):
    """Renumber affected driver/meal routes; None explicitly reapplies all routes.

    Filters and school calendars never change stored route positions. Stopped
    recipients keep their existing values and are not added to active routes.
    """
    roster = roster_from_historical_snapshot(candidate)
    events = defaultdict(list)
    for event in status_events_from_historical_snapshot(candidate):
        events[(event.person_id, event.meal)].append(event)
    histories = {key: resolve_status_history(*key, tuple(items), same_month_pause=False)
                 for key, items in events.items()}
    included = {
        person.person_id: tuple(meal for meal in Meal
            if (person.person_id, meal) not in histories
            or histories[(person.person_id, meal)].status_on(on) is not MealStatus.STOPPED)
        for person in roster.persons
    }
    rows = build_driver_household_rows(roster, routing_state_from_snapshot(candidate), on,
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
