"""Plain-text invoice notes from current, visible meal history only."""

from collections import defaultdict
from dataclasses import replace
from datetime import date

from src.domain.roster import InclusiveDateRange, Meal
from src.domain.status import MealStatus
from src.services.monthly_meal_history import build_monthly_meal_history


def invoice_history_notes(snapshot, revisions, starts_on, ends_on, meals):
    if not revisions:
        return {}
    # Read the full effective spans so a pause beginning in the previous month
    # can be joined to the stop that supersedes it. The monthly UI keeps raw rows.
    history = build_monthly_meal_history(snapshot, tuple(revisions), starts_on.year, starts_on.month,
        include_all_periods=True)
    selected_period = InclusiveDateRange(starts_on, ends_on)
    rows = defaultdict(list)
    order = (Meal.DINNER, Meal.BREAKFAST, Meal.LUNCH)
    for entry in sorted(_merged_invoice_entries(history.entries, selected_period),
                        key=lambda item: (order.index(item.meal), item.period.starts_on or starts_on)):
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


def _merged_invoice_entries(entries, selected):
    grouped = defaultdict(list)
    for entry in entries:
        grouped[entry.person_id, entry.meal].append(entry)
    result = []
    for items in grouped.values():
        merged = []
        for entry in sorted(items, key=lambda item: item.period.starts_on or date.min):
            start, end = entry.period.starts_on, entry.period.ends_on
            source = entry.source_period or entry.period
            visible = entry.period.overlaps(selected) and (
                source.ends_on is not None or (source.starts_on is not None and
                    (source.starts_on.year, source.starts_on.month) == (selected.starts_on.year, selected.starts_on.month)))
            if merged:
                previous, previous_visible = merged[-1]
                previous_end = previous.period.ends_on
                continuous = previous_end is not None and start is not None and start.toordinal() <= previous_end.toordinal() + 1
                if (continuous and previous.status is MealStatus.PAUSED
                        and entry.status in (MealStatus.PAUSED, MealStatus.STOPPED)
                        and (entry.status is not MealStatus.STOPPED or entry.period.overlaps(selected))):
                    merged[-1] = (replace(entry, period=InclusiveDateRange(previous.period.starts_on, end)),
                                  previous_visible or visible)
                    continue
            merged.append((entry, visible))
        result.extend(entry for entry, visible in merged if visible)
    return result
