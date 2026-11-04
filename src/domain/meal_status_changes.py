"""Build one multi-meal delivery-status change as an immutable UI revision."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from uuid import UUID

from .delivery_dates import validate_actual_delivery_date
from .intake import IntakeChannel
from .projections import roster_audit_changes, status_event_audit_change
from .revisions import RevisionActor, RevisionMetadata, RevisionRecord, RevisionSource
from .roster import InclusiveDateRange, Meal, MealEnrollment, RosterSnapshot, canonical_meals
from .status import MealStatus, StatusEvent, StatusHistory

MEAL_STATUS_CHANGE_POLICY_VERSION = 2


def validate_meal_status_transition(
    roster: RosterSnapshot,
    histories: Mapping[tuple[UUID, Meal], StatusHistory],
    targets: tuple[tuple[UUID, Meal], ...],
    status: MealStatus,
    periods: Mapping[Meal, InclusiveDateRange],
) -> None:
    """Reject repeated delivery/stops at each target's chosen effective start."""
    if status not in (MealStatus.DELIVERY, MealStatus.STOPPED):
        return
    duplicates = []
    for person_id, meal in targets:
        starts_on = periods[meal].starts_on
        if starts_on is None or meal not in roster.meals_for(person_id, on=starts_on):
            continue
        history = histories.get((person_id, meal))
        current = history.status_on(starts_on) if history is not None else MealStatus.DELIVERY
        if current is status:
            duplicates.append(f"{roster.person(person_id).profile.name} · {meal.value} ({starts_on:%Y-%m-%d})")
    if duplicates:
        raise ValueError("\n".join(duplicates) + f"\n시작일에 이미 {status.value} 상태이므로 같은 상태를 다시 저장할 수 없습니다.")


@dataclass(frozen=True, slots=True)
class PreparedMealStatusRevision:
    snapshot: RosterSnapshot
    events: tuple[StatusEvent, ...]
    record: RevisionRecord
    auto_enrolled_meals: tuple[Meal, ...]


def build_meal_status_revision(
    current_roster: RosterSnapshot,
    person_id: UUID,
    meals: tuple[Meal, ...],
    status: MealStatus,
    period: InclusiveDateRange,
    *,
    actor: RevisionActor,
    recorded_at: datetime,
    revision_id: UUID,
    event_ids: Mapping[Meal, UUID],
    periods_by_meal: Mapping[Meal, InclusiveDateRange] | None = None,
    reason: str = "배송 상태 변경",
    intake_channel: IntakeChannel | None = None,
    note: str = "",
    school_calendar=None,
) -> PreparedMealStatusRevision:
    """Apply one status/period to many meals, auto-enrolling delivery gaps."""

    if not isinstance(current_roster, RosterSnapshot):
        raise TypeError("current roster는 RosterSnapshot이어야 합니다.")
    if not isinstance(person_id, UUID):
        raise TypeError("person ID는 UUID여야 합니다.")
    current_roster.person(person_id)
    selected_meals = canonical_meals(meals)
    if not isinstance(status, MealStatus):
        raise TypeError("status는 MealStatus여야 합니다.")
    if intake_channel is not None and not isinstance(intake_channel, IntakeChannel):
        raise TypeError("접수 경로는 IntakeChannel이어야 합니다.")
    if not isinstance(note, str):
        raise TypeError("비고는 문자열이어야 합니다.")
    periods = _resolve_periods(
        current_roster, selected_meals, status, period, periods_by_meal
    )
    if not isinstance(actor, RevisionActor):
        raise TypeError("actor는 RevisionActor여야 합니다.")
    if not isinstance(recorded_at, datetime):
        raise TypeError("recorded_at은 datetime이어야 합니다.")
    if recorded_at.tzinfo is None or recorded_at.utcoffset() is None:
        raise ValueError("recorded_at에는 시간대가 있어야 합니다.")
    if not isinstance(revision_id, UUID):
        raise TypeError("revision ID는 UUID여야 합니다.")
    if revision_id == current_roster.revision_id:
        raise ValueError("새 revision ID는 현재 revision과 달라야 합니다.")

    ids_by_meal = dict(event_ids)
    if set(ids_by_meal) != set(selected_meals):
        raise ValueError("선택한 식종마다 정확히 하나의 상태 사건 ID가 필요합니다.")
    if any(not isinstance(event_id, UUID) for event_id in ids_by_meal.values()):
        raise TypeError("상태 사건 ID는 UUID여야 합니다.")
    if len(set(ids_by_meal.values())) != len(ids_by_meal):
        raise ValueError("상태 사건 ID는 중복될 수 없습니다.")

    not_enrolled = tuple(
        meal
        for meal in selected_meals
        if meal not in current_roster.meals_for(person_id, on=periods[meal].starts_on)
    )
    if status is not MealStatus.DELIVERY and not_enrolled:
        labels = " · ".join(meal.value for meal in not_enrolled)
        raise ValueError(
            f"시작일에 미가입인 식종에는 {status.value}을 적용할 수 없습니다: {labels}"
        )

    enrollments = current_roster.enrollments
    auto_enrolled: list[Meal] = []
    if status is MealStatus.DELIVERY:
        for meal in selected_meals:
            if meal is Meal.LUNCH and meal not in not_enrolled and current_roster.person(person_id).lunch_period is not None:
                continue
            updated = _cover_delivery_period(
                enrollments,
                person_id,
                meal,
                periods[meal],
            )
            if updated != enrollments:
                auto_enrolled.append(meal)
                enrollments = updated

    snapshot = RosterSnapshot(
        revision_id,
        persons=current_roster.persons,
        households=current_roster.households,
        memberships=current_roster.memberships,
        enrollments=enrollments,
        assignments=current_roster.assignments,
        custom_fields=current_roster.custom_fields,
        delivery_defaults=current_roster.delivery_defaults,
    )
    if status is MealStatus.DELIVERY and Meal.LUNCH in not_enrolled:
        from .lunch_periods import set_lunch_periods, suggested_person_lunch_period
        requested = periods[Meal.LUNCH]
        lunch_period = suggested_person_lunch_period(current_roster.person(person_id).profile,
            school_calendar, requested.starts_on, exact_start=True, ends_on=requested.ends_on)
        snapshot = set_lunch_periods(snapshot, {person_id: lunch_period},
                                    replace_from={person_id: requested.starts_on})
    events = tuple(
        StatusEvent(
            event_id=ids_by_meal[meal],
            revision_id=revision_id,
            person_id=person_id,
            meal=meal,
            status=status,
            period=periods[meal],
            recorded_at=recorded_at,
            source_text=f"{meal.value} {status.value} · {_period_text(periods[meal])}",
        )
        for meal in selected_meals
    )
    changes = (
        *roster_audit_changes(current_roster, snapshot),
        *(status_event_audit_change(event) for event in events),
    )
    decisions = {
        "meal_status_change": {
            "person_id": str(person_id),
            "meals": [meal.value for meal in selected_meals],
            "status": status.value,
            "starts_on": _common_value(periods, "starts_on"),
            "ends_on": _common_value(periods, "ends_on"),
            "periods": {
                meal.value: {
                    "starts_on": periods[meal].starts_on.isoformat(),
                    "ends_on": (
                        periods[meal].ends_on.isoformat()
                        if periods[meal].ends_on is not None
                        else None
                    ),
                }
                for meal in selected_meals
            },
            "auto_enrolled_meals": [meal.value for meal in auto_enrolled],
        }
    }
    if intake_channel is not None:
        decisions["meal_status_change"]["intake_channel"] = intake_channel.value
    if note.strip():
        decisions["meal_status_change"]["note"] = note.strip()
    metadata = RevisionMetadata(
        revision_id=revision_id,
        parent_revision_id=current_roster.revision_id,
        recorded_at=recorded_at,
        effective_on=min(item.starts_on for item in periods.values()),
        actor=actor,
        reason=reason,
        source=RevisionSource.UI,
        decisions=decisions,
        rule_versions={
            "meal_status_change_policy_version": MEAL_STATUS_CHANGE_POLICY_VERSION,
            "meal_status_sequence_policy_version": 1,
        },
    )
    return PreparedMealStatusRevision(
        snapshot,
        events,
        RevisionRecord(metadata, changes),
        tuple(auto_enrolled),
    )


@dataclass(frozen=True, slots=True)
class PreparedBatchMealStatusRevision:
    snapshot: RosterSnapshot
    events: tuple[StatusEvent, ...]
    record: RevisionRecord
    auto_enrolled_targets: tuple[tuple[UUID, Meal], ...]


def build_batch_meal_status_revision(
    current_roster: RosterSnapshot,
    targets: tuple[tuple[UUID, Meal], ...],
    status: MealStatus,
    period: InclusiveDateRange,
    *,
    actor: RevisionActor,
    recorded_at: datetime,
    revision_id: UUID,
    event_ids: Mapping[tuple[UUID, Meal], UUID],
    periods_by_meal: Mapping[Meal, InclusiveDateRange] | None = None,
    intake_channel: IntakeChannel | None = None,
    note: str = "",
    school_calendar=None,
) -> PreparedBatchMealStatusRevision:
    """Validate every target and prepare one atomic revision for the whole batch."""
    if not isinstance(current_roster, RosterSnapshot):
        raise TypeError("current roster는 RosterSnapshot이어야 합니다.")
    if not targets:
        raise ValueError("적용할 사람과 식종을 한 개 이상 선택해주세요.")
    if len(set(targets)) != len(targets):
        raise ValueError("같은 사람의 식종이 중복 선택되었습니다.")
    if set(event_ids) != set(targets):
        raise ValueError("적용할 항목마다 변경 기록 번호가 필요합니다.")
    if len(set(event_ids.values())) != len(event_ids):
        raise ValueError("변경 기록 번호는 중복될 수 없습니다.")
    if any(not isinstance(event_id, UUID) for event_id in event_ids.values()):
        raise TypeError("상태 사건 ID는 UUID여야 합니다.")
    if not isinstance(status, MealStatus):
        raise TypeError("status는 MealStatus여야 합니다.")
    if intake_channel is not None and not isinstance(intake_channel, IntakeChannel):
        raise TypeError("접수 경로는 IntakeChannel이어야 합니다.")
    if not isinstance(note, str):
        raise TypeError("비고는 문자열이어야 합니다.")
    if not isinstance(actor, RevisionActor):
        raise TypeError("actor는 RevisionActor여야 합니다.")
    if not isinstance(recorded_at, datetime):
        raise TypeError("recorded_at은 datetime이어야 합니다.")
    if recorded_at.tzinfo is None or recorded_at.utcoffset() is None:
        raise ValueError("recorded_at에는 시간대가 있어야 합니다.")
    if not isinstance(revision_id, UUID):
        raise TypeError("revision ID는 UUID여야 합니다.")
    if revision_id == current_roster.revision_id:
        raise ValueError("새 revision ID는 현재 revision과 달라야 합니다.")
    grouped: dict[UUID, list[Meal]] = {}
    for person_id, meal in targets:
        current_roster.person(person_id)
        if not isinstance(meal, Meal):
            raise TypeError("적용할 식종을 확인해주세요.")
        grouped.setdefault(person_id, []).append(meal)
    all_selected_meals = canonical_meals(
        tuple(dict.fromkeys(meal for _person_id, meal in targets))
    )
    periods = _resolve_periods(
        current_roster, all_selected_meals, status, period, periods_by_meal
    )
    enrollments = current_roster.enrollments
    events: list[StatusEvent] = []
    enrolled: list[tuple[UUID, Meal]] = []
    for person_id, meals in grouped.items():
        selected_meals = canonical_meals(meals)
        not_enrolled = tuple(
            meal
            for meal in selected_meals
            if meal not in current_roster.meals_for(person_id, on=periods[meal].starts_on)
        )
        if status is not MealStatus.DELIVERY and not_enrolled:
            labels = " · ".join(meal.value for meal in not_enrolled)
            raise ValueError(
                f"시작일에 미가입인 식종에는 {status.value}을 적용할 수 없습니다: {labels}"
            )
        if status is MealStatus.DELIVERY:
            for meal in selected_meals:
                if meal is Meal.LUNCH and meal not in not_enrolled and current_roster.person(person_id).lunch_period is not None:
                    continue
                updated = _cover_delivery_period(
                    enrollments, person_id, meal, periods[meal]
                )
                if updated != enrollments:
                    enrolled.append((person_id, meal))
                    enrollments = updated
        events.extend(
            StatusEvent(
                event_id=event_ids[(person_id, meal)],
                revision_id=revision_id,
                person_id=person_id,
                meal=meal,
                status=status,
                period=periods[meal],
                recorded_at=recorded_at,
                source_text=f"{meal.value} {status.value} · {_period_text(periods[meal])}",
            )
            for meal in selected_meals
        )
    working = RosterSnapshot(
        revision_id,
        persons=current_roster.persons,
        households=current_roster.households,
        memberships=current_roster.memberships,
        enrollments=enrollments,
        assignments=current_roster.assignments,
        custom_fields=current_roster.custom_fields,
        delivery_defaults=current_roster.delivery_defaults,
    )
    if status is MealStatus.DELIVERY and Meal.LUNCH in all_selected_meals:
        from .lunch_periods import set_lunch_periods, suggested_person_lunch_period
        requested = periods[Meal.LUNCH]
        updates = {pid: suggested_person_lunch_period(current_roster.person(pid).profile,
            school_calendar, requested.starts_on, exact_start=True, ends_on=requested.ends_on)
            for pid, selected in grouped.items() if Meal.LUNCH in selected
            and Meal.LUNCH not in current_roster.meals_for(pid, on=requested.starts_on)}
        working = set_lunch_periods(working, updates,
                                   replace_from={pid: requested.starts_on for pid in updates})
    changes = (
        *roster_audit_changes(current_roster, working),
        *(status_event_audit_change(event) for event in events),
    )
    decisions = {
        "batch_meal_status_change": {
            "targets": [
                {"person_id": str(pid), "meal": meal.value} for pid, meal in targets
            ],
            "status": status.value,
            "starts_on": _common_value(periods, "starts_on"),
            "ends_on": _common_value(periods, "ends_on"),
            "periods": {
                meal.value: {
                    "starts_on": periods[meal].starts_on.isoformat(),
                    "ends_on": (
                        periods[meal].ends_on.isoformat()
                        if periods[meal].ends_on is not None
                        else None
                    ),
                }
                for meal in all_selected_meals
            },
            "auto_enrolled_targets": [
                {"person_id": str(pid), "meal": meal.value}
                for pid, meal in enrolled
            ],
        }
    }
    if intake_channel is not None:
        decisions["batch_meal_status_change"]["intake_channel"] = intake_channel.value
    if note.strip():
        decisions["batch_meal_status_change"]["note"] = note.strip()
    metadata = RevisionMetadata(
        revision_id=revision_id,
        parent_revision_id=current_roster.revision_id,
        recorded_at=recorded_at,
        effective_on=min(item.starts_on for item in periods.values()),
        actor=actor,
        reason=f"배송 상태 일괄 변경 · {len(grouped)}명 {len(targets)}개",
        source=RevisionSource.UI,
        decisions=decisions,
        rule_versions={
            "meal_status_change_policy_version": MEAL_STATUS_CHANGE_POLICY_VERSION,
            "meal_status_sequence_policy_version": 1,
        },
    )
    return PreparedBatchMealStatusRevision(
        working, tuple(events), RevisionRecord(metadata, changes), tuple(enrolled)
    )


def _resolve_periods(
    roster: RosterSnapshot,
    meals: tuple[Meal, ...],
    status: MealStatus,
    common_period: InclusiveDateRange,
    periods_by_meal: Mapping[Meal, InclusiveDateRange] | None,
) -> dict[Meal, InclusiveDateRange]:
    if periods_by_meal is None:
        if not isinstance(common_period, InclusiveDateRange):
            raise TypeError("period는 InclusiveDateRange여야 합니다.")
        periods = {meal: common_period for meal in meals}
    else:
        periods = dict(periods_by_meal)
        if set(periods) != set(meals):
            raise ValueError("선택한 식종마다 적용 기간이 필요합니다.")
        if any(not isinstance(item, InclusiveDateRange) for item in periods.values()):
            raise TypeError("식종별 기간은 InclusiveDateRange여야 합니다.")
    for meal, selected_period in periods.items():
        if selected_period.starts_on is None:
            raise ValueError(f"{meal.value} 배송 상태 변경에는 시작일이 필요합니다.")
        if status is MealStatus.PAUSED and selected_period.ends_on is None:
            raise ValueError(f"{meal.value} 일시중지는 종료일이 필요합니다.")
        validate_actual_delivery_date(
            roster.delivery_defaults,
            meal,
            selected_period.starts_on,
            field_name="시작일",
        )
        if selected_period.ends_on is not None:
            validate_actual_delivery_date(
                roster.delivery_defaults,
                meal,
                selected_period.ends_on,
                field_name="종료일",
            )
    return periods


def _common_value(
    periods: Mapping[Meal, InclusiveDateRange], field: str
) -> str | None:
    values = {getattr(period, field) for period in periods.values()}
    if len(values) != 1:
        return None
    value = next(iter(values))
    return value.isoformat() if value is not None else None


def _cover_delivery_period(
    enrollments: tuple[MealEnrollment, ...],
    person_id: UUID,
    meal: Meal,
    requested: InclusiveDateRange,
) -> tuple[MealEnrollment, ...]:
    matching_periods = [
        enrollment.period
        for enrollment in enrollments
        if enrollment.person_id == person_id and enrollment.meal is meal
    ]
    if any(_period_covers(period, requested) for period in matching_periods):
        return enrollments

    merged_periods = _merge_periods((*matching_periods, requested))
    untouched = [
        enrollment
        for enrollment in enrollments
        if enrollment.person_id != person_id or enrollment.meal is not meal
    ]
    combined = [
        *untouched,
        *(MealEnrollment(person_id, meal, period) for period in merged_periods),
    ]
    return tuple(
        sorted(
            combined,
            key=lambda item: (
                str(item.person_id),
                item.meal.value,
                item.period.starts_on or date.min,
                item.period.ends_on or date.max,
            ),
        )
    )


def _merge_periods(
    periods: tuple[InclusiveDateRange, ...],
) -> tuple[InclusiveDateRange, ...]:
    ordered = sorted(periods, key=lambda item: item.starts_on or date.min)
    merged: list[InclusiveDateRange] = []
    for period in ordered:
        if not merged or not _periods_touch(merged[-1], period):
            merged.append(period)
            continue
        previous = merged[-1]
        merged[-1] = InclusiveDateRange(
            previous.starts_on,
            _later_end(previous.ends_on, period.ends_on),
        )
    return tuple(merged)


def _periods_touch(
    first: InclusiveDateRange,
    second: InclusiveDateRange,
) -> bool:
    if first.ends_on is None or second.starts_on is None:
        return True
    if first.ends_on == date.max:
        return True
    return second.starts_on <= first.ends_on + timedelta(days=1)


def _later_end(first: date | None, second: date | None) -> date | None:
    if first is None or second is None:
        return None
    return max(first, second)


def _period_covers(
    existing: InclusiveDateRange,
    requested: InclusiveDateRange,
) -> bool:
    starts_early_enough = existing.starts_on is None or (
        requested.starts_on is not None and existing.starts_on <= requested.starts_on
    )
    ends_late_enough = existing.ends_on is None or (
        requested.ends_on is not None and existing.ends_on >= requested.ends_on
    )
    return starts_early_enough and ends_late_enough


def _period_text(period: InclusiveDateRange) -> str:
    starts = period.starts_on.isoformat() if period.starts_on else "이전부터"
    ends = period.ends_on.isoformat() if period.ends_on else "계속"
    return f"{starts} ~ {ends}"
