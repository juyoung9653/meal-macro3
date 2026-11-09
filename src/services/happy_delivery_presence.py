"""Shared status-only recipient counts for happy lunchbox lists and reports."""
from dataclasses import dataclass
from datetime import date, timedelta
from uuid import UUID

from src.domain.roster import Meal
from src.domain.projections import status_events_from_historical_snapshot
from src.domain.pause_conversion import pause_conversion_from_snapshot
from src.domain.status import MealStatus, resolve_status_history


@dataclass(frozen=True)
class HappyRecipientPresence:
    """One recipient for display; this is not a physical shipment or meal charge."""
    person_id: UUID
    meal: Meal
    delivery_on: date

    @property
    def service_dates(self):
        return (self.delivery_on,)

    @property
    def entitlement_count(self):
        return 1


def happy_recipient_presences(snapshot, roster, starts_on, ends_on, meals):
    events = {}
    for event in status_events_from_historical_snapshot(snapshot):
        events.setdefault((event.person_id, event.meal), []).append(event)
    same_month = pause_conversion_from_snapshot(snapshot).same_month
    histories = {(person.person_id, meal): resolve_status_history(person.person_id, meal,
        tuple(events.get((person.person_id, meal), ())), same_month_pause=same_month)
        for person in roster.persons for meal in meals}
    result = []
    day = starts_on
    while day <= ends_on:
        for person in roster.persons:
            enrolled = roster.meals_for(person.person_id, on=day)
            for meal in meals:
                if meal in enrolled and histories[person.person_id, meal].status_on(day) is not MealStatus.STOPPED:
                    result.append(HappyRecipientPresence(person.person_id, meal, day))
        day += timedelta(days=1)
    return tuple(result)
