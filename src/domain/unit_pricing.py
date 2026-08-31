"""Effective-dated meal unit prices recorded in the revision ledger."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from types import MappingProxyType
from uuid import UUID

from .revisions import (
    AuditChange,
    ChangeOperation,
    HistoricalSnapshot,
    JsonValue,
    RevisionActor,
    RevisionMetadata,
    RevisionRecord,
    RevisionSource,
)
from .roster import Meal


DEFAULT_MEAL_UNIT_PRICE = 10_000
UNIT_PRICE_POLICY_VERSION = 1
UNIT_PRICE_SCHEDULE_KIND = "meal_unit_price_schedule"
UNIT_PRICE_SCHEDULE_ID = UUID("87a82049-0d0e-50bd-8279-ff9bba7973bf")


@dataclass(frozen=True, slots=True)
class UnitPriceChange:
    """One raw user instruction applied to one or more meal types."""

    change_id: UUID
    meals: tuple[Meal, ...]
    unit_price: int
    effective_on: date
    recorded_at: datetime

    def __post_init__(self) -> None:
        _validate_uuid(self.change_id, "단가 변경 ID")
        object.__setattr__(self, "meals", _canonical_meals(self.meals))
        object.__setattr__(self, "unit_price", _unit_price(self.unit_price))
        _validate_date(self.effective_on, "단가 적용일")
        _validate_datetime(self.recorded_at, "단가 기록 시각")


@dataclass(frozen=True, slots=True)
class UnitPriceSchedule:
    """Default price plus immutable effective-dated user changes."""

    default_unit_price: int = DEFAULT_MEAL_UNIT_PRICE
    changes: tuple[UnitPriceChange, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        default = _unit_price(self.default_unit_price)
        if default != DEFAULT_MEAL_UNIT_PRICE:
            raise ValueError(
                f"기본 급식 단가는 {DEFAULT_MEAL_UNIT_PRICE:,}원이어야 합니다."
            )
        changes = tuple(self.changes)
        if any(not isinstance(change, UnitPriceChange) for change in changes):
            raise TypeError("changes에는 UnitPriceChange만 허용됩니다.")
        ids = tuple(change.change_id for change in changes)
        if len(set(ids)) != len(ids):
            raise ValueError("단가 변경 ID는 중복될 수 없습니다.")
        object.__setattr__(self, "default_unit_price", default)
        object.__setattr__(self, "changes", changes)

    def price_for(self, meal: Meal, on: date) -> int:
        if not isinstance(meal, Meal):
            raise TypeError("meal은 Meal이어야 합니다.")
        _validate_date(on, "단가 조회일")
        price = self.default_unit_price
        winning_key: tuple[date, int] | None = None
        for ordinal, change in enumerate(self.changes):
            if meal not in change.meals or change.effective_on > on:
                continue
            key = (change.effective_on, ordinal)
            if winning_key is None or key > winning_key:
                winning_key = key
                price = change.unit_price
        return price

    def prices_on(self, on: date) -> Mapping[Meal, int]:
        _validate_date(on, "단가 조회일")
        return MappingProxyType({meal: self.price_for(meal, on) for meal in Meal})

    def with_change(self, change: UnitPriceChange) -> UnitPriceSchedule:
        if not isinstance(change, UnitPriceChange):
            raise TypeError("change는 UnitPriceChange여야 합니다.")
        return UnitPriceSchedule(
            default_unit_price=self.default_unit_price,
            changes=(*self.changes, change),
        )


@dataclass(frozen=True, slots=True)
class UnitPricePeriodSegment:
    """One inclusive price period inside a requested billing range."""

    meal: Meal
    starts_on: date
    ends_on: date
    unit_price: int

    def __post_init__(self) -> None:
        if not isinstance(self.meal, Meal):
            raise TypeError("meal은 Meal이어야 합니다.")
        _validate_date(self.starts_on, "단가 구간 시작일")
        _validate_date(self.ends_on, "단가 구간 종료일")
        if self.ends_on < self.starts_on:
            raise ValueError("단가 구간 종료일은 시작일보다 빠를 수 없습니다.")
        object.__setattr__(self, "unit_price", _unit_price(self.unit_price))


@dataclass(frozen=True, slots=True)
class PreparedUnitPriceRevision:
    schedule: UnitPriceSchedule
    change: UnitPriceChange
    record: RevisionRecord


def unit_price_period_segments(
    schedule: UnitPriceSchedule,
    meal: Meal,
    starts_on: date,
    ends_on: date,
) -> tuple[UnitPricePeriodSegment, ...]:
    """Resolve contiguous price segments for one meal in a closed date range."""

    if not isinstance(schedule, UnitPriceSchedule):
        raise TypeError("schedule은 UnitPriceSchedule이어야 합니다.")
    if not isinstance(meal, Meal):
        raise TypeError("meal은 Meal이어야 합니다.")
    _validate_closed_period(starts_on, ends_on)

    current_start = starts_on
    current_price = schedule.price_for(meal, starts_on)
    segments: list[UnitPricePeriodSegment] = []
    boundaries = sorted(
        {
            change.effective_on
            for change in schedule.changes
            if meal in change.meals and starts_on < change.effective_on <= ends_on
        }
    )
    for boundary in boundaries:
        next_price = schedule.price_for(meal, boundary)
        if next_price == current_price:
            continue
        segments.append(
            UnitPricePeriodSegment(
                meal,
                current_start,
                boundary - timedelta(days=1),
                current_price,
            )
        )
        current_start = boundary
        current_price = next_price
    segments.append(
        UnitPricePeriodSegment(meal, current_start, ends_on, current_price)
    )
    return tuple(segments)


def global_unit_price_note(
    schedule: UnitPriceSchedule,
    starts_on: date,
    ends_on: date,
    meals: Sequence[Meal] = tuple(Meal),
) -> str | None:
    """Describe only meals whose price genuinely changes inside the range."""

    if not isinstance(schedule, UnitPriceSchedule):
        raise TypeError("schedule은 UnitPriceSchedule이어야 합니다.")
    _validate_closed_period(starts_on, ends_on)
    selected_meals = _canonical_meals(meals)

    grouped: dict[
        tuple[tuple[date, date, int], ...],
        list[Meal],
    ] = {}
    for meal in selected_meals:
        segments = unit_price_period_segments(schedule, meal, starts_on, ends_on)
        if len(segments) == 1:
            continue
        signature = tuple(
            (segment.starts_on, segment.ends_on, segment.unit_price)
            for segment in segments
        )
        grouped.setdefault(signature, []).append(meal)

    if not grouped:
        return None

    clauses: list[str] = []
    for signature, grouped_meals in grouped.items():
        meal_label = "·".join(meal.value for meal in grouped_meals)
        periods = " / ".join(
            f"{_format_period_label(period_start, period_end, starts_on, ends_on)} "
            f"{unit_price:,}원"
            for period_start, period_end, unit_price in signature
        )
        clauses.append(f"{meal_label} 단가: {periods}")
    return "; ".join(clauses)


def build_unit_price_revision(
    current_revision_id: UUID,
    stored_state: Mapping[str, JsonValue] | None,
    meals: tuple[Meal, ...],
    unit_price: int,
    effective_on: date,
    *,
    actor: RevisionActor,
    recorded_at: datetime,
    revision_id: UUID,
    change_id: UUID,
    reason: str = "급식 단가 변경",
) -> PreparedUnitPriceRevision:
    """Append one multi-meal price instruction as a normal UI revision."""

    _validate_uuid(current_revision_id, "현재 revision ID")
    _validate_uuid(revision_id, "새 revision ID")
    if revision_id == current_revision_id:
        raise ValueError("새 revision ID는 현재 revision과 달라야 합니다.")
    if not isinstance(actor, RevisionActor):
        raise TypeError("actor는 RevisionActor여야 합니다.")
    _validate_datetime(recorded_at, "단가 기록 시각")
    selected_meals = _canonical_meals(meals)
    change = UnitPriceChange(
        change_id=change_id,
        meals=selected_meals,
        unit_price=unit_price,
        effective_on=effective_on,
        recorded_at=recorded_at,
    )
    schedule = unit_price_schedule_from_state(stored_state)
    if any(existing.change_id == change.change_id for existing in schedule.changes):
        raise ValueError("이미 사용한 단가 변경 ID입니다.")
    updated = schedule.with_change(change)
    after = unit_price_schedule_state(updated)
    operation = (
        ChangeOperation.INSERT
        if stored_state is None
        else ChangeOperation.UPDATE
    )
    audit_change = AuditChange(
        UNIT_PRICE_SCHEDULE_KIND,
        UNIT_PRICE_SCHEDULE_ID,
        operation,
        before=stored_state,
        after=after,
    )
    metadata = RevisionMetadata(
        revision_id=revision_id,
        parent_revision_id=current_revision_id,
        recorded_at=recorded_at,
        effective_on=effective_on,
        actor=actor,
        reason=reason,
        source=RevisionSource.UI,
        decisions={
            "unit_price_change": {
                "change_id": str(change_id),
                "meals": [meal.value for meal in selected_meals],
                "unit_price": change.unit_price,
                "effective_on": effective_on.isoformat(),
            }
        },
        rule_versions={"unit_price_policy_version": UNIT_PRICE_POLICY_VERSION},
    )
    return PreparedUnitPriceRevision(
        updated,
        change,
        RevisionRecord(metadata, (audit_change,)),
    )


def unit_price_schedule_from_snapshot(
    snapshot: HistoricalSnapshot,
) -> UnitPriceSchedule:
    if not isinstance(snapshot, HistoricalSnapshot):
        raise TypeError("snapshot은 HistoricalSnapshot이어야 합니다.")
    return unit_price_schedule_from_state(
        snapshot.entity(UNIT_PRICE_SCHEDULE_KIND, UNIT_PRICE_SCHEDULE_ID)
    )


def unit_price_schedule_state(
    schedule: UnitPriceSchedule,
) -> Mapping[str, JsonValue]:
    if not isinstance(schedule, UnitPriceSchedule):
        raise TypeError("schedule은 UnitPriceSchedule이어야 합니다.")
    return {
        "policy_version": UNIT_PRICE_POLICY_VERSION,
        "default_unit_price": schedule.default_unit_price,
        "changes": tuple(
            {
                "change_id": str(change.change_id),
                "meals": tuple(meal.value for meal in change.meals),
                "unit_price": change.unit_price,
                "effective_on": change.effective_on.isoformat(),
                "recorded_at": change.recorded_at.isoformat(),
            }
            for change in schedule.changes
        ),
    }


def unit_price_schedule_from_state(
    state: Mapping[str, JsonValue] | None,
) -> UnitPriceSchedule:
    if state is None:
        return UnitPriceSchedule()
    if not isinstance(state, Mapping):
        raise TypeError("단가 schedule state는 mapping이어야 합니다.")
    if state.get("policy_version") != UNIT_PRICE_POLICY_VERSION:
        raise ValueError("지원하지 않는 단가 정책 version입니다.")
    default = _unit_price(state.get("default_unit_price"))
    raw_changes = state.get("changes")
    if not isinstance(raw_changes, Sequence) or isinstance(
        raw_changes, (str, bytes, bytearray)
    ):
        raise TypeError("단가 변경 이력은 배열이어야 합니다.")
    changes: list[UnitPriceChange] = []
    for raw_change in raw_changes:
        if not isinstance(raw_change, Mapping):
            raise TypeError("단가 변경 항목은 object여야 합니다.")
        raw_meals = raw_change.get("meals")
        if not isinstance(raw_meals, Sequence) or isinstance(
            raw_meals, (str, bytes, bytearray)
        ):
            raise TypeError("단가 변경 식종은 배열이어야 합니다.")
        changes.append(
            UnitPriceChange(
                change_id=UUID(_required_text(raw_change.get("change_id"), "단가 변경 ID")),
                meals=tuple(Meal(_required_text(value, "식종")) for value in raw_meals),
                unit_price=_unit_price(raw_change.get("unit_price")),
                effective_on=date.fromisoformat(
                    _required_text(raw_change.get("effective_on"), "단가 적용일")
                ),
                recorded_at=datetime.fromisoformat(
                    _required_text(raw_change.get("recorded_at"), "단가 기록 시각")
                ),
            )
        )
    return UnitPriceSchedule(default, tuple(changes))


def _canonical_meals(meals: Sequence[Meal]) -> tuple[Meal, ...]:
    values = tuple(meals)
    if not values:
        raise ValueError("하나 이상의 식종을 선택해야 합니다.")
    if any(not isinstance(meal, Meal) for meal in values):
        raise TypeError("meals에는 Meal만 허용됩니다.")
    if len(set(values)) != len(values):
        raise ValueError("선택한 식종은 중복될 수 없습니다.")
    return tuple(meal for meal in Meal if meal in values)


def _unit_price(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("급식 단가는 정수여야 합니다.")
    if value < 0:
        raise ValueError("급식 단가는 0원 이상이어야 합니다.")
    return value


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label}은 문자열이어야 합니다.")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{label}은 비어 있을 수 없습니다.")
    return normalized


def _validate_uuid(value: object, label: str) -> None:
    if not isinstance(value, UUID):
        raise TypeError(f"{label}는 UUID여야 합니다.")


def _validate_date(value: object, label: str) -> None:
    if isinstance(value, datetime) or not isinstance(value, date):
        raise TypeError(f"{label}은 date여야 합니다.")


def _validate_datetime(value: object, label: str) -> None:
    if not isinstance(value, datetime):
        raise TypeError(f"{label}은 datetime이어야 합니다.")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label}에는 시간대가 있어야 합니다.")


def _validate_closed_period(starts_on: object, ends_on: object) -> None:
    _validate_date(starts_on, "조회 시작일")
    _validate_date(ends_on, "조회 종료일")
    if ends_on < starts_on:
        raise ValueError("조회 종료일은 시작일보다 빠를 수 없습니다.")


def _format_period_label(
    period_start: date,
    period_end: date,
    requested_start: date,
    requested_end: date,
) -> str:
    same_billing_month = (
        requested_start.year == requested_end.year
        and requested_start.month == requested_end.month
    )
    if same_billing_month:
        if period_start == period_end:
            return f"{period_start.day}일"
        return f"{period_start.day}~{period_end.day}일"
    if period_start.year == period_end.year:
        if period_start == period_end:
            return f"{period_start.month}/{period_start.day}"
        return (
            f"{period_start.month}/{period_start.day}~"
            f"{period_end.month}/{period_end.day}"
        )
    if period_start == period_end:
        return period_start.isoformat()
    return f"{period_start.isoformat()}~{period_end.isoformat()}"
