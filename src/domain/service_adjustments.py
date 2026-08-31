"""Vendor-specific closures and replacement-meal delivery mappings."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import StrEnum
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
from .public_holidays import PublicHolidayCandidate
from .roster import InclusiveDateRange, Meal


SERVICE_ADJUSTMENT_POLICY_VERSION = 1
SERVICE_ADJUSTMENT_SCHEDULE_KIND = "meal_service_adjustment_schedule"
SERVICE_ADJUSTMENT_SCHEDULE_ID = UUID(
    "6f9c24b2-6e73-5cd5-9610-677654380894"
)
_MEAL_ORDER = {meal: ordinal for ordinal, meal in enumerate(Meal)}


class ServiceTreatment(StrEnum):
    """How one original service date is fulfilled and billed."""

    NORMAL = "normal"
    NOT_PROVIDED = "not_provided"
    REPLACEMENT = "replacement"

    @property
    def label(self) -> str:
        return {
            ServiceTreatment.NORMAL: "정상 운영",
            ServiceTreatment.NOT_PROVIDED: "미제공 · 청구 제외",
            ServiceTreatment.REPLACEMENT: "대체식 제공 · 청구 포함",
        }[self]


class ServiceAdjustmentOrigin(StrEnum):
    """Why the operator opened and confirmed this instruction."""

    MANUAL = "manual"
    PUBLIC_HOLIDAY = "public_holiday"


@dataclass(frozen=True, slots=True)
class ReplacementDeliveryOverride:
    """One meal/service-date exception to a shared replacement date."""

    meal: Meal
    service_on: date
    delivery_on: date

    def __post_init__(self) -> None:
        if not isinstance(self.meal, Meal):
            raise TypeError("대체식 식종은 Meal이어야 합니다.")
        _validate_date(self.service_on, "원래 식사일")
        _validate_date(self.delivery_on, "대체식 제공일")


@dataclass(frozen=True, slots=True)
class ServiceAdjustmentChange:
    """One append-only closure or replacement instruction."""

    change_id: UUID
    period: InclusiveDateRange
    meals: tuple[Meal, ...]
    treatment: ServiceTreatment
    default_delivery_on: date | None
    delivery_overrides: tuple[ReplacementDeliveryOverride, ...]
    note: str
    recorded_at: datetime
    origin: ServiceAdjustmentOrigin = ServiceAdjustmentOrigin.MANUAL
    public_holiday: PublicHolidayCandidate | None = None

    def __post_init__(self) -> None:
        _validate_uuid(self.change_id, "휴무·대체식 변경 ID")
        if not isinstance(self.period, InclusiveDateRange):
            raise TypeError("휴무 기간은 InclusiveDateRange여야 합니다.")
        if self.period.starts_on is None or self.period.ends_on is None:
            raise ValueError("휴무 기간에는 시작일과 종료일이 모두 필요합니다.")
        object.__setattr__(self, "meals", _canonical_meals(self.meals))
        if not isinstance(self.treatment, ServiceTreatment):
            raise TypeError("식사 처리는 ServiceTreatment여야 합니다.")
        if self.default_delivery_on is not None:
            _validate_date(self.default_delivery_on, "기본 대체식 제공일")
        overrides = tuple(self.delivery_overrides)
        if any(
            not isinstance(item, ReplacementDeliveryOverride)
            for item in overrides
        ):
            raise TypeError(
                "대체식 개별 제공일에는 ReplacementDeliveryOverride만 허용됩니다."
            )
        keys = tuple((item.meal, item.service_on) for item in overrides)
        if len(set(keys)) != len(keys):
            raise ValueError("같은 식종·원래 식사일의 대체식 제공일이 중복되었습니다.")
        for item in overrides:
            if item.meal not in self.meals:
                raise ValueError("대체식 개별 제공일의 식종이 선택 범위 밖입니다.")
            if not self.period.contains(item.service_on):
                raise ValueError("대체식 개별 제공일의 원래 식사일이 휴무 기간 밖입니다.")
        if self.treatment is ServiceTreatment.REPLACEMENT:
            if self.default_delivery_on is None:
                raise ValueError("대체식 제공에는 기본 제공일이 필요합니다.")
        elif self.default_delivery_on is not None or overrides:
            raise ValueError("미제공·정상 운영에는 대체식 제공일을 지정할 수 없습니다.")
        object.__setattr__(
            self,
            "delivery_overrides",
            tuple(
                sorted(
                    overrides,
                    key=lambda item: (
                        item.service_on,
                        _MEAL_ORDER[item.meal],
                    ),
                )
            ),
        )
        object.__setattr__(self, "note", _required_text(self.note, "휴무 사유"))
        _validate_datetime(self.recorded_at, "휴무·대체식 기록 시각")
        if not isinstance(self.origin, ServiceAdjustmentOrigin):
            raise TypeError("휴무·대체식 입력 경로가 올바르지 않습니다.")
        if self.origin is ServiceAdjustmentOrigin.MANUAL:
            if self.public_holiday is not None:
                raise ValueError("직접 추가 일정에는 공휴일 후보를 연결할 수 없습니다.")
        else:
            if not isinstance(self.public_holiday, PublicHolidayCandidate):
                raise TypeError("공휴일 검토에는 원본 공휴일 후보가 필요합니다.")
            if (
                self.period.starts_on != self.public_holiday.day
                or self.period.ends_on != self.public_holiday.day
            ):
                raise ValueError("공휴일 검토 기간은 후보 날짜 하루와 같아야 합니다.")

    def delivery_on_for(self, meal: Meal, service_on: date) -> date:
        """Resolve the shared or customized replacement date."""

        if self.treatment is not ServiceTreatment.REPLACEMENT:
            raise ValueError("대체식 변경에서만 실제 제공일을 조회할 수 있습니다.")
        if meal not in self.meals or not self.period.contains(service_on):
            raise ValueError("식종·원래 식사일이 이 대체식 변경 범위 밖입니다.")
        for override in self.delivery_overrides:
            if override.meal is meal and override.service_on == service_on:
                return override.delivery_on
        assert self.default_delivery_on is not None
        return self.default_delivery_on


@dataclass(frozen=True, slots=True)
class ServiceProvision:
    """Resolved billing and delivery facts for one original meal date."""

    meal: Meal
    service_on: date
    treatment: ServiceTreatment
    delivery_on: date | None
    change_id: UUID | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.meal, Meal):
            raise TypeError("식종은 Meal이어야 합니다.")
        _validate_date(self.service_on, "원래 식사일")
        if not isinstance(self.treatment, ServiceTreatment):
            raise TypeError("식사 처리는 ServiceTreatment여야 합니다.")
        if self.delivery_on is not None:
            _validate_date(self.delivery_on, "실제 제공일")
        if self.change_id is not None:
            _validate_uuid(self.change_id, "휴무·대체식 변경 ID")
        if self.treatment is ServiceTreatment.NOT_PROVIDED:
            if self.delivery_on is not None:
                raise ValueError("미제공 식사에는 실제 제공일이 없어야 합니다.")
        elif self.delivery_on is None:
            raise ValueError("정상 운영·대체식 제공에는 실제 제공일이 필요합니다.")

    @property
    def billable(self) -> bool:
        return self.treatment is not ServiceTreatment.NOT_PROVIDED


@dataclass(frozen=True, slots=True)
class ServiceAdjustmentSchedule:
    """Append-only instructions resolved by latest matching ledger order."""

    changes: tuple[ServiceAdjustmentChange, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        changes = tuple(self.changes)
        if any(
            not isinstance(change, ServiceAdjustmentChange)
            for change in changes
        ):
            raise TypeError("changes에는 ServiceAdjustmentChange만 허용됩니다.")
        ids = tuple(change.change_id for change in changes)
        if len(set(ids)) != len(ids):
            raise ValueError("휴무·대체식 변경 ID는 중복될 수 없습니다.")
        object.__setattr__(self, "changes", changes)

    def provision_for(self, meal: Meal, service_on: date) -> ServiceProvision:
        if not isinstance(meal, Meal):
            raise TypeError("식종은 Meal이어야 합니다.")
        _validate_date(service_on, "원래 식사일")
        winner: ServiceAdjustmentChange | None = None
        for change in self.changes:
            if meal in change.meals and change.period.contains(service_on):
                winner = change
        if winner is None or winner.treatment is ServiceTreatment.NORMAL:
            return ServiceProvision(
                meal,
                service_on,
                ServiceTreatment.NORMAL,
                service_on,
                None if winner is None else winner.change_id,
            )
        if winner.treatment is ServiceTreatment.NOT_PROVIDED:
            return ServiceProvision(
                meal,
                service_on,
                winner.treatment,
                None,
                winner.change_id,
            )
        return ServiceProvision(
            meal,
            service_on,
            winner.treatment,
            winner.delivery_on_for(meal, service_on),
            winner.change_id,
        )

    def with_change(
        self,
        change: ServiceAdjustmentChange,
    ) -> ServiceAdjustmentSchedule:
        if not isinstance(change, ServiceAdjustmentChange):
            raise TypeError("change는 ServiceAdjustmentChange여야 합니다.")
        return ServiceAdjustmentSchedule((*self.changes, change))

    def public_holiday_treatments_for(
        self,
        holiday_on: date,
    ) -> Mapping[Meal, ServiceTreatment]:
        """Return the latest explicit holiday review for each meal."""

        _validate_date(holiday_on, "공휴일 날짜")
        treatments: dict[Meal, ServiceTreatment] = {}
        for change in self.changes:
            if (
                change.origin is not ServiceAdjustmentOrigin.PUBLIC_HOLIDAY
                or change.public_holiday is None
                or change.public_holiday.day != holiday_on
            ):
                continue
            for meal in change.meals:
                treatments[meal] = change.treatment
        return MappingProxyType(treatments)


@dataclass(frozen=True, slots=True)
class PreparedServiceAdjustmentRevision:
    schedule: ServiceAdjustmentSchedule
    change: ServiceAdjustmentChange
    record: RevisionRecord


@dataclass(frozen=True, slots=True)
class PreparedPublicHolidayReviewRevision:
    schedule: ServiceAdjustmentSchedule
    changes: tuple[ServiceAdjustmentChange, ...]
    record: RevisionRecord


def previous_common_operating_day(
    schedule: ServiceAdjustmentSchedule,
    meals: Sequence[Meal],
    starts_on: date,
    *,
    excluded_days: Sequence[date] = (),
) -> date:
    """Suggest the prior weekday without a recorded closure for any meal."""

    if not isinstance(schedule, ServiceAdjustmentSchedule):
        raise TypeError("schedule은 ServiceAdjustmentSchedule이어야 합니다.")
    selected_meals = _canonical_meals(meals)
    _validate_date(starts_on, "휴무 시작일")
    excluded = frozenset(excluded_days)
    for excluded_day in excluded:
        _validate_date(excluded_day, "제외할 운영일")
    candidate = starts_on - timedelta(days=1)
    for _attempt in range(3_660):
        if (
            candidate.weekday() < 5
            and candidate not in excluded
            and all(
            schedule.provision_for(meal, candidate).treatment
            is ServiceTreatment.NORMAL
            for meal in selected_meals
            )
        ):
            return candidate
        candidate -= timedelta(days=1)
    raise ValueError("직전 운영일을 찾지 못했습니다.")


def build_service_adjustment_revision(
    current_revision_id: UUID,
    stored_state: Mapping[str, JsonValue] | None,
    period: InclusiveDateRange,
    meals: tuple[Meal, ...],
    treatment: ServiceTreatment,
    default_delivery_on: date | None,
    delivery_overrides: tuple[ReplacementDeliveryOverride, ...],
    note: str,
    *,
    actor: RevisionActor,
    recorded_at: datetime,
    revision_id: UUID,
    change_id: UUID,
    origin: ServiceAdjustmentOrigin = ServiceAdjustmentOrigin.MANUAL,
    public_holiday: PublicHolidayCandidate | None = None,
    reason: str = "휴무·대체식 일정 저장",
) -> PreparedServiceAdjustmentRevision:
    """Append one vendor-local service instruction as a UI revision."""

    _validate_uuid(current_revision_id, "현재 revision ID")
    _validate_uuid(revision_id, "새 revision ID")
    if revision_id == current_revision_id:
        raise ValueError("새 revision ID는 현재 revision과 달라야 합니다.")
    if not isinstance(actor, RevisionActor):
        raise TypeError("actor는 RevisionActor여야 합니다.")
    _validate_datetime(recorded_at, "휴무·대체식 기록 시각")
    change = ServiceAdjustmentChange(
        change_id,
        period,
        meals,
        treatment,
        default_delivery_on,
        delivery_overrides,
        note,
        recorded_at,
        origin,
        public_holiday,
    )
    schedule = service_adjustment_schedule_from_state(stored_state)
    if any(existing.change_id == change.change_id for existing in schedule.changes):
        raise ValueError("이미 사용한 휴무·대체식 변경 ID입니다.")
    updated = schedule.with_change(change)
    after = service_adjustment_schedule_state(updated)
    operation = (
        ChangeOperation.INSERT
        if stored_state is None
        else ChangeOperation.UPDATE
    )
    audit_change = AuditChange(
        SERVICE_ADJUSTMENT_SCHEDULE_KIND,
        SERVICE_ADJUSTMENT_SCHEDULE_ID,
        operation,
        before=stored_state,
        after=after,
    )
    metadata = RevisionMetadata(
        revision_id=revision_id,
        parent_revision_id=current_revision_id,
        recorded_at=recorded_at,
        effective_on=change.period.starts_on,
        actor=actor,
        reason=reason,
        source=RevisionSource.UI,
        decisions={
            "service_adjustment_change": _service_adjustment_decision(change)
        },
        rule_versions={
            "service_adjustment_policy_version": (
                SERVICE_ADJUSTMENT_POLICY_VERSION
            )
        },
    )
    return PreparedServiceAdjustmentRevision(
        updated,
        change,
        RevisionRecord(metadata, (audit_change,)),
    )


def build_public_holiday_review_revision(
    current_revision_id: UUID,
    stored_state: Mapping[str, JsonValue] | None,
    public_holidays: tuple[PublicHolidayCandidate, ...],
    meals: tuple[Meal, ...],
    treatment: ServiceTreatment,
    replacement_deliveries: tuple[ReplacementDeliveryOverride, ...],
    *,
    actor: RevisionActor,
    recorded_at: datetime,
    revision_id: UUID,
    change_ids: tuple[UUID, ...],
    reason: str = "공휴일 운영 일괄 검토 저장",
) -> PreparedPublicHolidayReviewRevision:
    """Append N holiday decisions in one schedule audit and one revision."""

    _validate_uuid(current_revision_id, "현재 revision ID")
    _validate_uuid(revision_id, "새 revision ID")
    if revision_id == current_revision_id:
        raise ValueError("새 revision ID는 현재 revision과 달라야 합니다.")
    if not isinstance(actor, RevisionActor):
        raise TypeError("actor는 RevisionActor여야 합니다.")
    _validate_datetime(recorded_at, "공휴일 검토 기록 시각")
    if not isinstance(treatment, ServiceTreatment):
        raise TypeError("식사 처리는 ServiceTreatment여야 합니다.")
    selected_meals = _canonical_meals(meals)
    candidates = tuple(public_holidays)
    if not candidates:
        raise ValueError("하나 이상의 공휴일 후보를 선택해야 합니다.")
    if any(
        not isinstance(candidate, PublicHolidayCandidate)
        for candidate in candidates
    ):
        raise TypeError("공휴일 후보가 올바르지 않습니다.")
    if len({candidate.day for candidate in candidates}) != len(candidates):
        raise ValueError("같은 날짜의 공휴일 후보를 중복 선택할 수 없습니다.")
    ids = tuple(change_ids)
    if len(ids) != len(candidates):
        raise ValueError("공휴일 후보마다 변경 ID가 하나씩 필요합니다.")
    for change_id in ids:
        _validate_uuid(change_id, "공휴일 검토 변경 ID")
    if len(set(ids)) != len(ids):
        raise ValueError("공휴일 검토 변경 ID는 중복될 수 없습니다.")

    deliveries = tuple(replacement_deliveries)
    if any(
        not isinstance(item, ReplacementDeliveryOverride)
        for item in deliveries
    ):
        raise TypeError("대체식 제공일이 올바르지 않습니다.")
    actual_keys = tuple((item.service_on, item.meal) for item in deliveries)
    if len(set(actual_keys)) != len(actual_keys):
        raise ValueError("같은 공휴일·식종의 대체식 제공일이 중복되었습니다.")
    expected_keys = {
        (candidate.day, meal)
        for candidate in candidates
        for meal in selected_meals
    }
    if treatment is ServiceTreatment.REPLACEMENT:
        if set(actual_keys) != expected_keys:
            raise ValueError(
                "선택한 모든 공휴일·식종의 대체식 제공일이 필요합니다."
            )
    elif deliveries:
        raise ValueError("정상 운영·미제공에는 대체식 제공일이 없어야 합니다.")

    schedule = service_adjustment_schedule_from_state(stored_state)
    existing_ids = {change.change_id for change in schedule.changes}
    if existing_ids.intersection(ids):
        raise ValueError("이미 사용한 휴무·대체식 변경 ID입니다.")
    delivery_by_key = {
        (item.service_on, item.meal): item.delivery_on
        for item in deliveries
    }
    ordered_pairs = tuple(
        sorted(
            zip(candidates, ids, strict=True),
            key=lambda pair: pair[0].day,
        )
    )
    changes: list[ServiceAdjustmentChange] = []
    updated = schedule
    for candidate, change_id in ordered_pairs:
        default_delivery_on: date | None = None
        overrides: tuple[ReplacementDeliveryOverride, ...] = ()
        if treatment is ServiceTreatment.REPLACEMENT:
            default_delivery_on = delivery_by_key[
                (candidate.day, selected_meals[0])
            ]
            overrides = tuple(
                ReplacementDeliveryOverride(
                    meal,
                    candidate.day,
                    delivery_by_key[(candidate.day, meal)],
                )
                for meal in selected_meals
                if delivery_by_key[(candidate.day, meal)]
                != default_delivery_on
            )
        change = ServiceAdjustmentChange(
            change_id,
            InclusiveDateRange(candidate.day, candidate.day),
            selected_meals,
            treatment,
            default_delivery_on,
            overrides,
            candidate.name,
            recorded_at,
            ServiceAdjustmentOrigin.PUBLIC_HOLIDAY,
            candidate,
        )
        changes.append(change)
        updated = updated.with_change(change)

    audit_change = AuditChange(
        SERVICE_ADJUSTMENT_SCHEDULE_KIND,
        SERVICE_ADJUSTMENT_SCHEDULE_ID,
        (
            ChangeOperation.INSERT
            if stored_state is None
            else ChangeOperation.UPDATE
        ),
        before=stored_state,
        after=service_adjustment_schedule_state(updated),
    )
    metadata = RevisionMetadata(
        revision_id=revision_id,
        parent_revision_id=current_revision_id,
        recorded_at=recorded_at,
        effective_on=changes[0].period.starts_on,
        actor=actor,
        reason=reason,
        source=RevisionSource.UI,
        decisions={
            "public_holiday_reviews": tuple(
                _service_adjustment_decision(change) for change in changes
            )
        },
        rule_versions={
            "service_adjustment_policy_version": (
                SERVICE_ADJUSTMENT_POLICY_VERSION
            )
        },
    )
    return PreparedPublicHolidayReviewRevision(
        updated,
        tuple(changes),
        RevisionRecord(metadata, (audit_change,)),
    )


def service_adjustment_schedule_from_snapshot(
    snapshot: HistoricalSnapshot,
) -> ServiceAdjustmentSchedule:
    if not isinstance(snapshot, HistoricalSnapshot):
        raise TypeError("snapshot은 HistoricalSnapshot이어야 합니다.")
    return service_adjustment_schedule_from_state(
        snapshot.entity(
            SERVICE_ADJUSTMENT_SCHEDULE_KIND,
            SERVICE_ADJUSTMENT_SCHEDULE_ID,
        )
    )


def service_adjustment_schedule_state(
    schedule: ServiceAdjustmentSchedule,
) -> Mapping[str, JsonValue]:
    if not isinstance(schedule, ServiceAdjustmentSchedule):
        raise TypeError("schedule은 ServiceAdjustmentSchedule이어야 합니다.")
    return {
        "policy_version": SERVICE_ADJUSTMENT_POLICY_VERSION,
        "changes": tuple(
            {
                "change_id": str(change.change_id),
                "period_start": change.period.starts_on.isoformat(),
                "period_end": change.period.ends_on.isoformat(),
                "meals": tuple(meal.value for meal in change.meals),
                "treatment": change.treatment.value,
                "default_delivery_on": (
                    None
                    if change.default_delivery_on is None
                    else change.default_delivery_on.isoformat()
                ),
                "delivery_overrides": tuple(
                    {
                        "meal": item.meal.value,
                        "service_on": item.service_on.isoformat(),
                        "delivery_on": item.delivery_on.isoformat(),
                    }
                    for item in change.delivery_overrides
                ),
                "note": change.note,
                "recorded_at": change.recorded_at.isoformat(),
                "origin": change.origin.value,
                "public_holiday": (
                    None
                    if change.public_holiday is None
                    else {
                        "day": change.public_holiday.day.isoformat(),
                        "name": change.public_holiday.name,
                        "provider_id": change.public_holiday.provider_id,
                        "provider_version": (
                            change.public_holiday.provider_version
                        ),
                    }
                ),
            }
            for change in schedule.changes
        ),
    }


def service_adjustment_schedule_from_state(
    state: Mapping[str, JsonValue] | None,
) -> ServiceAdjustmentSchedule:
    if state is None:
        return ServiceAdjustmentSchedule()
    if not isinstance(state, Mapping):
        raise TypeError("휴무·대체식 schedule state는 mapping이어야 합니다.")
    if state.get("policy_version") != SERVICE_ADJUSTMENT_POLICY_VERSION:
        raise ValueError("지원하지 않는 휴무·대체식 정책 version입니다.")
    raw_changes = state.get("changes")
    if not isinstance(raw_changes, Sequence) or isinstance(
        raw_changes,
        (str, bytes, bytearray),
    ):
        raise TypeError("휴무·대체식 변경 이력은 배열이어야 합니다.")
    changes: list[ServiceAdjustmentChange] = []
    for raw_change in raw_changes:
        if not isinstance(raw_change, Mapping):
            raise TypeError("휴무·대체식 변경 항목은 object여야 합니다.")
        raw_meals = raw_change.get("meals")
        if not isinstance(raw_meals, Sequence) or isinstance(
            raw_meals,
            (str, bytes, bytearray),
        ):
            raise TypeError("휴무·대체식 변경 식종은 배열이어야 합니다.")
        raw_overrides = raw_change.get("delivery_overrides")
        if not isinstance(raw_overrides, Sequence) or isinstance(
            raw_overrides,
            (str, bytes, bytearray),
        ):
            raise TypeError("대체식 개별 제공일은 배열이어야 합니다.")
        overrides: list[ReplacementDeliveryOverride] = []
        for raw_override in raw_overrides:
            if not isinstance(raw_override, Mapping):
                raise TypeError("대체식 개별 제공일 항목은 object여야 합니다.")
            overrides.append(
                ReplacementDeliveryOverride(
                    Meal(_required_text(raw_override.get("meal"), "식종")),
                    date.fromisoformat(
                        _required_text(
                            raw_override.get("service_on"),
                            "원래 식사일",
                        )
                    ),
                    date.fromisoformat(
                        _required_text(
                            raw_override.get("delivery_on"),
                            "대체식 제공일",
                        )
                    ),
                )
            )
        raw_default = raw_change.get("default_delivery_on")
        default_delivery_on = (
            None
            if raw_default is None
            else date.fromisoformat(
                _required_text(raw_default, "기본 대체식 제공일")
            )
        )
        origin = ServiceAdjustmentOrigin(
            _required_text(
                raw_change.get("origin", ServiceAdjustmentOrigin.MANUAL.value),
                "휴무·대체식 입력 경로",
            )
        )
        raw_public_holiday = raw_change.get("public_holiday")
        if raw_public_holiday is None:
            public_holiday = None
        else:
            if not isinstance(raw_public_holiday, Mapping):
                raise TypeError("공휴일 원본 정보는 object여야 합니다.")
            public_holiday = PublicHolidayCandidate(
                date.fromisoformat(
                    _required_text(raw_public_holiday.get("day"), "공휴일 날짜")
                ),
                _required_text(raw_public_holiday.get("name"), "공휴일 이름"),
                _required_text(
                    raw_public_holiday.get("provider_id"),
                    "공휴일 제공자",
                ),
                _required_text(
                    raw_public_holiday.get("provider_version"),
                    "공휴일 제공자 version",
                ),
            )
        changes.append(
            ServiceAdjustmentChange(
                UUID(
                    _required_text(
                        raw_change.get("change_id"),
                        "휴무·대체식 변경 ID",
                    )
                ),
                InclusiveDateRange(
                    date.fromisoformat(
                        _required_text(
                            raw_change.get("period_start"),
                            "휴무 시작일",
                        )
                    ),
                    date.fromisoformat(
                        _required_text(
                            raw_change.get("period_end"),
                            "휴무 종료일",
                        )
                    ),
                ),
                tuple(
                    Meal(_required_text(value, "식종"))
                    for value in raw_meals
                ),
                ServiceTreatment(
                    _required_text(raw_change.get("treatment"), "식사 처리")
                ),
                default_delivery_on,
                tuple(overrides),
                _required_text(raw_change.get("note"), "휴무 사유"),
                datetime.fromisoformat(
                    _required_text(
                        raw_change.get("recorded_at"),
                        "휴무·대체식 기록 시각",
                    )
                ),
                origin,
                public_holiday,
            )
        )
    return ServiceAdjustmentSchedule(tuple(changes))


def _canonical_meals(meals: Sequence[Meal]) -> tuple[Meal, ...]:
    values = tuple(meals)
    if not values:
        raise ValueError("하나 이상의 식종을 선택해야 합니다.")
    if any(not isinstance(meal, Meal) for meal in values):
        raise TypeError("meals에는 Meal만 허용됩니다.")
    if len(set(values)) != len(values):
        raise ValueError("선택한 식종은 중복될 수 없습니다.")
    return tuple(meal for meal in Meal if meal in values)


def _service_adjustment_decision(
    change: ServiceAdjustmentChange,
) -> Mapping[str, JsonValue]:
    assert change.period.starts_on is not None
    assert change.period.ends_on is not None
    return {
        "change_id": str(change.change_id),
        "period_start": change.period.starts_on.isoformat(),
        "period_end": change.period.ends_on.isoformat(),
        "meals": tuple(meal.value for meal in change.meals),
        "treatment": change.treatment.value,
        "default_delivery_on": (
            None
            if change.default_delivery_on is None
            else change.default_delivery_on.isoformat()
        ),
        "delivery_overrides": tuple(
            {
                "meal": item.meal.value,
                "service_on": item.service_on.isoformat(),
                "delivery_on": item.delivery_on.isoformat(),
            }
            for item in change.delivery_overrides
        ),
        "note": change.note,
        "origin": change.origin.value,
        "public_holiday": (
            None
            if change.public_holiday is None
            else {
                "candidate_key": change.public_holiday.candidate_key,
                "day": change.public_holiday.day.isoformat(),
                "name": change.public_holiday.name,
                "provider_id": change.public_holiday.provider_id,
                "provider_version": change.public_holiday.provider_version,
            }
        ),
    }


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label}은 문자열이어야 합니다.")
    normalized = " ".join(value.split())
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
