"""Legacy daily-report memo propagation over authoritative roster/status facts.

The sampling, propagation and address grouping follow 일일식수매크로's
builder.py/memo.py; free-text parsing and Excel dependencies are not imported.
"""

from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
import re
from uuid import UUID

from src.domain.delivery_routing import RoutingState
from src.domain.entitlements import PhysicalShipment
from src.domain.roster import Meal, RosterSnapshot
from src.domain.status import MealStatus, StatusHistory


def remark_sample_dates(day: date, meal: Meal, active_dates: set[date]) -> tuple[date, ...]:
    monday = day - timedelta(days=day.weekday())
    previous = next((
        candidate for offset in range(1, 23)
        if (candidate := monday - timedelta(days=offset)).weekday() < 5
        and candidate in active_dates
    ), None)
    dates = {previous} if previous is not None else set()
    dates.update(
        candidate for candidate in active_dates
        if (candidate == day if meal is Meal.BREAKFAST else monday <= candidate <= day)
    )
    return tuple(sorted(dates))


def build_status_remarks(
    roster: RosterSnapshot,
    routing: RoutingState,
    histories: Mapping[tuple[UUID, Meal], StatusHistory],
    day: date,
    meal: Meal,
    shipments: Sequence[PhysicalShipment],
    allowed_forms: set[str | None] | None,
) -> Mapping[str, str]:
    sampled = remark_sample_dates(day, meal, {
        shipment.delivery_on for shipment in shipments if shipment.meal is meal
    })
    grouped: defaultdict[str, defaultdict[str, defaultdict[str, set[str]]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(set))
    )
    enrollment_starts = {
        (item.person_id, item.meal, item.period.starts_on)
        for item in roster.enrollments
    }
    for person in roster.persons:
        history = histories.get((person.person_id, meal))
        if history is None:
            continue
        if allowed_forms is not None:
            form = next((setting.form for setting in roster.delivery_settings_for(
                person.person_id, on=day
            ) if setting.meal is meal), None)
            if form not in allowed_forms:
                continue
        action = ""
        for sampled_on in sampled:
            active = _active_action(roster, history, sampled_on, enrollment_starts)
            if active:
                action = active
            elif "일시중지" in action:
                action = "복원"
        if not action:
            continue
        view = routing.resolve(roster, person.person_id, day)
        # Memo-only people have no shipment and must not create a count failure.
        driver = view.drivers[meal].strip() or "미지정"
        area = view.jurisdiction or "미지정"
        if area == "위례동":
            address = " ".join(value for value in (view.road_address, view.detail_address) if value)
            area = _address_key(address) or area
        grouped[driver][action][area].add(person.profile.name)
    return {
        driver: "\n".join(
            f"{action}\n" + ", ".join(
                f"{area}({', '.join(sorted(names))})"
                for area, names in sorted(areas.items())
            )
            for action, areas in sorted(actions.items())
        )
        for driver, actions in grouped.items()
    }


def _active_action(
    roster: RosterSnapshot,
    history: StatusHistory,
    day: date,
    enrollment_starts: set[tuple[UUID, Meal, date | None]],
) -> str:
    if history.meal not in roster.meals_for(history.person_id, on=day):
        return ""
    for interval in history.intervals:
        if not interval.period.contains(day):
            continue
        start, end = interval.period.starts_on, interval.period.ends_on
        if interval.status is MealStatus.PAUSED:
            assert end is not None
            period = _month_day(start) if start is not None else ""
            if start != end:
                period += f"~{_month_day(end)}"
            return f"일시중지 {period}"
        if start == day and interval.status is MealStatus.STOPPED:
            return f"{interval.status.value} {_month_day(day)}~"
        # Effective intervals, rather than superseded raw events, own the action.
        if interval.status is MealStatus.STOPPED:
            return ""
        break
    if (history.person_id, history.meal, day) in enrollment_starts:
        return f"신규 {_month_day(day)}~"
    if any(
        interval.status is MealStatus.DELIVERY and interval.period.starts_on == day
        and any(
            event.event_id in interval.source_event_ids
            and event.status is MealStatus.DELIVERY and event.period.starts_on == day
            for event in history.events
        )
        for interval in history.intervals
    ):
        return f"배송 {_month_day(day)}~"
    return ""


def _month_day(day: date) -> str:
    return f"{day.month}/{day.day}"


def _address_key(address: str) -> str:
    if match := re.search(r"\b(\d{3,4}-\d{3,4})\b", address):
        return match.group(1)
    if match := re.search(r"(\d+)\s*동\s*(\d+)\s*호", address):
        return f"{match.group(1)}동 {match.group(2)}호"
    if match := re.search(r"(\d+)\s*호", address):
        return f"{match.group(1)}호"
    return ""
