"""One eligibility rule for the roster, route editor and persisted route numbers."""
from .roster import Meal
from .school_calendar import SchoolLunchScheduleMissingError
from .status import MealStatus


def effective_meal_status(roster, histories, calendar, person_id, meal, day, *, enrolled_meals=None):
    """Resolve display eligibility without applying recurring delivery weekdays."""
    meals = roster.meals_for(person_id, on=day) if enrolled_meals is None else enrolled_meals
    if meal not in meals:
        return None
    if meal is Meal.LUNCH:
        try:
            if not calendar.vendor_service_available(roster.person(person_id).profile, day):
                return None
        except SchoolLunchScheduleMissingError:
            return None
    history = histories.get((person_id, meal))
    return MealStatus.DELIVERY if history is None else history.status_on(day)
