"""Service-date pricing of immutable meal entitlements."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID

from .entitlements import MealEntitlement
from .revisions import JsonValue, freeze_json_object
from .roster import Meal
from .unit_pricing import (
    UNIT_PRICE_POLICY_VERSION,
    UnitPriceSchedule,
    global_unit_price_note,
)


SERVICE_DATE_PRICING_MODE = "service_date_effective"
_MEAL_ORDER = {meal: ordinal for ordinal, meal in enumerate(Meal)}


@dataclass(frozen=True, slots=True)
class UnitPriceBreakdownLine:
    """One person's entitlements for one meal and one effective unit price."""

    person_id: UUID
    meal: Meal
    unit_price: int
    service_dates: tuple[date, ...]

    def __post_init__(self) -> None:
        _validate_uuid(self.person_id, "사람 ID")
        if not isinstance(self.meal, Meal):
            raise TypeError("meal은 Meal이어야 합니다.")
        if isinstance(self.unit_price, bool) or not isinstance(self.unit_price, int):
            raise TypeError("급식 단가는 정수여야 합니다.")
        if self.unit_price < 0:
            raise ValueError("급식 단가는 0원 이상이어야 합니다.")
        dates = tuple(self.service_dates)
        if not dates:
            raise ValueError("단가 계산 근거에는 하나 이상의 서비스일이 필요합니다.")
        if any(isinstance(day, datetime) or not isinstance(day, date) for day in dates):
            raise TypeError("service_dates에는 date만 허용됩니다.")
        if tuple(sorted(set(dates))) != dates:
            raise ValueError("service_dates는 중복 없이 날짜순이어야 합니다.")
        object.__setattr__(self, "service_dates", dates)

    @property
    def count(self) -> int:
        return len(self.service_dates)

    @property
    def subtotal(self) -> int:
        return self.count * self.unit_price

    @property
    def expression(self) -> str:
        return f"{self.unit_price:,}원×{self.count}식"


@dataclass(frozen=True, slots=True)
class PersonBillingSummary:
    """One person's meal counts, mixed-price evidence, and total amount."""

    person_id: UUID
    lines: tuple[UnitPriceBreakdownLine, ...]

    def __post_init__(self) -> None:
        _validate_uuid(self.person_id, "사람 ID")
        lines = tuple(self.lines)
        if not lines:
            raise ValueError("사람별 청구 요약에는 하나 이상의 단가 근거가 필요합니다.")
        if any(not isinstance(line, UnitPriceBreakdownLine) for line in lines):
            raise TypeError("lines에는 UnitPriceBreakdownLine만 허용됩니다.")
        if any(line.person_id != self.person_id for line in lines):
            raise ValueError("단가 근거의 사람 ID가 청구 요약과 다릅니다.")
        keys = tuple((line.meal, line.unit_price) for line in lines)
        if len(set(keys)) != len(keys):
            raise ValueError("같은 식종·단가의 근거는 한 줄로 합쳐야 합니다.")
        seen_entitlements: set[tuple[Meal, date]] = set()
        for line in lines:
            for service_on in line.service_dates:
                key = (line.meal, service_on)
                if key in seen_entitlements:
                    raise ValueError("같은 식종·서비스일을 두 번 청구할 수 없습니다.")
                seen_entitlements.add(key)
        ordered = tuple(sorted(lines, key=_line_sort_key))
        object.__setattr__(self, "lines", ordered)

    def count_for(self, meal: Meal) -> int:
        if not isinstance(meal, Meal):
            raise TypeError("meal은 Meal이어야 합니다.")
        return sum(line.count for line in self.lines if line.meal is meal)

    def amount_for(self, meal: Meal) -> int:
        if not isinstance(meal, Meal):
            raise TypeError("meal은 Meal이어야 합니다.")
        return sum(line.subtotal for line in self.lines if line.meal is meal)

    def price_expression_for(self, meal: Meal) -> str:
        if not isinstance(meal, Meal):
            raise TypeError("meal은 Meal이어야 합니다.")
        return " + ".join(
            line.expression for line in self.lines if line.meal is meal
        )

    @property
    def total_count(self) -> int:
        return sum(line.count for line in self.lines)

    @property
    def total_amount(self) -> int:
        return sum(line.subtotal for line in self.lines)


@dataclass(frozen=True, slots=True)
class EntitlementBilling:
    """Deterministic date-priced billing result for all entitled people."""

    summaries: tuple[PersonBillingSummary, ...]

    def __post_init__(self) -> None:
        summaries = tuple(self.summaries)
        if any(not isinstance(item, PersonBillingSummary) for item in summaries):
            raise TypeError("summaries에는 PersonBillingSummary만 허용됩니다.")
        ids = tuple(item.person_id for item in summaries)
        if len(set(ids)) != len(ids):
            raise ValueError("사람별 청구 요약은 중복될 수 없습니다.")
        object.__setattr__(
            self,
            "summaries",
            tuple(sorted(summaries, key=lambda item: str(item.person_id))),
        )

    def summary(self, person_id: UUID) -> PersonBillingSummary:
        _validate_uuid(person_id, "사람 ID")
        for summary in self.summaries:
            if summary.person_id == person_id:
                return summary
        raise KeyError(f"청구 대상에 없는 사람입니다: {person_id}")

    @property
    def total_count(self) -> int:
        return sum(summary.total_count for summary in self.summaries)

    @property
    def total_amount(self) -> int:
        return sum(summary.total_amount for summary in self.summaries)

    def calculation_manifest(
        self,
        schedule: UnitPriceSchedule,
        *,
        period_start: date,
        period_end: date,
        meals: Sequence[Meal] = tuple(Meal),
    ) -> Mapping[str, JsonValue]:
        if not isinstance(schedule, UnitPriceSchedule):
            raise TypeError("schedule은 UnitPriceSchedule이어야 합니다.")
        note = global_unit_price_note(
            schedule,
            period_start,
            period_end,
            meals,
        )
        for summary in self.summaries:
            for line in summary.lines:
                if any(
                    not period_start <= service_on <= period_end
                    for service_on in line.service_dates
                ):
                    raise ValueError(
                        "청구 결과에 calculation manifest 대상 기간 밖의 "
                        "서비스일이 있습니다."
                    )
                if any(
                    schedule.price_for(line.meal, service_on) != line.unit_price
                    for service_on in line.service_dates
                ):
                    raise ValueError(
                        "청구 결과와 calculation manifest의 단가 schedule이 다릅니다."
                    )
        return freeze_json_object(
            {
                "unit_price_policy_version": UNIT_PRICE_POLICY_VERSION,
                "unit_price_mode": SERVICE_DATE_PRICING_MODE,
                "default_unit_price": schedule.default_unit_price,
                "period_start": period_start.isoformat(),
                "period_end": period_end.isoformat(),
                "global_unit_price_note": note,
                "total_count": self.total_count,
                "total_amount": self.total_amount,
                "people": tuple(
                    {
                        "person_id": str(summary.person_id),
                        "total_count": summary.total_count,
                        "total_amount": summary.total_amount,
                        "lines": tuple(
                            {
                                "meal": line.meal.value,
                                "unit_price": line.unit_price,
                                "count": line.count,
                                "subtotal": line.subtotal,
                            }
                            for line in summary.lines
                        ),
                    }
                    for summary in self.summaries
                ),
            }
        )


def price_entitlements(
    entitlements: Iterable[MealEntitlement],
    schedule: UnitPriceSchedule,
) -> EntitlementBilling:
    """Apply each entitlement's service-date price, then aggregate evidence."""

    if not isinstance(schedule, UnitPriceSchedule):
        raise TypeError("schedule은 UnitPriceSchedule이어야 합니다.")
    items = tuple(entitlements)
    if any(not isinstance(item, MealEntitlement) for item in items):
        raise TypeError("entitlements에는 MealEntitlement만 허용됩니다.")
    identity_keys = {
        (item.person_id, item.meal, item.service_on) for item in items
    }
    if len(identity_keys) != len(items):
        raise ValueError("같은 사람·식종·서비스일의 entitlement가 중복되었습니다.")

    dates_by_line: defaultdict[tuple[UUID, Meal, int], list[date]] = defaultdict(list)
    for entitlement in items:
        unit_price = schedule.price_for(
            entitlement.meal,
            entitlement.service_on,
        )
        dates_by_line[
            (entitlement.person_id, entitlement.meal, unit_price)
        ].append(entitlement.service_on)

    lines_by_person: defaultdict[UUID, list[UnitPriceBreakdownLine]] = defaultdict(list)
    for (person_id, meal, unit_price), service_dates in dates_by_line.items():
        lines_by_person[person_id].append(
            UnitPriceBreakdownLine(
                person_id,
                meal,
                unit_price,
                tuple(sorted(service_dates)),
            )
        )
    return EntitlementBilling(
        tuple(
            PersonBillingSummary(person_id, tuple(lines))
            for person_id, lines in lines_by_person.items()
        )
    )


def _line_sort_key(line: UnitPriceBreakdownLine) -> tuple[int, date, int]:
    return (_MEAL_ORDER[line.meal], line.service_dates[0], line.unit_price)


def _validate_uuid(value: object, label: str) -> None:
    if not isinstance(value, UUID):
        raise TypeError(f"{label}는 UUID여야 합니다.")
