"""Plain-text invoice notes from current, visible meal history only."""

from collections import defaultdict

from src.domain.roster import InclusiveDateRange, Meal
from src.services.monthly_meal_history import build_monthly_meal_history


def invoice_history_notes(snapshot, revisions, starts_on, ends_on, meals):
    if not revisions:
        return {}
    history = build_monthly_meal_history(snapshot, tuple(revisions), starts_on.year, starts_on.month)
    selected_period = InclusiveDateRange(starts_on, ends_on)
    rows = defaultdict(list)
    order = (Meal.DINNER, Meal.BREAKFAST, Meal.LUNCH)
    for entry in sorted(history.entries, key=lambda item: (order.index(item.meal), item.period.starts_on or starts_on)):
        if entry.meal not in meals or not entry.period.overlaps(selected_period):
            continue
        first = entry.period.starts_on
        last = entry.period.ends_on
        start = "시작일 미지정" if first is None else f"{first.month}/{first.day}"
        end = "" if last is None else f"{last.month}/{last.day}"
        label = entry.status.value
        text = f"{entry.meal.value}: {start}~{end} {label}"
        if text not in rows[entry.person_id]:
            rows[entry.person_id].append(text)
    return {person_id: "\n".join(lines) for person_id, lines in rows.items()}
