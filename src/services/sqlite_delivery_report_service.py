from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from types import MappingProxyType
from uuid import UUID

from src.core.diff_format import fmt_cell
from src.domain.entitlements import (
    BreakfastDeliveryRule,
    ClosedFridayMode,
    MealEntitlement,
    PhysicalDeliveryCalendar,
    PhysicalShipment,
    plan_breakfast_shipments,
    decide_entitlement,
)
from src.domain.delivery_defaults import delivery_defaults_state
from src.domain.delivery_routing import RoutingState, routing_state_from_snapshot
from src.domain.projections import (
    roster_from_historical_snapshot,
    status_events_from_historical_snapshot,
)
from src.domain.revisions import (
    HistoricalSnapshot,
    JsonValue,
    freeze_json_object,
    plain_json,
)
from src.domain.roster import Meal, RosterSnapshot
from src.domain.school_calendar import (
    SchoolLunchCalendarError,
    SchoolLunchScheduleMissingError,
    school_lunch_calendar_from_snapshot,
)
from src.domain.service_adjustments import (
    SERVICE_ADJUSTMENT_POLICY_VERSION,
    ServiceAdjustmentSchedule,
    ServiceTreatment,
    service_adjustment_schedule_from_snapshot,
    service_adjustment_schedule_state,
)
from src.domain.status import StatusEvent, resolve_status_history
from src.services.delivery_service import (
    DeliveryReport,
    DeliveryReportRow,
    DeliveryReportTotals,
)
from src.services.meal_service_availability import (
    RECURRING_MEAL_SERVICE_POLICY_VERSION,
    default_delivery_weekdays,
    physical_delivery_on,
    regular_service_available,
)


DELIVERY_REPORT_CALCULATION_CONTRACT_VERSION = 3
DELIVERY_REPORT_GROUPING_POLICY_VERSION = 2


@dataclass(frozen=True, slots=True)
class PreparedDeliveryReportData:
    """Immutable delivery reports and the facts needed to reproduce them."""

    reports: tuple[DeliveryReport, ...]
    shipments: tuple[PhysicalShipment, ...]
    calculation_manifest: Mapping[str, JsonValue]

    def __post_init__(self) -> None:
        reports = tuple(self.reports)
        shipments = tuple(self.shipments)
        if any(not isinstance(item, DeliveryReport) for item in reports):
            raise TypeError("reports에는 DeliveryReport만 허용됩니다.")
        if any(not isinstance(item, PhysicalShipment) for item in shipments):
            raise TypeError("shipments에는 PhysicalShipment만 허용됩니다.")
        object.__setattr__(self, "reports", reports)
        object.__setattr__(self, "shipments", shipments)
        object.__setattr__(
            self,
            "calculation_manifest",
            freeze_json_object(self.calculation_manifest),
        )


@dataclass(frozen=True, slots=True)
class _ShipmentPresence:
    person_id: UUID
    household_id: UUID
    driver: str
    sequence: str
    jurisdiction: str
    form: str | None
    entitlement_count: int


@dataclass(frozen=True, slots=True)
class _DeliveryAggregate:
    buckets_by_staff: Mapping[str, tuple[int, int, int, int, int]]
    areas_by_staff: Mapping[str, frozenset[str]]
    forms_by_staff: Mapping[str, Mapping[str | None, tuple[int, int]]]


_EMPTY_BUCKETS = (0, 0, 0, 0, 0)


def prepare_delivery_report_data(
    snapshot: HistoricalSnapshot,
    period_start: date,
    period_end: date,
    meals: Sequence[Meal],
    *,
    staff_sort: Mapping[Meal | str, Sequence[str]] | None = None,
) -> PreparedDeliveryReportData:
    """Build report rows from entitlement service dates and physical shipments.

    The report range is a physical-delivery range.  Breakfast weekend meals and
    replacement meals can therefore be represented on a different day from the
    entitlement's original service date.
    """

    return _prepare_delivery_data(
        snapshot, period_start, period_end, meals, staff_sort=staff_sort, build_reports=True
    )


def prepare_physical_delivery_data(
    snapshot: HistoricalSnapshot,
    period_start: date,
    period_end: date,
    meals: Sequence[Meal],
    *,
    missing_lunch_schedule_as_zero: bool = False,
) -> PreparedDeliveryReportData:
    """Prepare physical shipments without report grouping or driver requirements.

    ``reports`` is empty; shipments and calculation provenance use the same
    entitlement rules and evaluation window as delivery reports.
    Monthly statistics may omit lunch on dates without a reviewed schedule.
    """
    return _prepare_delivery_data(
        snapshot, period_start, period_end, meals, staff_sort=None, build_reports=False,
        missing_lunch_schedule_as_zero=missing_lunch_schedule_as_zero,
    )


def _prepare_delivery_data(
    snapshot: HistoricalSnapshot,
    period_start: date,
    period_end: date,
    meals: Sequence[Meal],
    *,
    staff_sort: Mapping[Meal | str, Sequence[str]] | None,
    build_reports: bool,
    missing_lunch_schedule_as_zero: bool = False,
) -> PreparedDeliveryReportData:
    if not isinstance(snapshot, HistoricalSnapshot):
        raise TypeError("snapshot은 HistoricalSnapshot이어야 합니다.")
    _validate_date(period_start, "일일식수 시작일")
    _validate_date(period_end, "일일식수 종료일")
    if period_start > period_end:
        raise ValueError("일일식수 시작일은 종료일보다 늦을 수 없습니다.")
    selected_meals = _canonical_meals(meals)
    normalized_staff_sort = _normalize_staff_sort(staff_sort)

    roster = roster_from_historical_snapshot(snapshot)
    routing = routing_state_from_snapshot(snapshot)
    school_calendar = school_lunch_calendar_from_snapshot(snapshot)
    adjustments = service_adjustment_schedule_from_snapshot(snapshot)
    # Retired policy records remain in the ledger, but reports use this fixed rule.
    breakfast_rule = BreakfastDeliveryRule(
        "breakfast-delivery-policy-v1@default",
        ClosedFridayMode.THURSDAY_WEEKEND_ONLY,
    )
    status_events = status_events_from_historical_snapshot(snapshot)
    events_by_key: defaultdict[tuple[UUID, Meal], list[StatusEvent]] = defaultdict(list)
    for event in status_events:
        events_by_key[(event.person_id, event.meal)].append(event)

    comparison_start = date(period_start.year, 1, 1)
    normal_evaluation_end = period_end + timedelta(days=6)
    service_days = set(_days(comparison_start, normal_evaluation_end))
    service_days.update(
        _replacement_service_days(
            adjustments,
            selected_meals,
            comparison_start,
            period_end,
        )
    )
    ordered_service_days = tuple(sorted(service_days))
    physical_calendar = _breakfast_calendar(
        adjustments,
        ordered_service_days,
    )

    normal_breakfast: list[MealEntitlement] = []
    shipments: list[PhysicalShipment] = []
    exclusions: Counter[str] = Counter()
    for person in roster.persons:
        for meal in selected_meals:
            history = resolve_status_history(
                person.person_id,
                meal,
                tuple(events_by_key[(person.person_id, meal)]),
            )
            for service_on in ordered_service_days:
                if meal not in roster.meals_for(person.person_id, on=service_on):
                    continue
                provision = adjustments.provision_for(meal, service_on)
                candidate_delivery = _candidate_delivery(
                    meal,
                    provision.treatment,
                    provision.delivery_on,
                    service_on,
                )
                if provision.treatment is ServiceTreatment.NORMAL:
                    candidate_delivery = physical_delivery_on(roster, person, meal, service_on)
                try:
                    available = regular_service_available(
                        roster,
                        person,
                        meal,
                        service_on,
                        school_calendar,
                    )
                except SchoolLunchCalendarError as error:
                    if (missing_lunch_schedule_as_zero and meal is Meal.LUNCH
                            and isinstance(error, SchoolLunchScheduleMissingError)):
                        continue
                    if candidate_delivery is not None and _contains(
                        period_start,
                        period_end,
                        candidate_delivery,
                    ):
                        raise
                    # Earlier dates are used only for a best-effort previous-day
                    # comparison.  A schedule gap there must not block the chosen
                    # report period, whose own dates remain strict.
                    continue
                decision = decide_entitlement(
                    roster,
                    person.person_id,
                    meal,
                    service_on,
                    status_history=history,
                    service_available=available,
                    service_provision=provision,
                )
                entitlement = decision.entitlement
                if entitlement is None:
                    if period_start <= service_on <= normal_evaluation_end:
                        exclusions.update(item.value for item in decision.exclusions)
                    continue
                if provision.treatment is ServiceTreatment.REPLACEMENT:
                    assert provision.delivery_on is not None
                    shipments.append(
                        PhysicalShipment(
                            person.person_id,
                            meal,
                            provision.delivery_on,
                            (service_on,),
                        )
                    )
                elif meal is Meal.BREAKFAST and (
                    physical_delivery_on(roster, person, meal, service_on)
                    == service_on - timedelta(days=(service_on.weekday() - default_delivery_weekdays(meal)[service_on.weekday()]) % 7)
                ):
                    normal_breakfast.append(entitlement)
                else:
                    delivery_on = physical_delivery_on(roster, person, meal, service_on)
                    if adjustments.provision_for(meal, delivery_on).treatment is not ServiceTreatment.NORMAL:
                        exclusions["delivery_day_closed"] += 1
                        continue
                    shipments.append(
                        PhysicalShipment(
                            person.person_id,
                            meal,
                            delivery_on,
                            (service_on,),
                        )
                    )

    breakfast_plans = (
        (plan_breakfast_shipments(normal_breakfast, physical_calendar, breakfast_rule),)
        if normal_breakfast
        else ()
    )
    shipments.extend(
        shipment
        for plan in breakfast_plans
        for shipment in plan.shipments
    )
    unassigned = Counter(
        item.reason.value
        for plan in breakfast_plans
        for item in plan.unassigned
        if period_start <= item.entitlement.service_on <= normal_evaluation_end
    )
    coalesced_shipments = _coalesce_shipments(
        item
        for item in shipments
        if comparison_start <= item.delivery_on <= period_end
    )

    reports: tuple[DeliveryReport, ...] = ()
    if build_reports:
        target_keys = tuple(
            sorted(
                {
                    (item.delivery_on, item.meal)
                    for item in coalesced_shipments
                    if period_start <= item.delivery_on <= period_end
                },
                key=lambda item: (item[0], tuple(Meal).index(item[1])),
            )
        )
        previous_by_key = _previous_delivery_keys(coalesced_shipments, target_keys)
        relevant_keys = set(target_keys) | set(previous_by_key.values())
        relevant_shipments = tuple(
            item
            for item in coalesced_shipments
            if (item.delivery_on, item.meal) in relevant_keys
        )
        presences = _shipment_presences(roster, relevant_shipments, routing)
        aggregates = {
            key: _aggregate_presences(
                presences.get(key, ()),
                roster,
                context=f"{key[0]:%Y%m%d} {key[1].value}",
            )
            for key in relevant_keys
        }
        reports = tuple(
            _build_report(
                day,
                meal,
                aggregates[(day, meal)],
                aggregates.get(previous_by_key.get((day, meal)), _empty_aggregate()),
                normalized_staff_sort.get(meal, ()),
            )
            for day, meal in target_keys
        )

    target_shipments = tuple(
        item
        for item in coalesced_shipments
        if period_start <= item.delivery_on <= period_end
    )
    manifest = freeze_json_object(
        {
            "calculation_contract_version": (
                DELIVERY_REPORT_CALCULATION_CONTRACT_VERSION
            ),
            "source_revision_id": str(snapshot.revision_id),
            "selected_meals": tuple(meal.value for meal in selected_meals),
            "report_period": {
                "basis": "physical_delivery_day",
                "starts_on": period_start.isoformat(),
                "ends_on": period_end.isoformat(),
            },
            "comparison": {
                "basis": "previous_physical_delivery_in_same_calendar_year",
                "starts_on": comparison_start.isoformat(),
            },
            "recurring_service_policy": {
                "version": RECURRING_MEAL_SERVICE_POLICY_VERSION,
                "defaults": delivery_defaults_state(roster.delivery_defaults),
                "lunch": "configured_days_and_reviewed_school_weekdays",
                "household_weekday_override": "replaces_regular_days; lunch_requires_school_service",
                "household_delivery_weekday": "latest_configured_weekday_on_or_before_service_day",
            },
            "breakfast_physical_delivery": {
                "policy": {
                    "policy_version": 1,
                    "default_closed_friday_mode": breakfast_rule.closed_friday_mode.value,
                    "change_count": 0,
                    "content_sha256": _state_sha256(
                        {
                            "policy_version": 1,
                            "default_closed_friday_mode": breakfast_rule.closed_friday_mode.value,
                            "changes": (),
                        }
                    ),
                },
                "used_rule_versions": tuple(
                    plan.rule_version for plan in breakfast_plans
                ),
                "used_closed_friday_modes": tuple(
                    sorted(
                        {
                            breakfast_rule.closed_friday_mode.value
                            for item in normal_breakfast
                        }
                    )
                ),
                "unassigned": dict(sorted(unassigned.items())),
            },
            "school_lunch_calendar": school_calendar.manifest(),
            "service_adjustments": {
                "policy_version": SERVICE_ADJUSTMENT_POLICY_VERSION,
                "content_sha256": _state_sha256(
                    service_adjustment_schedule_state(adjustments)
                ),
                "relevant_change_ids": _relevant_adjustment_ids(
                    adjustments,
                    selected_meals,
                    comparison_start,
                    normal_evaluation_end,
                    period_start,
                    period_end,
                ),
            },
            "household_grouping": {
                "policy_version": DELIVERY_REPORT_GROUPING_POLICY_VERSION,
                "key": "stable_household_id",
                "max_supported_size": 5,
                "form_breakdown": "staff_memo_households_and_entitlements",
            },
            "staff_order": {
                meal.value: normalized_staff_sort.get(meal, ())
                for meal in selected_meals
            },
            "status_event_count": len(status_events),
            "exclusions": dict(sorted(exclusions.items())),
            "report_count": len(reports),
            "target_shipment_count": len(target_shipments),
            "target_entitlement_count": sum(
                item.entitlement_count for item in target_shipments
            ),
        }
    )
    if not build_reports:
        manifest = freeze_json_object({
            key: value for key, value in manifest.items()
            if key not in {"household_grouping", "staff_order", "report_count"}
        })
    return PreparedDeliveryReportData(reports, target_shipments, manifest)


def _replacement_service_days(
    schedule: ServiceAdjustmentSchedule,
    meals: tuple[Meal, ...],
    comparison_start: date,
    period_end: date,
) -> set[date]:
    result: set[date] = set()
    for change in schedule.changes:
        if change.treatment is not ServiceTreatment.REPLACEMENT:
            continue
        if not any(meal in change.meals for meal in meals):
            continue
        assert change.period.starts_on is not None
        assert change.period.ends_on is not None
        for service_on in _days(change.period.starts_on, change.period.ends_on):
            for meal in meals:
                if meal not in change.meals:
                    continue
                provision = schedule.provision_for(meal, service_on)
                if (
                    provision.treatment is ServiceTreatment.REPLACEMENT
                    and provision.delivery_on is not None
                    and comparison_start <= provision.delivery_on <= period_end
                ):
                    result.add(service_on)
    return result


def _breakfast_calendar(
    schedule: ServiceAdjustmentSchedule,
    service_days: Sequence[date],
) -> PhysicalDeliveryCalendar:
    probe_days: set[date] = set()
    for service_on in service_days:
        probe_days.add(service_on)
        friday = service_on - timedelta(days=service_on.weekday() - 4)
        probe_days.add(friday)
        probe_days.add(friday - timedelta(days=1))
    closed: set[date] = set()
    opened: set[date] = set()
    for day in probe_days:
        provision = schedule.provision_for(Meal.BREAKFAST, day)
        if provision.treatment is not ServiceTreatment.NORMAL:
            closed.add(day)
        elif provision.change_id is not None:
            opened.add(day)
    return PhysicalDeliveryCalendar(frozenset(closed), frozenset(opened))


def _candidate_delivery(
    meal: Meal,
    treatment: ServiceTreatment,
    delivery_on: date | None,
    service_on: date,
) -> date | None:
    if treatment is ServiceTreatment.NOT_PROVIDED:
        return None
    if treatment is ServiceTreatment.REPLACEMENT:
        return delivery_on
    if meal is Meal.BREAKFAST:
        return None
    return service_on


def _coalesce_shipments(
    shipments,
) -> tuple[PhysicalShipment, ...]:
    service_dates_by_key: defaultdict[
        tuple[UUID, Meal, date], set[date]
    ] = defaultdict(set)
    for item in shipments:
        service_dates_by_key[
            (item.person_id, item.meal, item.delivery_on)
        ].update(item.service_dates)
    return tuple(
        PhysicalShipment(person_id, meal, delivery_on, tuple(sorted(service_dates)))
        for (person_id, meal, delivery_on), service_dates in sorted(
            service_dates_by_key.items(),
            key=lambda item: (
                item[0][2],
                tuple(Meal).index(item[0][1]),
                str(item[0][0]),
            ),
        )
    )


def _previous_delivery_keys(
    shipments: tuple[PhysicalShipment, ...],
    target_keys: tuple[tuple[date, Meal], ...],
) -> dict[tuple[date, Meal], tuple[date, Meal]]:
    dates_by_meal: defaultdict[Meal, set[date]] = defaultdict(set)
    for item in shipments:
        dates_by_meal[item.meal].add(item.delivery_on)
    result: dict[tuple[date, Meal], tuple[date, Meal]] = {}
    for day, meal in target_keys:
        previous = max(
            (
                candidate
                for candidate in dates_by_meal[meal]
                if candidate.year == day.year and candidate < day
            ),
            default=None,
        )
        if previous is not None:
            result[(day, meal)] = (previous, meal)
    return result


def _shipment_presences(
    roster: RosterSnapshot,
    shipments: tuple[PhysicalShipment, ...],
    routing: RoutingState,
) -> Mapping[tuple[date, Meal], tuple[_ShipmentPresence, ...]]:
    by_key: defaultdict[tuple[date, Meal], list[_ShipmentPresence]] = defaultdict(list)
    for shipment in shipments:
        person = roster.person(shipment.person_id)
        household_ids: set[UUID] = set()
        for service_on in shipment.service_dates:
            household = roster.household_for(shipment.person_id, on=service_on)
            if household is None:
                raise ValueError(
                    f"{person.profile.name}님의 {service_on:%Y.%m.%d} 식사일에 "
                    "유효한 가구가 없습니다."
                )
            household_ids.add(household.household_id)
        if len(household_ids) != 1:
            shown = ", ".join(day.strftime("%Y.%m.%d") for day in shipment.service_dates)
            raise ValueError(
                f"{person.profile.name}님의 한 번 배송에 서로 다른 가구가 섞입니다({shown}). "
                "가구 변경 적용일을 확인해주세요."
            )
        assignment = roster.resolve_assignment(shipment.person_id, shipment.meal)
        delivery_view = routing.resolve(
            roster, shipment.person_id, shipment.delivery_on
        )
        driver = "" if assignment.driver is None else assignment.driver.strip()
        if not driver:
            raise ValueError(
                f"{shipment.delivery_on:%Y.%m.%d} {shipment.meal.value} 배송 대상 "
                f"{person.profile.name}님의 배송담당이 비어 있습니다."
            )
        by_key[(shipment.delivery_on, shipment.meal)].append(
            _ShipmentPresence(
                shipment.person_id,
                next(iter(household_ids)),
                driver,
                "" if assignment.sequence is None else assignment.sequence.strip(),
                delivery_view.jurisdiction or "",
                next(
                    (
                        setting.form
                        for setting in roster.delivery_settings_for(
                            shipment.person_id, on=shipment.service_dates[0]
                        )
                        if setting.meal is shipment.meal
                    ),
                    None,
                ),
                shipment.entitlement_count,
            )
        )
    return MappingProxyType(
        {
            key: tuple(
                sorted(
                    values,
                    key=lambda item: (
                        item.driver.casefold(),
                        _sequence_sort_key(item.sequence),
                        str(item.person_id),
                    ),
                )
            )
            for key, values in by_key.items()
        }
    )


def _aggregate_presences(
    presences: Sequence[_ShipmentPresence],
    roster: RosterSnapshot,
    *,
    context: str,
) -> _DeliveryAggregate:
    locations: defaultdict[tuple[str, UUID], set[UUID]] = defaultdict(set)
    areas: defaultdict[str, set[str]] = defaultdict(set)
    for item in presences:
        locations[(item.driver, item.household_id)].add(item.person_id)
        if item.jurisdiction.strip():
            areas[item.driver].add(item.jurisdiction.strip())
    buckets: defaultdict[str, list[int]] = defaultdict(lambda: [0] * 5)
    form_households: defaultdict[tuple[str, str | None], set[UUID]] = defaultdict(set)
    form_meals: Counter[tuple[str, str | None]] = Counter()
    for item in presences:
        form_households[(item.driver, item.form)].add(item.household_id)
        form_meals[(item.driver, item.form)] += item.entitlement_count
    for (driver, household_id), person_ids in locations.items():
        household_size = len(person_ids)
        if household_size > 5:
            names = ", ".join(
                roster.person(person_id).profile.name
                for person_id in sorted(person_ids, key=str)
            )
            raise ValueError(
                f"{context}: {driver} 담당 가구가 6명 이상입니다"
                f"({household_size}명: {names}; 가구 {household_id}). "
                "현재 일일식수 양식은 1~5명 가구만 지원합니다."
            )
        buckets[driver][household_size - 1] += 1
    return _DeliveryAggregate(
        MappingProxyType(
            {
                driver: tuple(values)
                for driver, values in buckets.items()
            }
        ),
        MappingProxyType(
            {driver: frozenset(values) for driver, values in areas.items()}
        ),
        MappingProxyType({
            driver: MappingProxyType({
                form: (len(households), form_meals[(driver, form)])
                for (staff, form), households in form_households.items()
                if staff == driver
            })
            for driver in {staff for staff, _form in form_households}
        }),
    )


def _build_report(
    day: date,
    meal: Meal,
    current: _DeliveryAggregate,
    previous: _DeliveryAggregate,
    staff_sort: Sequence[str],
) -> DeliveryReport:
    all_staff = set(current.buckets_by_staff) | set(previous.buckets_by_staff)
    configured_order = {
        name: index for index, name in enumerate(_normalized_names(staff_sort))
    }
    ordered_staff = sorted(
        all_staff,
        key=lambda name: (
            (0, configured_order[name], "", "")
            if name in configured_order
            else (1, 0, name.casefold(), name)
        ),
    )
    rows: list[DeliveryReportRow] = []
    totals = [0, 0, 0, 0, 0]
    person_total = 0
    for staff in ordered_staff:
        current_buckets = current.buckets_by_staff.get(staff, _EMPTY_BUCKETS)
        previous_buckets = previous.buckets_by_staff.get(staff, _EMPTY_BUCKETS)
        household_count = sum(current_buckets)
        people = sum(
            size * count for size, count in enumerate(current_buckets, start=1)
        )
        area_values = current.areas_by_staff.get(
            staff, frozenset()
        ) | previous.areas_by_staff.get(staff, frozenset())
        ordered_areas = sorted(area_values, key=lambda value: (value.casefold(), value))
        rows.append(
            DeliveryReportRow(
                f"{staff}\n({household_count})",
                tuple(
                    fmt_cell(current_buckets[index], previous_buckets[index])
                    for index in range(5)
                ),
                people,
                _form_memo(current.forms_by_staff.get(staff, {})),
                "\n".join(
                    ", ".join(ordered_areas[index : index + 2])
                    for index in range(0, len(ordered_areas), 2)
                ),
            )
        )
        for index, count in enumerate(current_buckets):
            totals[index] += count
        person_total += people
    return DeliveryReport(
        day,
        meal.value,
        tuple(rows),
        DeliveryReportTotals(tuple(totals), person_total),
    )


def _empty_aggregate() -> _DeliveryAggregate:
    return _DeliveryAggregate(MappingProxyType({}), MappingProxyType({}), MappingProxyType({}))


def _form_memo(forms: Mapping[str | None, tuple[int, int]]) -> str:
    if not any(form is not None for form in forms):
        return ""
    return "\n".join(
        f"{form or '기본'} {counts[0]}가구·{counts[1]}식"
        for form, counts in sorted(forms.items(), key=lambda item: (item[0] is not None, item[0] or ""))
    )


def _relevant_adjustment_ids(
    schedule: ServiceAdjustmentSchedule,
    meals: tuple[Meal, ...],
    evaluation_start: date,
    evaluation_end: date,
    report_start: date,
    report_end: date,
) -> tuple[str, ...]:
    result: list[str] = []
    for change in schedule.changes:
        if not any(meal in change.meals for meal in meals):
            continue
        assert change.period.starts_on is not None
        assert change.period.ends_on is not None
        overlaps_evaluation = (
            change.period.starts_on <= evaluation_end
            and evaluation_start <= change.period.ends_on
        )
        replacement_hits_report = False
        if change.treatment is ServiceTreatment.REPLACEMENT:
            replacement_dates = {change.default_delivery_on} | {
                item.delivery_on for item in change.delivery_overrides
            }
            replacement_hits_report = any(
                day is not None and report_start <= day <= report_end
                for day in replacement_dates
            )
        if overlaps_evaluation or replacement_hits_report:
            result.append(str(change.change_id))
    return tuple(result)


def _normalize_staff_sort(
    value: Mapping[Meal | str, Sequence[str]] | None,
) -> Mapping[Meal, tuple[str, ...]]:
    if value is None:
        return MappingProxyType({})
    if not isinstance(value, Mapping):
        raise TypeError("staff_sort는 mapping 또는 None이어야 합니다.")
    result: dict[Meal, tuple[str, ...]] = {}
    for raw_meal, names in value.items():
        meal = raw_meal if isinstance(raw_meal, Meal) else Meal(str(raw_meal))
        if isinstance(names, (str, bytes)) or not isinstance(names, Sequence):
            raise TypeError("배송담당 순서는 문자열 sequence여야 합니다.")
        result[meal] = _normalized_names(names)
    return MappingProxyType(result)


def _normalized_names(names: Sequence[str]) -> tuple[str, ...]:
    result: list[str] = []
    for value in names:
        if not isinstance(value, str):
            raise TypeError("배송담당 순서 이름은 문자열이어야 합니다.")
        name = value.strip()
        if name and name not in result:
            result.append(name)
    return tuple(result)


def _canonical_meals(meals: Sequence[Meal]) -> tuple[Meal, ...]:
    selected = tuple(meals)
    if not selected:
        raise ValueError("일일식수에 포함할 식종이 없습니다.")
    if any(not isinstance(meal, Meal) for meal in selected):
        raise TypeError("meals에는 Meal만 허용됩니다.")
    if len(set(selected)) != len(selected):
        raise ValueError("일일식수 식종은 중복될 수 없습니다.")
    return tuple(meal for meal in Meal if meal in selected)


def _state_sha256(state: Mapping[str, JsonValue]) -> str:
    encoded = json.dumps(
        plain_json(state),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _sequence_sort_key(value: str) -> tuple[int, float, str]:
    text = value.strip()
    if not text:
        return 2, 0, ""
    try:
        return 0, float(text), text
    except ValueError:
        return 1, 0, text.casefold()


def _days(starts_on: date, ends_on: date):
    current = starts_on
    while current <= ends_on:
        yield current
        current += timedelta(days=1)


def _contains(starts_on: date, ends_on: date, day: date) -> bool:
    return starts_on <= day <= ends_on


def _validate_date(value: object, label: str) -> None:
    if isinstance(value, datetime) or not isinstance(value, date):
        raise TypeError(f"{label}은 date여야 합니다.")
