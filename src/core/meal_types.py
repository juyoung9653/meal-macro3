"""식사 종류 enum — 중앙 집중형 문자열 리터럴 관리."""

from enum import StrEnum

# Meal weekday rules: which weekdays each meal operates on
MEAL_RULES: dict[str, set[int]] = {
    "조식": {4},  # Friday only
    "중식": {0, 1, 2, 3, 4},  # Mon-Fri
    "석식": {0, 1, 2, 3},  # Mon-Thu
}

MEAL_LABELS: dict[str, str] = {
    "조식": "조식(금요일)",
    "중식": "중식(월,화,수,목,금)",
    "석식": "석식(월,화,수,목)",
}

ERRORS: dict[str, str] = {
    "배송안함": "날짜 매칭 오류: 해당 날짜는 배송을 하지 않았습니다",
    "식종불일치": "유효하지 않은 식종입니다. 허용되는 값: [석식, 중식, 조식]",
    "날짜형식": "날짜 형식이 유효하지 않습니다. (YYYYMMDD 형식 필요)",
    "배송일아님": "식종 불일치! {} ({})은 {} 배송일이 아닙니다.",
    "날짜없음": "{}에 해당하는 날짜를 db시트의 헤더에서 찾을 수 없습니다.",
}


class MealType(StrEnum):
    BREAKFAST = "breakfast"
    DINNER = "dinner"
    LUNCH = "lunch"

    @classmethod
    def from_name(cls, korean_name: str) -> "MealType | None":
        """Map Korean meal name to MealType. E.g. '조식' → MealType.BREAKFAST."""
        _MAP = {
            "조식": cls.BREAKFAST,
            "중식": cls.LUNCH,
            "방중": cls.LUNCH,
            "석식": cls.DINNER,
        }
        for key, val in _MAP.items():
            if key in korean_name:
                return val
        return None

    def label(self) -> str:
        """Display label for this meal type."""
        _LABEL_MAP = {
            MealType.BREAKFAST: "조식(금요일)",
            MealType.DINNER: "석식(월,화,수,목)",
            MealType.LUNCH: "중식(월,화,수,목,금)",
        }
        return _LABEL_MAP[self]

    def weekdays(self) -> set[int]:
        """Weekdays this meal operates on (0=Mon, 6=Sun)."""
        _WEEKDAY_MAP = {
            MealType.BREAKFAST: {4},
            MealType.DINNER: {0, 1, 2, 3},
            MealType.LUNCH: {0, 1, 2, 3, 4},
        }
        return _WEEKDAY_MAP[self]
