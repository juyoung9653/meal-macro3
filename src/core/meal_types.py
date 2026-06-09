"""식사 종류 enum — 중앙 집중형 문자열 리터럴 관리."""

from enum import StrEnum


class MealType(StrEnum):
    BREAKFAST = "breakfast"
    DINNER = "dinner"
    LUNCH = "lunch"


MEAL_KEYS: list[str] = [m.value for m in MealType]
ALL_MEALS: set[str] = {m.value for m in MealType}
