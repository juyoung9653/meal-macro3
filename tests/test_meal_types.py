"""Tests for src.core.meal_types — MealType enum, MEAL_KEYS, ALL_MEALS."""

from src.core.meal_types import ALL_MEALS, MEAL_KEYS, MealType


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


class TestMealKeys:
    def test_order(self):
        assert MEAL_KEYS == ["breakfast", "dinner", "lunch"]

    def test_all_meals_content(self):
        assert ALL_MEALS == {"breakfast", "dinner", "lunch"}

    def test_consistency_with_enum(self):
        """MEAL_KEYS and ALL_MEALS should match MealType enum values."""
        assert set(MEAL_KEYS) == set(m.value for m in MealType)
        assert ALL_MEALS == set(m.value for m in MealType)
