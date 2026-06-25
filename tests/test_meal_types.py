"""Tests for src.core.meal_types — MealType enum, MEAL_RULES, MEAL_LABELS, ERRORS."""

from src.core.meal_types import ERRORS, MEAL_LABELS, MEAL_RULES, MealType


class TestMealType:
    def test_enum_values(self):
        assert MealType.BREAKFAST == "breakfast"
        assert MealType.DINNER == "dinner"
        assert MealType.LUNCH == "lunch"

    def test_enum_membership(self):
        assert MealType.BREAKFAST in MealType
        assert MealType.DINNER in MealType
        assert MealType.LUNCH in MealType

    def test_str_interface(self):
        assert str(MealType.BREAKFAST) == "breakfast"
        assert str(MealType.DINNER) == "dinner"
        assert str(MealType.LUNCH) == "lunch"

    def test_is_str_subclass(self):
        assert isinstance(MealType.BREAKFAST, str)
        assert isinstance(MealType.DINNER, str)
        assert isinstance(MealType.LUNCH, str)

    def test_from_string(self):
        assert MealType("breakfast") == MealType.BREAKFAST
        assert MealType("dinner") == MealType.DINNER
        assert MealType("lunch") == MealType.LUNCH


class TestFromName:
    def test_breakfast(self):
        assert MealType.from_name("조식") == MealType.BREAKFAST
        assert MealType.from_name("조식(금요일)") == MealType.BREAKFAST

    def test_lunch(self):
        assert MealType.from_name("중식") == MealType.LUNCH
        assert MealType.from_name("중식(월,화,수,목,금)") == MealType.LUNCH
        assert MealType.from_name("방중") == MealType.LUNCH

    def test_dinner(self):
        assert MealType.from_name("석식") == MealType.DINNER
        assert MealType.from_name("석식(월,화,수,목)") == MealType.DINNER

    def test_unknown(self):
        assert MealType.from_name("간식") is None
        assert MealType.from_name("") is None
        assert MealType.from_name("아침") is None


class TestLabel:
    def test_breakfast_label(self):
        assert MealType.BREAKFAST.label() == "조식(금요일)"

    def test_dinner_label(self):
        assert MealType.DINNER.label() == "석식(월,화,수,목)"

    def test_lunch_label(self):
        assert MealType.LUNCH.label() == "중식(월,화,수,목,금)"


class TestWeekdays:
    def test_breakfast_weekdays(self):
        assert MealType.BREAKFAST.weekdays() == {4}  # Friday

    def test_dinner_weekdays(self):
        assert MealType.DINNER.weekdays() == {0, 1, 2, 3}  # Mon-Thu

    def test_lunch_weekdays(self):
        assert MealType.LUNCH.weekdays() == {0, 1, 2, 3, 4}  # Mon-Fri


class TestModuleConstants:
    def test_meal_rules(self):
        assert MEAL_RULES["조식"] == {4}
        assert MEAL_RULES["중식"] == {0, 1, 2, 3, 4}
        assert MEAL_RULES["석식"] == {0, 1, 2, 3}

    def test_meal_labels(self):
        assert MEAL_LABELS["조식"] == "조식(금요일)"
        assert MEAL_LABELS["중식"] == "중식(월,화,수,목,금)"
        assert MEAL_LABELS["석식"] == "석식(월,화,수,목)"

    def test_errors(self):
        assert "배송안함" in ERRORS
        assert "식종불일치" in ERRORS
        assert "날짜형식" in ERRORS
        assert "배송일아님" in ERRORS
        assert "날짜없음" in ERRORS
        assert "유효하지 않은 식종" in ERRORS["식종불일치"]
