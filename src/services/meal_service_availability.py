"""Recurring meal availability shared by monthly billing and delivery reports."""

from __future__ import annotations

from datetime import date, timedelta

from src.domain.roster import Meal, Person, RosterSnapshot
from src.domain.school_calendar import SchoolLunchCalendar


RECURRING_MEAL_SERVICE_POLICY_VERSION = 4


def default_service_weekdays(meal: Meal, roster: RosterSnapshot | None = None) -> tuple[int, ...]:
    if roster is not None:
        return next(item.weekdays for item in roster.delivery_defaults if item.meal is meal)
    return (4, 5, 6) if meal is Meal.BREAKFAST else (0, 1, 2, 3, 4)


def default_delivery_weekdays(meal: Meal, roster: RosterSnapshot | None = None) -> tuple[int, ...]:
    if roster is not None:
        return next(item.delivery_weekdays for item in roster.delivery_defaults if item.meal is meal)
    return (0, 1, 2, 3, 4, 4, 4) if meal is Meal.BREAKFAST else tuple(range(7))


def physical_delivery_on(roster: RosterSnapshot, person: Person, meal: Meal, service_on: date) -> date:
    """Choose the latest configured weekday at or before the meal's service date."""
    setting = next((item for item in roster.delivery_settings_for(person.person_id, on=service_on) if item.meal is meal), None)
    mapping = setting.delivery_weekdays if setting and setting.delivery_weekdays is not None else default_delivery_weekdays(meal, roster)
    delivery_weekday = mapping[service_on.weekday()]
    return service_on - timedelta(days=(service_on.weekday() - delivery_weekday) % 7)


def regular_service_available(
    roster: RosterSnapshot,
    person: Person,
    meal: Meal,
    service_on: date,
    school_calendar: SchoolLunchCalendar,
) -> bool:
    """Apply a household's service weekdays before the school schedule."""

    setting = next(
        (
            item
            for item in roster.delivery_settings_for(person.person_id, on=service_on)
            if item.meal is meal
        ),
        None,
    )
    weekdays = setting.weekdays if setting is not None and setting.weekdays is not None else default_service_weekdays(meal, roster)
    if service_on.weekday() not in weekdays:
        return False
    return meal is not Meal.LUNCH or school_calendar.vendor_service_available(person.profile, service_on)
