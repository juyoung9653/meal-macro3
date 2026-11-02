"""Nonzero delivery reference days within each report's Monday-Sunday week."""
from bisect import bisect_left, bisect_right
from datetime import timedelta


def reference_delivery_days(shipments, starts_on, ends_on, meals):
    """Prefer today/past in the same week, then its first future delivery."""
    dates = {meal: sorted({item.delivery_on for item in shipments
                          if item.meal is meal and item.entitlement_count > 0})
             for meal in meals}
    result = {}
    day = starts_on
    while day <= ends_on:
        week_start = day - timedelta(days=day.weekday())
        week_end = week_start + timedelta(days=6)
        for meal in meals:
            candidates = dates[meal]
            first = bisect_left(candidates, week_start)
            after_today = bisect_right(candidates, day)
            if after_today > first:
                reference = candidates[after_today - 1]
            elif first < len(candidates) and candidates[first] <= week_end:
                reference = candidates[first]
            else:
                reference = None
            result[day, meal] = reference
        day += timedelta(days=1)
    return result
