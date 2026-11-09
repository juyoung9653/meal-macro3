"""Happy lunchbox route totals from the current SQLite delivery contract."""
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from types import SimpleNamespace

from src.domain.daily_report_choices import ReportRemarkOptions
from src.domain.daily_driver_order import daily_driver_orders
from src.domain.pause_conversion import pause_conversion_from_snapshot
from src.domain.projections import status_events_from_historical_snapshot
from src.domain.roster import Meal
from src.domain.status import resolve_status_history
from src.services.daily_report_mapping import DailyReportMapping
from src.services.happy_delivery_presence import happy_recipient_presences
from src.services.delivery_households import require_delivery_household
from src.services.delivery_report_remarks import build_additional_remarks, report_remark_columns
from src.services.sqlite_delivery_report_service import delivery_forms_from_snapshot


@dataclass(frozen=True)
class DeliveryRoute:
    driver: str
    region: str


@dataclass
class DailyDeliveryRow:
    route: DeliveryRoute
    breakfast: int = 0
    dinner: int = 0
    lunch: int = 0
    dinner_packages: str = ""
    note: str = ""
    dinner_note: str | None = None
    breakfast_note: str | None = None
    lunch_note: str | None = None
    dinner_order: int = 0
    lunch_order: int = 0
    _dinner_household_keys: list[str] = field(default_factory=list, repr=False)
    _lunch_household_keys: list[str] = field(default_factory=list, repr=False)


@dataclass(frozen=True)
class DailyDeliverySheet:
    delivery_date: date
    rows: tuple[DailyDeliveryRow, ...]
    breakfast_total: int
    dinner_total: int
    lunch_total: int
    show_term: bool = False
    breakfast_on: date | None = None
    dinner_on: date | None = None
    lunch_on: date | None = None
    show_lunch: bool = False


@dataclass(frozen=True)
class PreparedHappyReports:
    reports: tuple[DailyDeliverySheet, ...]
    shipments: tuple
    calculation_manifest: dict


def prepare_happy_report_data(snapshot, period_start, period_end, meals, *,
                              mapping_snapshot=None, delivery_forms=None,
                              remark_options=ReportRemarkOptions()):
    if type(period_start) is not date or type(period_end) is not date or period_start > period_end:
        raise ValueError("일일식수의 시작일과 종료일을 확인해주세요.")
    meals = tuple(meals)
    if not meals or any(not isinstance(meal, Meal) for meal in meals) or len(set(meals)) != len(meals):
        raise ValueError("일일식수에 포함할 식종을 중복 없이 선택해주세요.")
    mapping_snapshot = snapshot if mapping_snapshot is None else mapping_snapshot
    report_mapping = DailyReportMapping(snapshot, mapping_snapshot, "행복도시락")
    roster = report_mapping.roster
    routing = report_mapping.routing
    if not isinstance(remark_options, ReportRemarkOptions) or set(remark_options.columns) - {
        key for key, _ in report_remark_columns(roster)
    }:
        raise ValueError("일일식수 비고 항목을 확인해주세요.")
    allowed = None if delivery_forms is None else set(delivery_forms)
    if allowed is not None and (not allowed or allowed - set(delivery_forms_from_snapshot(snapshot))):
        raise ValueError("일일식수 배송 형태를 확인해주세요.")
    sample_start = period_start - timedelta(days=28)
    remark_shipments = happy_recipient_presences(snapshot, roster, sample_start, period_end, meals)

    def form_for(shipment):
        return next((item.form for item in roster.delivery_settings_for(shipment.person_id,
            on=shipment.service_dates[0]) if item.meal is shipment.meal), None)

    shipments = tuple(item for item in remark_shipments if allowed is None or form_for(item) in allowed)
    values_for = report_mapping.values_for

    grouped = {}
    presences = defaultdict(list)
    for shipment in shipments:
        if shipment.delivery_on < period_start:
            continue
        values = values_for(shipment.person_id, shipment.delivery_on, shipment.meal)
        key = shipment.delivery_on, values["driver"], values["region"]
        row = grouped.setdefault(key, DailyDeliveryRow(DeliveryRoute(key[1], key[2])))
        field_name = {Meal.BREAKFAST: "breakfast", Meal.DINNER: "dinner", Meal.LUNCH: "lunch"}[shipment.meal]
        setattr(row, field_name, getattr(row, field_name) + 1)
        household_id = require_delivery_household(roster, shipment.person_id, shipment.service_dates)
        if shipment.meal in (Meal.DINNER, Meal.LUNCH):
            keys = row._lunch_household_keys if shipment.meal is Meal.LUNCH else row._dinner_household_keys
            keys.append(str(household_id))
        presences[shipment.delivery_on, shipment.meal].append(SimpleNamespace(
            person_id=shipment.person_id, driver=values["driver"], form=form_for(shipment),
            sequence=roster.resolve_assignment(shipment.person_id, shipment.meal).sequence or ""))
    events = defaultdict(list)
    for event in status_events_from_historical_snapshot(snapshot):
        events[event.person_id, event.meal].append(event)
    conversion = pause_conversion_from_snapshot(snapshot)
    histories = {(person.person_id, meal): resolve_status_history(person.person_id, meal,
        tuple(events[person.person_id, meal]), same_month_pause=conversion.same_month)
        for person in roster.persons for meal in meals}
    extra_notes = defaultdict(list)
    for day, meal in presences:
        status = report_mapping.remarks_for(histories, day, meal, remark_shipments,
            None if remark_options.include_excluded_deliveries else allowed, presences[day, meal])
        additional = build_additional_remarks(roster, report_mapping.for_meal(meal), histories, day, meal,
            presences[day, meal], remark_options,
            form_shipments=remark_shipments if remark_options.include_excluded_deliveries else shipments)
        for driver in status.keys() | additional.keys():
            parts = (additional.get(driver, ""), status.get(driver, "")) if remark_options.position == "above" else (
                status.get(driver, ""), additional.get(driver, ""))
            extra_notes[day, driver, meal].extend(part for part in parts if part)
    ranks = {driver.name: index for index, driver in enumerate(routing.drivers)}
    if routing.driver_order is not None:
        by_id = {driver.driver_id: driver for driver in routing.drivers}
        ranks = {by_id[driver_id].name: index for index, driver_id in enumerate(routing.driver_order)}
    drivers = {driver.driver_id: driver.name for driver in routing.drivers}
    meal_ranks = {meal: {drivers[driver_id]: index for index, driver_id in enumerate(order)}
                  for meal, order in daily_driver_orders(snapshot, routing).items()}
    reports = []
    for day in sorted({key[0] for key in grouped}):
        keys = sorted((key for key in grouped if key[0] == day),
            key=lambda key: (ranks.get(key[1], len(ranks)), key[1]))
        seen = set()
        dinner_meal = Meal.DINNER if any(grouped[key].dinner for key in keys) else Meal.BREAKFAST
        for key in keys:
            row = grouped[key]
            row.dinner_order = meal_ranks[dinner_meal].get(row.route.driver, len(drivers))
            row.lunch_order = meal_ranks[Meal.LUNCH].get(row.route.driver, len(drivers))
            row.dinner_packages = "/".join(f"{size}*{count}" for size, count in
                sorted(Counter(Counter(row._dinner_household_keys).values()).items(), reverse=True))
            for meal, variant, count in ((Meal.BREAKFAST, "breakfast", row.breakfast),
                                         (Meal.DINNER, "dinner", row.dinner), (Meal.LUNCH, "lunch", row.lunch)):
                parts = []
                marker = row.route.driver, meal
                if count and marker not in seen:
                    parts.extend(extra_notes[day, row.route.driver, meal])
                    seen.add(marker)
                setattr(row, variant + "_note", "\n".join(dict.fromkeys(parts)))
        rows = tuple(grouped[key] for key in keys)
        reports.append(DailyDeliverySheet(day, rows, sum(row.breakfast for row in rows),
            sum(row.dinner for row in rows), sum(row.lunch for row in rows)))
    references = {(item.delivery_on, item.meal): item.delivery_on for item in shipments
                  if item.delivery_on >= period_start}
    by_day = {report.delivery_date: report for report in reports}
    display_reports = []
    show_term = any(enrollment.meal in meals and enrollment.meal in (Meal.BREAKFAST, Meal.DINNER)
                    for enrollment in roster.enrollments)
    show_lunch = Meal.LUNCH in meals and any(
        enrollment.meal is Meal.LUNCH for enrollment in roster.enrollments
    )
    day = period_start
    while day <= period_end:
        displayed = {}
        for meal in meals:
            reference = references.get((day, meal), day)
            original = by_day.get(reference)
            if original is None:
                continue
            for row in original.rows:
                count = getattr(row, {Meal.BREAKFAST: "breakfast", Meal.DINNER: "dinner", Meal.LUNCH: "lunch"}[meal])
                if not count:
                    continue
                key = row.route.driver, row.route.region
                target = displayed.setdefault(key, DailyDeliveryRow(row.route))
                if meal is Meal.BREAKFAST:
                    target.breakfast = count
                    if not target.dinner:
                        target.dinner_order = row.dinner_order
                elif meal is Meal.DINNER:
                    target.dinner = count
                    target.dinner_packages = row.dinner_packages
                    target._dinner_household_keys = list(row._dinner_household_keys)
                    target.dinner_order = row.dinner_order
                else:
                    target.lunch = count
                    target.lunch_order = row.lunch_order
                    target._lunch_household_keys = list(row._lunch_household_keys)
                note_field = "lunch_note" if meal is Meal.LUNCH else "dinner_note"
                notes = (getattr(target, note_field), getattr(row, "breakfast_note" if meal is Meal.BREAKFAST else note_field))
                setattr(target, note_field, "\n".join(dict.fromkeys(note for note in notes if note)))
        rows = tuple(displayed.values())
        if show_term or show_lunch:
            display_reports.append(DailyDeliverySheet(day, rows, sum(row.breakfast for row in rows),
                sum(row.dinner for row in rows), sum(row.lunch for row in rows), show_term,
                breakfast_on=references.get((day, Meal.BREAKFAST)),
                dinner_on=references.get((day, Meal.DINNER)),
                lunch_on=references.get((day, Meal.LUNCH)), show_lunch=show_lunch))
        day += timedelta(days=1)
    target_shipments = tuple(item for item in shipments if period_start <= item.delivery_on <= period_end)
    manifest = {"happy_report_contract_version": 5,
        "source_revision_id": str(snapshot.revision_id),
        "recipient_basis": "enrolled_delivery_or_paused",
        "display_reference_days": [{"date": report.delivery_date.isoformat(),
            "breakfast": report.breakfast_on.isoformat() if report.breakfast_on else None,
            "dinner": report.dinner_on.isoformat() if report.dinner_on else None,
            "lunch": report.lunch_on.isoformat() if report.lunch_on else None} for report in display_reports],
        "report_period_start": period_start.isoformat(), "report_period_end": period_end.isoformat(),
        "selected_delivery_forms": None if allowed is None else list(delivery_forms),
        "remarks": remark_options.manifest(),
        "column_mapping": report_mapping.manifest(),
        "household_grouping": "database_household", "quantity_basis": "one_per_enrolled_recipient",
        "breakfast_quantity_basis": "one_per_enrolled_recipient"}
    return PreparedHappyReports(tuple(display_reports), target_shipments, manifest)
