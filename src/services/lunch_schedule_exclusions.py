"""Readable exclusions shared by completion and empty-output diagnostics."""


def lunch_schedule_exclusion_lines(manifest):
    physical = manifest.get("physical_delivery", manifest)
    return tuple(
        f"{item['school_name']} · {item['person_count']}명 · "
        f"{', '.join(item['service_dates'])} ({item['meal_count']}식 제외)"
        for item in physical.get("missing_lunch_schedules", ())
    )
