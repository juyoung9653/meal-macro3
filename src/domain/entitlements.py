"""Meal entitlement decisions and physical delivery planning.

Entitlements are counted per service date. Physical shipments are a separate
projection and may bundle several breakfast service dates into one delivery.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import StrEnum
from uuid import UUID

from .roster import Meal, PersonProfile, RosterSnapshot
from .service_adjustments import ServiceProvision
from .status import MealStatus, StatusHistory


class EntitlementExclusion(StrEnum):
    NOT_ENROLLED = "미가입"
    SERVICE_UNAVAILABLE = "서비스없음"
    PAUSED = "일시중지"
    STOPPED = "중지"


@dataclass(frozen=True, slots=True)
class MealEntitlement:
    person_id: UUID
    meal: Meal
    service_on: date

    def __post_init__(self) -> None:
        _validate_uuid(self.person_id, "사람 ID")
        if not isinstance(self.meal, Meal):
            raise TypeError("meal은 Meal이어야 합니다.")
        _validate_date(self.service_on, "서비스일")


@dataclass(frozen=True, slots=True)
class EntitlementDecision:
    person_id: UUID
    meal: Meal
    service_on: date
    status: MealStatus
    exclusions: tuple[EntitlementExclusion, ...]

    def __post_init__(self) -> None:
        _validate_uuid(self.person_id, "사람 ID")
        if not isinstance(self.meal, Meal):
            raise TypeError("meal은 Meal이어야 합니다.")
        _validate_date(self.service_on, "서비스일")
        if not isinstance(self.status, MealStatus):
            raise TypeError("status는 MealStatus여야 합니다.")
        exclusions = tuple(self.exclusions)
        if any(not isinstance(item, EntitlementExclusion) for item in exclusions):
            raise TypeError("exclusions에는 EntitlementExclusion만 허용됩니다.")
        if len(set(exclusions)) != len(exclusions):
            raise ValueError("entitlement 제외 사유는 중복될 수 없습니다.")
        object.__setattr__(self, "exclusions", exclusions)

    @property
    def entitled(self) -> bool:
        return not self.exclusions

    @property
    def entitlement(self) -> MealEntitlement | None:
        if not self.entitled:
            return None
        return MealEntitlement(self.person_id, self.meal, self.service_on)


def decide_entitlement(
    roster: RosterSnapshot,
    person_id: UUID,
    meal: Meal,
    service_on: date,
    *,
    status_history: StatusHistory | None = None,
    service_available: bool = True,
    service_provision: ServiceProvision | None = None,
) -> EntitlementDecision:
    """Evaluate enrollment, service availability, and effective meal status."""

    if not isinstance(roster, RosterSnapshot):
        raise TypeError("roster는 RosterSnapshot이어야 합니다.")
    roster.person(person_id)
    if not isinstance(meal, Meal):
        raise TypeError("meal은 Meal이어야 합니다.")
    _validate_date(service_on, "서비스일")
    if not isinstance(service_available, bool):
        raise TypeError("service_available은 bool이어야 합니다.")
    if service_provision is not None:
        if not isinstance(service_provision, ServiceProvision):
            raise TypeError("service_provision은 ServiceProvision 또는 None이어야 합니다.")
        if (
            service_provision.meal is not meal
            or service_provision.service_on != service_on
        ):
            raise ValueError("휴무·대체식 결정의 식종·원래 식사일이 다릅니다.")
    if status_history is not None:
        if not isinstance(status_history, StatusHistory):
            raise TypeError("status_history는 StatusHistory 또는 None이어야 합니다.")
        if status_history.person_id != person_id or status_history.meal is not meal:
            raise ValueError("상태 이력의 사람·식종이 entitlement 대상과 다릅니다.")

    enrolled = meal in roster.meals_for(person_id, on=service_on)
    status = (
        MealStatus.DELIVERY
        if status_history is None
        else status_history.status_on(service_on)
    )
    exclusions: list[EntitlementExclusion] = []
    if not enrolled:
        exclusions.append(EntitlementExclusion.NOT_ENROLLED)
    if not service_available or (
        service_provision is not None and not service_provision.billable
    ):
        exclusions.append(EntitlementExclusion.SERVICE_UNAVAILABLE)
    if status is MealStatus.PAUSED:
        exclusions.append(EntitlementExclusion.PAUSED)
    elif status is MealStatus.STOPPED:
        exclusions.append(EntitlementExclusion.STOPPED)
    return EntitlementDecision(person_id, meal, service_on, status, tuple(exclusions))


class SchoolLunchMark(StrEnum):
    SCHOOL_PROVIDES = "O"
    VENDOR_REQUIRED = "X"


@dataclass(frozen=True, slots=True)
class LunchDayRule:
    """Typed result of one school's schedule row for one weekday."""

    mark: SchoolLunchMark
    grades_without_school_meal: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if not isinstance(self.mark, SchoolLunchMark):
            raise TypeError("mark는 SchoolLunchMark여야 합니다.")
        grades = frozenset(
            _required_text(grade, "학교 미급식 학년")
            for grade in self.grades_without_school_meal
        )
        object.__setattr__(self, "grades_without_school_meal", grades)

    def vendor_service_available(self, profile: PersonProfile, service_on: date) -> bool:
        if not isinstance(profile, PersonProfile):
            raise TypeError("profile은 PersonProfile이어야 합니다.")
        _validate_date(service_on, "중식 서비스일")
        if service_on.weekday() >= 5:
            return False
        if self.mark is SchoolLunchMark.VENDOR_REQUIRED:
            return True
        return (
            profile.grade is not None
            and profile.grade in self.grades_without_school_meal
        )


@dataclass(frozen=True, slots=True)
class PhysicalDeliveryCalendar:
    """Weekday-default calendar with explicit closed/open exceptions."""

    closed_days: frozenset[date] = field(default_factory=frozenset)
    opened_days: frozenset[date] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        closed = frozenset(self.closed_days)
        opened = frozenset(self.opened_days)
        for day in (*closed, *opened):
            _validate_date(day, "물리 배송일")
        overlap = closed & opened
        if overlap:
            raise ValueError(f"배송 휴무일과 예외 영업일이 겹칩니다: {sorted(overlap)}")
        object.__setattr__(self, "closed_days", closed)
        object.__setattr__(self, "opened_days", opened)

    def is_open(self, day: date) -> bool:
        _validate_date(day, "물리 배송 조회일")
        if day in self.opened_days:
            return True
        if day in self.closed_days:
            return False
        return day.weekday() < 5


class ClosedFridayMode(StrEnum):
    THURSDAY_WEEKEND_ONLY = "목요일_토일배송"
    SKIP_FRIDAY_WEEKEND = "금토일_미배송"


@dataclass(frozen=True, slots=True)
class BreakfastDeliveryRule:
    version: str
    closed_friday_mode: ClosedFridayMode

    def __post_init__(self) -> None:
        object.__setattr__(self, "version", _required_text(self.version, "조식 규칙 version"))
        if not isinstance(self.closed_friday_mode, ClosedFridayMode):
            raise TypeError("closed_friday_mode은 ClosedFridayMode여야 합니다.")


class UnassignedReason(StrEnum):
    DELIVERY_DAY_CLOSED = "배송일휴무"
    CLOSED_FRIDAY_POLICY = "금요일휴무정책"


@dataclass(frozen=True, slots=True)
class UnassignedEntitlement:
    entitlement: MealEntitlement
    reason: UnassignedReason

    def __post_init__(self) -> None:
        if not isinstance(self.entitlement, MealEntitlement):
            raise TypeError("entitlement는 MealEntitlement이어야 합니다.")
        if not isinstance(self.reason, UnassignedReason):
            raise TypeError("reason은 UnassignedReason이어야 합니다.")


@dataclass(frozen=True, slots=True)
class PhysicalShipment:
    person_id: UUID
    meal: Meal
    delivery_on: date
    service_dates: tuple[date, ...]

    def __post_init__(self) -> None:
        _validate_uuid(self.person_id, "사람 ID")
        if not isinstance(self.meal, Meal):
            raise TypeError("meal은 Meal이어야 합니다.")
        _validate_date(self.delivery_on, "물리 배송일")
        service_dates = tuple(self.service_dates)
        if not service_dates:
            raise ValueError("물리 배송에는 하나 이상의 entitlement가 필요합니다.")
        if any(
            isinstance(day, datetime) or not isinstance(day, date)
            for day in service_dates
        ):
            raise TypeError("service_dates에는 date만 허용됩니다.")
        if tuple(sorted(set(service_dates))) != service_dates:
            raise ValueError("service_dates는 중복 없이 날짜순이어야 합니다.")
        object.__setattr__(self, "service_dates", service_dates)

    @property
    def entitlement_count(self) -> int:
        return len(self.service_dates)


@dataclass(frozen=True, slots=True)
class BreakfastShipmentPlan:
    rule_version: str
    shipments: tuple[PhysicalShipment, ...]
    unassigned: tuple[UnassignedEntitlement, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "rule_version", _required_text(self.rule_version, "규칙 version"))
        shipments = tuple(self.shipments)
        unassigned = tuple(self.unassigned)
        if any(not isinstance(item, PhysicalShipment) for item in shipments):
            raise TypeError("shipments에는 PhysicalShipment만 허용됩니다.")
        if any(not isinstance(item, UnassignedEntitlement) for item in unassigned):
            raise TypeError("unassigned에는 UnassignedEntitlement만 허용됩니다.")
        object.__setattr__(self, "shipments", shipments)
        object.__setattr__(self, "unassigned", unassigned)

    @property
    def entitlement_count(self) -> int:
        return sum(item.entitlement_count for item in self.shipments) + len(
            self.unassigned
        )


def plan_breakfast_shipments(
    entitlements: Iterable[MealEntitlement],
    calendar: PhysicalDeliveryCalendar,
    rule: BreakfastDeliveryRule,
) -> BreakfastShipmentPlan:
    """Bundle breakfast service dates without changing entitlement counts."""

    if not isinstance(calendar, PhysicalDeliveryCalendar):
        raise TypeError("calendar는 PhysicalDeliveryCalendar여야 합니다.")
    if not isinstance(rule, BreakfastDeliveryRule):
        raise TypeError("rule은 BreakfastDeliveryRule이어야 합니다.")
    items = tuple(entitlements)
    if any(not isinstance(item, MealEntitlement) for item in items):
        raise TypeError("entitlements에는 MealEntitlement만 허용됩니다.")
    if any(item.meal is not Meal.BREAKFAST for item in items):
        raise ValueError("조식 물리 배송에는 조식 entitlement만 허용됩니다.")
    identity_keys = {(item.person_id, item.service_on) for item in items}
    if len(identity_keys) != len(items):
        raise ValueError("같은 사람·서비스일의 조식 entitlement가 중복되었습니다.")

    grouped: defaultdict[tuple[UUID, date], list[date]] = defaultdict(list)
    unassigned: list[UnassignedEntitlement] = []
    for entitlement in sorted(
        items,
        key=lambda item: (str(item.person_id), item.service_on),
    ):
        delivery_on, reason = _breakfast_delivery_date(entitlement.service_on, calendar, rule)
        if delivery_on is None:
            assert reason is not None
            unassigned.append(UnassignedEntitlement(entitlement, reason))
        else:
            grouped[(entitlement.person_id, delivery_on)].append(
                entitlement.service_on
            )

    shipments = tuple(
        PhysicalShipment(
            person_id,
            Meal.BREAKFAST,
            delivery_on,
            tuple(sorted(service_dates)),
        )
        for (person_id, delivery_on), service_dates in sorted(
            grouped.items(),
            key=lambda item: (item[0][1], str(item[0][0])),
        )
    )
    return BreakfastShipmentPlan(rule.version, shipments, tuple(unassigned))


def _breakfast_delivery_date(
    service_on: date,
    calendar: PhysicalDeliveryCalendar,
    rule: BreakfastDeliveryRule,
) -> tuple[date | None, UnassignedReason | None]:
    weekday = service_on.weekday()
    if weekday <= 3:
        if calendar.is_open(service_on):
            return service_on, None
        return None, UnassignedReason.DELIVERY_DAY_CLOSED

    friday = service_on - timedelta(days=weekday - 4)
    if calendar.is_open(friday):
        return friday, None
    if weekday == 4:
        return None, UnassignedReason.CLOSED_FRIDAY_POLICY
    if rule.closed_friday_mode is ClosedFridayMode.SKIP_FRIDAY_WEEKEND:
        return None, UnassignedReason.CLOSED_FRIDAY_POLICY

    thursday = friday - timedelta(days=1)
    if calendar.is_open(thursday):
        return thursday, None
    return None, UnassignedReason.DELIVERY_DAY_CLOSED


def _validate_uuid(value: object, label: str) -> None:
    if not isinstance(value, UUID):
        raise TypeError(f"{label}는 UUID여야 합니다.")


def _validate_date(value: object, label: str) -> None:
    if isinstance(value, datetime) or not isinstance(value, date):
        raise TypeError(f"{label}은 date여야 합니다.")


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label}은 문자열이어야 합니다.")
    normalized = " ".join(value.split())
    if not normalized:
        raise ValueError(f"{label}은 비어 있을 수 없습니다.")
    return normalized
