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
from src.domain.pause_conversion import pause_conversion_from_snapshot
from src.domain.entitlements import (
    PhysicalShipment,
    decide_entitlement,
)
from src.domain.delivery_defaults import delivery_defaults_state
from src.domain.delivery_routing import RoutingState, routing_state_from_snapshot
from src.domain.daily_driver_order import daily_driver_orders
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
from src.domain.status import StatusEvent, StatusHistory, resolve_status_history
from src.services.delivery_report_remarks import build_additional_remarks, report_remark_columns
from src.domain.daily_report_choices import ReportRemarkOptions
from src.services.delivery_service import (
    DeliveryReport,
    DeliveryReportRow,
    DeliveryReportTotals,
)
from src.services.delivery_households import require_delivery_household
from src.services.daily_report_mapping import DailyReportMapping
from src.services.meal_service_availability import (
    RECURRING_MEAL_SERVICE_POLICY_VERSION,
    physical_delivery_on,
    regular_service_available,
)


DELIVERY_REPORT_CALCULATION_CONTRACT_VERSION = 7
DELIVERY_REPORT_GROUPING_POLICY_VERSION = 3


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


class _MissingDeliveryDriverError(ValueError):
    """A shipment cannot be grouped because its dated driver is missing."""


_EMPTY_BUCKETS = (0, 0, 0, 0, 0)


def prepare_delivery_report_data(
    snapshot: HistoricalSnapshot,
    period_start: date,
    period_end: date,
    meals: Sequence[Meal],
    *,
    staff_sort: Mapping[Meal | str, Sequence[str]] | None = None,
    delivery_forms: Sequence[str | None] | None = None,
    remark_options: ReportRemarkOptions = ReportRemarkOptions(),
    mapping_snapshot: HistoricalSnapshot | None = None,
) -> PreparedDeliveryReportData:
    """Build report rows from entitlement service dates and physical shipments.

    The report range is a physical-delivery range.  Breakfast weekend meals and
    replacement meals can therefore be represented on a different day from the
    entitlement's original service date.
    """

    return _prepare_delivery_data(
        snapshot, period_start, period_end, meals, staff_sort=staff_sort,
        build_reports=True, delivery_forms=delivery_forms, remark_options=remark_options,
        mapping_snapshot=mapping_snapshot,
    )


def delivery_forms_from_snapshot(snapshot: HistoricalSnapshot) -> tuple[str | None, ...]:
    """List the default and named forms saved in the selected source revision."""
    if not isinstance(snapshot, HistoricalSnapshot):
        raise TypeError("snapshot은 HistoricalSnapshot이어야 합니다.")
    roster = roster_from_historical_snapshot(snapshot)
    names = {
        setting.form
        for owner in (*roster.persons, *roster.households)
        for setting in (owner.delivery_settings or ())
        if setting.form is not None
    }
    return (None, *sorted(names, key=lambda value: (value.casefold(), value)))


def prepare_physical_delivery_data(
    snapshot: HistoricalSnapshot,
    period_start: date,
    period_end: date,
    meals: Sequence[Meal],
    *,
    missing_lunch_schedule_as_zero: bool = True,
    include_service_period: bool = False,
) -> PreparedDeliveryReportData:
    """Prepare physical shipments without report grouping or driver requirements.

    ``reports`` is empty; shipments and calculation provenance use the same
    entitlement rules and evaluation window as delivery reports.
    Missing school schedules exclude only the affected lunch dates.
    include_service_period also retains meals eaten in the requested period,
    including bundles physically delivered in a different month or year.
    """
    return _prepare_delivery_data(
        snapshot, period_start, period_end, meals, staff_sort=None, build_reports=False,
        missing_lunch_schedule_as_zero=missing_lunch_schedule_as_zero,
        include_service_period=include_service_period,
    )


def _prepare_delivery_data(
    snapshot: HistoricalSnapshot,
    period_start: date,
    period_end: date,
    meals: Sequence[Meal],
    *,
    staff_sort: Mapping[Meal | str, Sequence[str]] | None,
    build_reports: bool,
    delivery_forms: Sequence[str | None] | None = None,
    remark_options: ReportRemarkOptions = ReportRemarkOptions(),
    missing_lunch_schedule_as_zero: bool = True,
    include_service_period: bool = False,
    mapping_snapshot: HistoricalSnapshot | None = None,
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
    if not isinstance(remark_options, ReportRemarkOptions):
        raise TypeError("일일식수 비고 설정이 올바르지 않습니다.")
    if build_reports and set(remark_options.columns) - {key for key, _ in report_remark_columns(roster)}:
        raise ValueError("비고에 선택한 칼럼이 원본에 없습니다. 비고 설정이나 원본 변경번호를 확인해주세요.")
    selected_forms: tuple[str | None, ...] | None = None
    if delivery_forms is not None:
        if not build_reports:
            raise ValueError("일일식수 문서에서만 배송 형태를 선택할 수 있습니다.")
        selected_forms = _canonical_delivery_forms(delivery_forms)
        available_forms = set(delivery_forms_from_snapshot(snapshot))
        unknown = set(selected_forms) - available_forms
        if unknown:
            raise ValueError("선택한 원본의 배송 설정에 없는 배송 형태가 있습니다.")
    routing = routing_state_from_snapshot(snapshot)
    report_mapping = DailyReportMapping(
        snapshot, snapshot if mapping_snapshot is None else mapping_snapshot, "급식",
    ) if build_reports else None
    if staff_sort is None:
        orders = daily_driver_orders(snapshot, routing)
        normalized_staff_sort = _normalize_staff_sort({
            meal: tuple(routing._drivers_by_id[driver_id].name for driver_id in orders[meal])
            for meal in selected_meals
        })
    school_calendar = school_lunch_calendar_from_snapshot(snapshot)
    adjustments = service_adjustment_schedule_from_snapshot(snapshot)
    status_events = status_events_from_historical_snapshot(snapshot)
    conversion = pause_conversion_from_snapshot(snapshot)
    events_by_key: defaultdict[tuple[UUID, Meal], list[StatusEvent]] = defaultdict(list)
    for event in status_events:
        events_by_key[(event.person_id, event.meal)].append(event)

    comparison_start = date(period_start.year, 1, 1)
    # Remarks sample one active weekday before the target week (up to 22 days),
    # including December for January reports. Count comparisons remain same-year.
    evaluation_start = min(
        comparison_start,
        period_start - timedelta(days=period_start.weekday() + 22),
    ) if build_reports else comparison_start
    normal_evaluation_end = period_end + timedelta(days=6)
    service_days = set(_days(evaluation_start, normal_evaluation_end))
    service_days.update(
        _replacement_service_days(
            adjustments,
            selected_meals,
            evaluation_start,
            period_end,
        )
    )
    ordered_service_days = tuple(sorted(service_days))
    shipments: list[PhysicalShipment] = []
    exclusions: Counter[str] = Counter()
    missing_lunch_schools = defaultdict(lambda: {"dates": set(), "people": set(), "count": 0})
    allowed_forms = set(selected_forms) if selected_forms is not None else None
    remark_forms = (set(remark_options.separate_forms)
                    if build_reports and remark_options.include_excluded_deliveries else set())
    evaluated_forms = allowed_forms | remark_forms if allowed_forms is not None else None
    histories: dict[tuple[UUID, Meal], StatusHistory] = {}
    for person in roster.persons:
        for meal in selected_meals:
            eligible_service_days = (
                ordered_service_days if evaluated_forms is None else tuple(
                    day for day in ordered_service_days
                    if _delivery_form_on(roster, person.person_id, meal, day) in evaluated_forms
                )
            )
            if not eligible_service_days:
                continue
            history = resolve_status_history(
                person.person_id,
                meal,
                tuple(events_by_key[(person.person_id, meal)]),
                same_month_pause=conversion.same_month,
            )
            histories[(person.person_id, meal)] = history
            for service_on in eligible_service_days:
                counted_form = allowed_forms is None or _delivery_form_on(roster, person.person_id, meal, service_on) in allowed_forms
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
                    delivery_provision = adjustments.provision_for(meal, candidate_delivery)
                    if delivery_provision.treatment is not ServiceTreatment.NORMAL:
                        candidate_delivery = delivery_provision.delivery_on
                if candidate_delivery is None:
                    if counted_form:
                        exclusions["delivery_day_closed"] += 1
                    continue
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
                        in_period = (period_start <= candidate_delivery <= period_end or
                                     (include_service_period and period_start <= service_on <= period_end))
                        if counted_form and in_period and decide_entitlement(
                            roster, person.person_id, meal, service_on,
                            status_history=history, service_available=True,
                            service_provision=provision,
                        ).entitlement is not None:
                            school = person.profile.school_name or "학교명 없음"
                            missing_lunch_schools[school]["dates"].add(service_on)
                            missing_lunch_schools[school]["people"].add(person.person_id)
                            missing_lunch_schools[school]["count"] += 1
                            exclusions["school_lunch_schedule_missing"] += 1
                        continue
                    if (period_start <= candidate_delivery <= period_end
                            or (include_service_period and period_start <= service_on <= period_end)):
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
                    if counted_form and period_start <= service_on <= normal_evaluation_end:
                        exclusions.update(item.value for item in decision.exclusions)
                    continue
                shipments.append(PhysicalShipment(person.person_id, meal, candidate_delivery, (service_on,)))

    coalesced_shipments = _coalesce_shipments(
        item
        for item in shipments
        if (evaluation_start <= item.delivery_on <= period_end
            or (include_service_period and any(period_start <= day <= period_end for day in item.service_dates)))
    )
    remark_shipments = tuple(item for item in coalesced_shipments
                             if _shipment_form(roster, item) in remark_forms
                             and period_start <= item.delivery_on <= period_end)
    if selected_forms is not None:
        coalesced_shipments = tuple(
            item for item in coalesced_shipments
            if _shipment_form(roster, item) in allowed_forms
        )

    reports: tuple[DeliveryReport, ...] = ()
    if build_reports:
        for key in remark_options.columns:
            if key.startswith("status:"):
                status_meal = Meal[key.split(":", 1)[1].upper()]
                for person in roster.persons:
                    histories.setdefault((person.person_id, status_meal), resolve_status_history(
                        person.person_id, status_meal, tuple(events_by_key[(person.person_id, status_meal)]),
                        same_month_pause=conversion.same_month))
        target_keys = tuple(
            sorted(
                {
                    (item.delivery_on, item.meal)
                    for item in (*coalesced_shipments, *remark_shipments)
                    if period_start <= item.delivery_on <= period_end
                },
                key=lambda item: (item[0], tuple(Meal).index(item[1])),
            )
        )
        previous_by_key = _previous_delivery_keys(coalesced_shipments, target_keys)
        target_key_set = set(target_keys)
        shipments_by_key: defaultdict[tuple[date, Meal], list[PhysicalShipment]] = defaultdict(list)
        for item in coalesced_shipments:
            shipments_by_key[(item.delivery_on, item.meal)].append(item)
        # Validate every selected date before consulting earlier comparison data.
        presences = _shipment_presences(
            roster,
            tuple(item for item in coalesced_shipments
                  if (item.delivery_on, item.meal) in target_key_set),
            routing,
            report_mapping=report_mapping,
        )
        aggregates = {
            key: _aggregate_presences(
                presences.get(key, ()),
                roster,
                context=f"{key[0]:%Y%m%d} {key[1].value}",
            )
            for key in target_keys
        }
        previous_aggregates: dict[tuple[date, Meal], _DeliveryAggregate | None] = dict(aggregates)
        for key in dict.fromkeys(previous_by_key.values()):
            if key in target_key_set:
                continue
            try:
                previous_presences = _shipment_presences(roster, tuple(shipments_by_key[key]), routing, report_mapping=report_mapping)
            except _MissingDeliveryDriverError:
                # An incomplete earlier day is unknown, never an empty or partial
                # delivery count. Suppress its deltas without blocking this period.
                previous_aggregates[key] = None
            else:
                previous_aggregates[key] = _aggregate_presences(
                    previous_presences.get(key, ()), roster,
                    context=f"{key[0]:%Y%m%d} {key[1].value}",
                )
        def build_report_for(day, meal):
            additional, headings = build_additional_remarks(
                roster, report_mapping.for_meal(meal), histories, day, meal,
                presences.get((day, meal), ()), remark_options,
                form_shipments=remark_shipments if remark_forms else None, include_headings=True)
            return _build_report(
                day,
                meal,
                aggregates[(day, meal)],
                previous_aggregates.get(previous_by_key.get((day, meal)), _empty_aggregate()),
                normalized_staff_sort.get(meal, ()),
                report_mapping.remarks_for(histories, day, meal, coalesced_shipments, allowed_forms,
                                           presences.get((day, meal), ())),
                additional,
                remark_options.position,
                headings,
            )

        reports = tuple(build_report_for(day, meal) for day, meal in target_keys)

    target_shipments = tuple(
        item
        for item in coalesced_shipments
        if (period_start <= item.delivery_on <= period_end
            or (include_service_period and any(period_start <= day <= period_end for day in item.service_dates)))
    )
    manifest = freeze_json_object(
        {
            "calculation_contract_version": (
                DELIVERY_REPORT_CALCULATION_CONTRACT_VERSION
            ),
            "source_revision_id": str(snapshot.revision_id),
            "selected_meals": tuple(meal.value for meal in selected_meals),
            **({"selected_delivery_forms": selected_forms} if build_reports else {}),
            **({"column_mapping": report_mapping.manifest()} if build_reports else {}),
            **({"remark_options": remark_options.manifest()}
               if build_reports and remark_options != ReportRemarkOptions() else {}),
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
                "policy_version": 2,
                "basis": "configured_delivery_weekdays_and_explicit_adjustments",
                "unreviewed_holidays": "normal",
                "closed_delivery_day": "exclude_entire_bundle",
            },
            "school_lunch_calendar": school_calendar.manifest(),
            "missing_lunch_schedule_policy": "exclude_affected_lunch_dates" if missing_lunch_schedule_as_zero else "require_schedule",
            "missing_lunch_schedules": [
                {"school_name": school, "service_dates": [day.isoformat() for day in sorted(values["dates"])],
                 "person_count": len(values["people"]), "meal_count": values["count"]}
                for school, values in sorted(missing_lunch_schools.items())
            ],
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
                "basis": "source_revision_current_household",
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
            "includes_service_period": include_service_period,
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
                    result.update(service_on + timedelta(days=offset) for offset in range(7))
    return result


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
    *, report_mapping: DailyReportMapping | None = None,
) -> Mapping[tuple[date, Meal], tuple[_ShipmentPresence, ...]]:
    by_key: defaultdict[tuple[date, Meal], list[_ShipmentPresence]] = defaultdict(list)
    for shipment in shipments:
        person = roster.person(shipment.person_id)
        household_id = require_delivery_household(
            roster, shipment.person_id, shipment.service_dates,
        )
        assignment = roster.resolve_assignment(shipment.person_id, shipment.meal)
        delivery_view = (report_mapping.for_meal(shipment.meal) if report_mapping else routing).resolve(
            roster, shipment.person_id, shipment.delivery_on
        )
        driver = delivery_view.drivers[shipment.meal].strip()
        if not driver and (report_mapping is None or (
            report_mapping.mapping["driver"] == "배송담당" and not report_mapping.fallbacks.get("driver")
        )):
            raise _MissingDeliveryDriverError(
                f"{shipment.delivery_on:%Y.%m.%d} {shipment.meal.value} 배송 대상 "
                f"{person.profile.name}님의 배송담당이 비어 있습니다."
            )
        by_key[(shipment.delivery_on, shipment.meal)].append(
            _ShipmentPresence(
                shipment.person_id,
                household_id,
                driver,
                "" if assignment.sequence is None else assignment.sequence.strip(),
                delivery_view.jurisdiction or "",
                _shipment_form(roster, shipment),
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


def _shipment_form(roster: RosterSnapshot, shipment: PhysicalShipment) -> str | None:
    return _delivery_form_on(
        roster, shipment.person_id, shipment.meal, shipment.service_dates[0],
    )


def _delivery_form_on(
    roster: RosterSnapshot, person_id: UUID, meal: Meal, on: date,
) -> str | None:
    return next(
        (
            setting.form
            for setting in roster.delivery_settings_for(
                person_id, on=on
            )
            if setting.meal is meal
        ),
        None,
    )


def _canonical_delivery_forms(forms: Sequence[str | None]) -> tuple[str | None, ...]:
    if isinstance(forms, (str, bytes)):
        raise TypeError("배송 형태는 목록으로 선택해야 합니다.")
    selected = tuple(forms)
    if not selected:
        raise ValueError("문서에 포함할 배송 형태를 하나 이상 선택해야 합니다.")
    for form in selected:
        if form is not None and (
            not isinstance(form, str) or not form or form != " ".join(form.split())
        ):
            raise ValueError("배송 형태 선택 값이 올바르지 않습니다.")
    if len(set(selected)) != len(selected):
        raise ValueError("같은 배송 형태를 중복 선택할 수 없습니다.")
    return tuple(sorted(selected, key=lambda value: (value is not None, value.casefold() if value else "", value or "")))


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
    previous: _DeliveryAggregate | None,
    staff_sort: Sequence[str],
    status_remarks: Mapping[str, str],
    additional_remarks: Mapping[str, str] | None = None,
    remark_position: str = "above",
    additional_headings: Mapping[str, tuple[str, ...]] | None = None,
) -> DeliveryReport:
    # None denotes unavailable comparison data; equal buckets suppress deltas.
    if previous is None:
        previous = current
    all_staff = set(current.buckets_by_staff) | set(previous.buckets_by_staff) | set(status_remarks) | set(additional_remarks or {})
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
                _combine_remarks(
                    "\n".join(filter(None, (status_remarks.get(staff, ""),
                                           _form_memo(current.forms_by_staff.get(staff, {}))))),
                    (additional_remarks or {}).get(staff, ""), remark_position,
                ),
                "\n".join(
                    ", ".join(ordered_areas[index : index + 2])
                    for index in range(0, len(ordered_areas), 2)
                ),
                memo_headings=(additional_headings or {}).get(staff, ()),
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


def _validate_date(value: object, label: str) -> None:
    if isinstance(value, datetime) or not isinstance(value, date):
        raise TypeError(f"{label}은 date여야 합니다.")


def _combine_remarks(existing, additional, position):
    parts = (additional, existing) if position == "above" else (existing, additional)
    return "\n".join(part for part in parts if part)
