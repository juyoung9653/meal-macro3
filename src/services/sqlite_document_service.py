"""Calculate monthly SQLite-v2 invoice/confirmation facts from one revision."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from calendar import monthrange
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from uuid import UUID

from src.core.date_utils import get_week_ranges
from src.core.delivery_matrix import RosterEntry
from src.core.invoice_data import (
    MealPrices,
    MonthlyDelivery,
    MonthlyDeliveryBatch,
    WeeklyDelivery,
)
from src.core.models import FULL_ADDRESS_FIELD, combine_address
from src.domain.billing import EntitlementBilling, price_entitlements
from src.domain.delivery_routing import (
    PersonDeliveryView,
    RoutingState,
    routing_state_from_snapshot,
)
from src.domain.entitlements import (
    MealEntitlement,
)
from src.domain.delivery_defaults import delivery_defaults_state
from src.domain.invoice_settings import invoice_settings_from_snapshot
from src.services.invoice_remarks import invoice_history_notes
from src.domain.projections import (
    roster_from_historical_snapshot,
    status_events_from_historical_snapshot,
)
from src.domain.provider_settings import provider_from_snapshot
from src.domain.revisions import (
    HistoricalSnapshot,
    JsonValue,
    freeze_json_object,
    plain_json,
)
from src.domain.roster import Meal, Person, RosterSnapshot
from src.domain.school_calendar import school_lunch_calendar_from_snapshot
from src.domain.service_adjustments import (
    SERVICE_ADJUSTMENT_POLICY_VERSION,
    ServiceAdjustmentSchedule,
    service_adjustment_schedule_from_snapshot,
    service_adjustment_schedule_state,
)
from src.domain.unit_pricing import (
    UnitPriceSchedule,
    global_unit_price_note,
    unit_price_schedule_from_snapshot,
)
from src.services.meal_service_availability import (
    RECURRING_MEAL_SERVICE_POLICY_VERSION,
)

MONTHLY_DOCUMENT_CALCULATION_CONTRACT_VERSION = 4


class HouseholdServiceDatesError(ValueError):
    """A household diagnostic with a private lookup key for the current UI row."""

    def __init__(self, message: str, person_id: UUID) -> None:
        super().__init__(message)
        self.person_id = person_id


@dataclass(frozen=True, slots=True)
class PreparedMonthlyDocumentData:
    """One immutable calculation shared by invoice and confirmation writers."""

    batch: MonthlyDeliveryBatch
    billing: EntitlementBilling
    entitlements: tuple[MealEntitlement, ...]
    calculation_manifest: Mapping[str, JsonValue]
    global_note: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.batch, MonthlyDeliveryBatch):
            raise TypeError("batch는 MonthlyDeliveryBatch여야 합니다.")
        if not isinstance(self.billing, EntitlementBilling):
            raise TypeError("billing은 EntitlementBilling이어야 합니다.")
        entitlements = tuple(self.entitlements)
        if any(not isinstance(item, MealEntitlement) for item in entitlements):
            raise TypeError("entitlements에는 MealEntitlement만 허용됩니다.")
        object.__setattr__(self, "entitlements", entitlements)
        object.__setattr__(
            self,
            "calculation_manifest",
            freeze_json_object(self.calculation_manifest),
        )


def prepare_monthly_document_data(
    snapshot: HistoricalSnapshot,
    period_start: date,
    period_end: date,
    meals: Sequence[Meal],
    *,
    revisions=(),
) -> PreparedMonthlyDocumentData:
    """Resolve enrollment, school calendar, closures, status, and pricing once."""
    from src.services.sqlite_delivery_report_service import prepare_physical_delivery_data

    if not isinstance(snapshot, HistoricalSnapshot):
        raise TypeError("snapshot은 HistoricalSnapshot이어야 합니다.")
    _validate_date(period_start, "문서 시작일")
    _validate_date(period_end, "문서 종료일")
    if period_start > period_end:
        raise ValueError("문서 시작일은 종료일보다 늦을 수 없습니다.")
    if (period_start.year, period_start.month) != (
        period_end.year,
        period_end.month,
    ):
        raise ValueError("청구서·확인서 계산 기간은 같은 달이어야 합니다.")
    selected_meals = _canonical_meals(meals)

    roster = roster_from_historical_snapshot(snapshot)
    provider = provider_from_snapshot(snapshot)
    school_calendar = school_lunch_calendar_from_snapshot(snapshot)
    adjustments = service_adjustment_schedule_from_snapshot(snapshot)
    price_schedule = unit_price_schedule_from_snapshot(snapshot)
    status_events = status_events_from_historical_snapshot(snapshot)
    physical = prepare_physical_delivery_data(snapshot, period_start, period_end, selected_meals)
    policy = invoice_settings_from_snapshot(snapshot)
    month_end = date(period_end.year, period_end.month, monthrange(period_end.year, period_end.month)[1])
    delivery_dates = {
        (shipment.person_id, shipment.meal, service_on): shipment.delivery_on
        for shipment in physical.shipments
        if period_start <= shipment.delivery_on <= period_end
        for service_on in shipment.service_dates
        if policy.include_next_month or service_on <= month_end
    }
    entitlements = [MealEntitlement(*key) for key in delivery_dates]
    exclusions = physical.calculation_manifest["exclusions"]

    ordered_entitlements = tuple(
        sorted(
            entitlements,
            key=lambda item: (str(item.person_id), tuple(Meal).index(item.meal), item.service_on),
        )
    )
    billing = price_entitlements(ordered_entitlements, price_schedule)
    batch = _monthly_batch(
        roster,
        billing,
        ordered_entitlements,
        price_schedule,
        period_start,
        period_end,
        provider,
        routing=routing_state_from_snapshot(snapshot),
        delivery_dates=delivery_dates,
        history_notes=invoice_history_notes(snapshot, revisions, period_start, period_end, selected_meals),
    )
    global_note = global_unit_price_note(
        price_schedule,
        min((period_start, *(item.service_on for item in ordered_entitlements))),
        max((period_end, *(item.service_on for item in ordered_entitlements))),
        selected_meals,
    )
    billing_manifest = billing.calculation_manifest(
        price_schedule,
        period_start=period_start,
        period_end=period_end,
        meals=selected_meals,
        delivery_dates=delivery_dates,
    )
    calculation_manifest = freeze_json_object(
        {
            "calculation_contract_version": MONTHLY_DOCUMENT_CALCULATION_CONTRACT_VERSION,
            "document_presentation": {
                "policy_version": 1,
                "effective_on": period_end.isoformat(),
                "routing_basis": "document_period_end",
            },
            "source_revision_id": str(snapshot.revision_id),
            "period_basis": "physical_delivery_day",
            "include_next_month": policy.include_next_month,
            "physical_delivery": physical.calculation_manifest,
            "selected_meals": tuple(meal.value for meal in selected_meals),
            "recurring_service_policy": {
                "version": RECURRING_MEAL_SERVICE_POLICY_VERSION,
                "defaults": delivery_defaults_state(roster.delivery_defaults),
                "lunch": "configured_days_and_reviewed_school_weekdays",
                "household_weekday_override": "replaces_regular_days; lunch_requires_school_service",
                "household_delivery_weekday": "latest_configured_weekday_on_or_before_service_day",
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
                    period_start,
                    period_end,
                ),
            },
            "status_event_count": len(status_events),
            "exclusions": dict(sorted(exclusions.items())),
            "billing": billing_manifest,
        }
    )
    return PreparedMonthlyDocumentData(
        batch,
        billing,
        ordered_entitlements,
        calculation_manifest,
        global_note,
    )


def _monthly_batch(
    roster: RosterSnapshot,
    billing: EntitlementBilling,
    entitlements: tuple[MealEntitlement, ...],
    price_schedule: UnitPriceSchedule,
    period_start: date,
    period_end: date,
    provider: str = "",
    *,
    routing: RoutingState,
    delivery_dates: Mapping[tuple[UUID, Meal, date], date] | None = None,
    history_notes: Mapping[UUID, str] | None = None,
) -> MonthlyDeliveryBatch:
    by_person: defaultdict[UUID, list[MealEntitlement]] = defaultdict(list)
    for entitlement in entitlements:
        by_person[entitlement.person_id].append(entitlement)
    summaries = {summary.person_id: summary for summary in billing.summaries}
    deliveries: list[MonthlyDelivery] = []
    for position, person in enumerate(roster.persons, start=2):
        person_entitlements = tuple(by_person[person.person_id])
        if not person_entitlements:
            continue
        summary = summaries[person.person_id]
        active_meals = tuple(
            meal
            for meal in Meal
            if any(item.meal is meal for item in person_entitlements)
        )
        assignments = tuple(
            roster.resolve_assignment(person.person_id, meal)
            for meal in active_meals
        )
        delivery_view = routing.resolve(roster, person.person_id, period_end)
        service_dates = tuple(item.service_on for item in person_entitlements)
        deliveries.append(
            MonthlyDelivery(
                roster=_roster_entry(person, roster, position, provider, delivery_view=delivery_view),
                note=(history_notes or {}).get(person.person_id, ""),
                delivery_staff=_join_unique(delivery_view.drivers[meal] for meal in active_meals),
                delivery_sequence=_join_unique(item.sequence for item in assignments),
                breakfast_count=summary.count_for(Meal.BREAKFAST),
                lunch_count=summary.count_for(Meal.LUNCH),
                dinner_count=summary.count_for(Meal.DINNER),
                weekly=_weekly_deliveries(
                    period_start.year,
                    period_start.month,
                    person_entitlements,
                    delivery_dates=delivery_dates,
                ),
                billing_amount=summary.total_amount,
                household_key=str(
                    _household_for_service_dates(roster, person.person_id, service_dates)
                ),
            )
        )
    prices = price_schedule.prices_on(period_start)
    return MonthlyDeliveryBatch(
        period_start.year,
        period_start.month,
        MealPrices(
            breakfast=prices[Meal.BREAKFAST],
            lunch=prices[Meal.LUNCH],
            dinner=prices[Meal.DINNER],
        ),
        any(item.lunch_count > 0 for item in deliveries),
        tuple(deliveries),
    )


def _roster_entry(
    person: Person,
    roster: RosterSnapshot,
    position: int,
    provider: str = "",
    *,
    delivery_view: PersonDeliveryView | None = None,
) -> RosterEntry:
    return RosterEntry(position, roster_mapping_values(person, roster, provider, delivery_view=delivery_view))


def roster_mapping_values(
    person: Person, roster: RosterSnapshot, provider: str = "", *,
    delivery_view: PersonDeliveryView | None = None,
) -> dict[str, object]:
    """Raw mapping sources shared by invoices, confirmations and substitute sheets."""
    profile = person.profile
    birth_date = profile.birth_date
    extra: dict[str, object] = {
        "취학유형": profile.school_type or "",
        "구분": profile.school_type or "",
        "학교명": profile.school_name or "",
        "학년": profile.grade or "",
        "반": profile.class_name or "",
        "성명": profile.name,
        "이름": profile.name,
        "생년월일": "" if birth_date is None else birth_date,
        "세대주 성명": profile.household_head_name or "",
        "세대주성명": profile.household_head_name or "",
        "아동과의 관계": profile.relationship_to_child or "",
        "아동과의관계": profile.relationship_to_child or "",
        "관할동": profile.jurisdiction or "",
        "동": profile.jurisdiction or "",
        "도로명주소": profile.road_address or "",
        "상세주소": profile.detail_address or "",
        "주소": combine_address(profile.road_address, profile.detail_address),
        FULL_ADDRESS_FIELD: combine_address(
            profile.road_address,
            profile.detail_address,
        ),
        "전화번호": profile.telephone or "",
        "핸드폰": profile.mobile_phone or "",
        "제공기관": provider,
        "비고": profile.note or "",
    }
    definitions = {item.definition_id: item for item in roster.custom_fields}
    for definition_id, value in profile.custom_values.items():
        definition = definitions.get(definition_id)
        if definition is not None:
            # A same-named custom field is selected by ID, never by replacing
            # the standard field the user selected in output column mappings.
            extra.setdefault(definition.name, value)
            extra[f"custom:{definition_id}"] = value
    # Dated presentation fields take precedence over legacy custom columns.
    if delivery_view is not None:
        road, detail = delivery_view.road_address, delivery_view.detail_address
        extra.update({
            "관할동": delivery_view.jurisdiction or "",
            "동": delivery_view.jurisdiction or "",
            "도로명주소": road or "",
            "상세주소": detail or "",
            "주소": combine_address(road, detail),
            FULL_ADDRESS_FIELD: combine_address(road, detail),
        })
    # A legacy custom field must not override the database-wide provider.
    extra["제공기관"] = provider
    return extra


def _weekly_deliveries(
    year: int,
    month: int,
    entitlements: tuple[MealEntitlement, ...],
    *,
    delivery_dates: Mapping[tuple[UUID, Meal, date], date] | None = None,
) -> tuple[WeeklyDelivery, ...]:
    result = []
    for week, starts_on, ends_on in get_week_ranges(year, month):
        counts = Counter(
            item.meal
            for item in entitlements
            if starts_on <= (
                item.service_on if delivery_dates is None else
                delivery_dates[(item.person_id, item.meal, item.service_on)]
            ) <= ends_on
        )
        result.append(
            WeeklyDelivery(
                week,
                starts_on,
                ends_on,
                counts[Meal.BREAKFAST],
                counts[Meal.LUNCH],
                counts[Meal.DINNER],
            )
        )
    return tuple(result)


def _household_for_service_dates(
    roster: RosterSnapshot,
    person_id: UUID,
    service_dates: tuple[date, ...],
) -> UUID:
    memberships = tuple(
        item for item in roster.memberships if item.person_id == person_id
    )
    household_ids = {
        membership.household_id
        for membership in memberships
        if any(membership.period.contains(day) for day in service_dates)
    }
    if len(household_ids) != 1:
        person = roster.person(person_id)
        profile = person.profile
        identity = [f"성명: {profile.name}"]
        if profile.birth_date is not None:
            identity.append(f"생년월일: {profile.birth_date.isoformat()}")
        if profile.school_name:
            identity.append(f"학교명: {profile.school_name}")
        if not memberships:
            reason = "청구 대상자의 가구 소속 기록이 없습니다."
        elif not household_ids:
            reason = "청구 대상자의 가구 소속 기간이 실제 서비스일과 겹치지 않습니다."
        else:
            reason = (
                f"실제 서비스일에 해당하는 가구 소속이 {len(household_ids)}개입니다. "
                "한 문서 기간의 가구를 하나로 결정할 수 없어 문서를 만들 수 없습니다."
            )
        periods = ["저장된 가구 소속 기간:"]
        household_numbers: dict[UUID, int] = {}
        for membership in memberships:
            number = household_numbers.setdefault(
                membership.household_id, len(household_numbers) + 1
            )
            starts_on = membership.period.starts_on
            ends_on = membership.period.ends_on
            periods.append(
                f"- 가구 {number}: 적용 시작일 {starts_on.isoformat() if starts_on else '제한 없음'}, "
                f"종료일 {ends_on.isoformat() if ends_on else '없음'}"
            )
        if not memberships:
            periods.append("- 기록 없음")
            periods.append("과거에 추가된 대상자라면 설정 → 가구 소속 기록 복구에서 복구 가능 여부를 확인해주세요.")
        raise HouseholdServiceDatesError(
            "\n".join((
                reason,
                " / ".join(identity),
                f"실제 서비스일: {_service_date_ranges(service_dates)}",
                "확인할 저장 항목: 가구 소속 / 적용 시작일 / 종료일",
                *periods,
                "위 날짜와 가구 소속 기록을 확인해주세요. 이 항목은 명단의 표시 칼럼이 아닙니다.",
            )),
            person_id,
        )
    return next(iter(household_ids))


def _service_date_ranges(service_dates: tuple[date, ...]) -> str:
    """List exact service dates, compacting only consecutive calendar days."""
    days = sorted(set(service_dates))
    ranges: list[str] = []
    for day in days:
        if ranges and day == ends_on + timedelta(days=1):
            ends_on = day
            ranges[-1] = f"{starts_on.isoformat()} ~ {ends_on.isoformat()}"
        else:
            starts_on = ends_on = day
            ranges.append(day.isoformat())
    return ", ".join(ranges) or "없음"


def _relevant_adjustment_ids(
    schedule: ServiceAdjustmentSchedule,
    meals: tuple[Meal, ...],
    starts_on: date,
    ends_on: date,
) -> tuple[str, ...]:
    return tuple(
        str(change.change_id)
        for change in schedule.changes
        if any(meal in change.meals for meal in meals)
        and change.period.starts_on is not None
        and change.period.ends_on is not None
        and change.period.starts_on <= ends_on
        and starts_on <= change.period.ends_on
    )


def _state_sha256(state: Mapping[str, JsonValue]) -> str:
    encoded = json.dumps(
        plain_json(state),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _canonical_meals(meals: Sequence[Meal]) -> tuple[Meal, ...]:
    selected = tuple(meals)
    if not selected:
        raise ValueError("문서에 포함할 식종이 없습니다.")
    if any(not isinstance(meal, Meal) for meal in selected):
        raise TypeError("meals에는 Meal만 허용됩니다.")
    if len(set(selected)) != len(selected):
        raise ValueError("문서 식종은 중복될 수 없습니다.")
    return tuple(meal for meal in Meal if meal in selected)


def _join_unique(values) -> str:
    unique: list[str] = []
    for value in values:
        text = "" if value is None else str(value).strip()
        if text and text not in unique:
            unique.append(text)
    return " / ".join(unique)


def _days(starts_on: date, ends_on: date):
    current = starts_on
    while current <= ends_on:
        yield current
        current += timedelta(days=1)


def _validate_date(value: object, label: str) -> None:
    if isinstance(value, datetime) or not isinstance(value, date):
        raise TypeError(f"{label}은 date여야 합니다.")
