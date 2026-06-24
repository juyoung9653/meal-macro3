"""Tests for src.core.meal_types — MealType enum, MEAL_KEYS, ALL_MEALS."""

from src.core.meal_types import MealType


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
