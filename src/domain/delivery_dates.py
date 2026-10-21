"""Configured actual delivery weekdays used by manual date entry."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime, timedelta

from .roster import HouseholdDeliverySetting, Meal


_WEEKDAY_LABELS = ("월", "화", "수", "목", "금", "토", "일")


def actual_delivery_weekdays(
    defaults: Iterable[HouseholdDeliverySetting], meal: Meal
) -> tuple[int, ...]:
    """Return delivery weekdays reached by configured service weekdays."""
    setting = next((item for item in defaults if item.meal is meal), None)
    if setting is None or setting.weekdays is None or setting.delivery_weekdays is None:
        raise ValueError(f"{meal.value} 배송 기본값에 요일 설정이 없습니다.")
    return tuple(
        sorted({setting.delivery_weekdays[service_day] for service_day in setting.weekdays})
    )


def next_actual_delivery_date(
    defaults: Iterable[HouseholdDeliverySetting], meal: Meal, on_or_after: date
) -> date:
    weekdays = actual_delivery_weekdays(defaults, meal)
    if not weekdays:
        raise ValueError(f"{meal.value}에 설정된 실제 배송 요일이 없습니다.")
    return min(
        on_or_after + timedelta(days=(weekday - on_or_after.weekday()) % 7)
        for weekday in weekdays
    )


def validate_actual_delivery_date(
    defaults: Iterable[HouseholdDeliverySetting],
    meal: Meal,
    value: date,
    *,
    field_name: str,
) -> None:
    if isinstance(value, datetime) or not isinstance(value, date):
        raise TypeError(f"{field_name}은 날짜여야 합니다.")
    weekdays = actual_delivery_weekdays(defaults, meal)
    if value.weekday() not in weekdays:
        raise ValueError(
            f"{meal.value} {field_name}은 실제 배송 요일({weekday_text(weekdays)})로 "
            "선택해주세요."
        )


def weekday_text(weekdays: Iterable[int]) -> str:
    values = tuple(weekdays)
    return "·".join(f"{_WEEKDAY_LABELS[weekday]}요일" for weekday in values) or "없음"
