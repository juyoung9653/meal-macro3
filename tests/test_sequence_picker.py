"""Focused contracts for the meal-by-meal sequence picker."""

import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QStandardItemModel
from PySide6.QtWidgets import QApplication, QTableView

from src.desktop_v2.column_search import (
    ALL_SEQUENCES_EMPTY,
    ColumnSearchHeader,
    SequenceValuePicker,
    sequence_exclusive_token,
    sequence_pair_token,
    sequence_present_token,
)
from src.domain.roster import Meal


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def values():
    return (
        sequence_pair_token(Meal.DINNER.value, "001"),
        sequence_pair_token(Meal.DINNER.value, "1"),
        sequence_pair_token(Meal.BREAKFAST.value, "3"),
        sequence_pair_token(Meal.LUNCH.value, "12"),
    )


def emitted_by(picker):
    emitted = []
    picker.applied.connect(lambda *args: emitted.append(args))
    return emitted


def parent(picker, meal):
    return next(
        picker.list.topLevelItem(index)
        for index in range(picker.list.topLevelItemCount())
        if picker.list.topLevelItem(index).text(0) == meal.value
    )


def leaf(picker, token):
    return next(
        item for item in picker._leaves
        if item.data(0, Qt.ItemDataRole.UserRole) == token
    )


def test_tree_uses_meal_groups_and_parent_selects_only_nonempty_values(app):
    picker = SequenceValuePicker(values(), frozenset())
    dinner = parent(picker, Meal.DINNER)
    assert [picker.list.topLevelItem(index).text(0) for index in range(3)] == [
        Meal.DINNER.value, Meal.BREAKFAST.value, Meal.LUNCH.value,
    ]
    blank = leaf(picker, sequence_pair_token(Meal.DINNER.value, ""))
    assert blank.text(0) == "(빈 값)"
    assert blank.checkState(0) == Qt.CheckState.Unchecked

    dinner.setCheckState(0, Qt.CheckState.Checked)
    assert blank.checkState(0) == Qt.CheckState.Unchecked
    assert all(
        dinner.child(index).checkState(0) == Qt.CheckState.Checked
        for index in range(dinner.childCount())
        if dinner.child(index) is not blank
    )
    blank.setCheckState(0, Qt.CheckState.Checked)
    result = emitted_by(picker)
    picker.apply.click()
    assert result[-1][1] == frozenset({
        sequence_pair_token(Meal.DINNER.value, ""),
        sequence_pair_token(Meal.DINNER.value, "001"),
        sequence_pair_token(Meal.DINNER.value, "1"),
    })
    picker.close()


def test_none_empty_select_all_reset_and_cancel_keep_distinct_meanings(app):
    unrestricted = SequenceValuePicker(values(), None)
    result = emitted_by(unrestricted)
    unrestricted.search.setText("001")
    unrestricted.apply.click()
    assert result[-1] == ("001", None)

    unrestricted.select_all.click()
    unrestricted.apply.click()
    assert result[-1][1] is not None
    assert result[-1][1] == frozenset(
        item.data(0, Qt.ItemDataRole.UserRole) for item in unrestricted._leaves
    )

    empty = SequenceValuePicker(values(), frozenset())
    empty_result = emitted_by(empty)
    empty.cancel.click()
    assert empty_result == []
    empty.apply.click()
    assert empty_result[-1] == ("", frozenset())
    empty.reset.click()
    assert empty_result[-1] == ("", None)
    unrestricted.close()
    empty.close()


def test_saved_structured_selection_roundtrips_with_implied_blank_meals(app):
    lunch = sequence_pair_token(Meal.LUNCH.value, "12")
    picker = SequenceValuePicker(values(), frozenset((lunch,)))
    result = emitted_by(picker)

    assert leaf(picker, sequence_pair_token(Meal.DINNER.value, "")).checkState(0) == Qt.CheckState.Checked
    assert leaf(picker, sequence_pair_token(Meal.BREAKFAST.value, "")).checkState(0) == Qt.CheckState.Checked
    assert leaf(picker, lunch).checkState(0) == Qt.CheckState.Checked
    picker.apply.click()
    assert result[-1] == ("", frozenset((lunch,)))

    leaf(picker, sequence_pair_token(Meal.LUNCH.value, "")).setCheckState(0, Qt.CheckState.Checked)
    picker.apply.click()
    assert result[-1][1] == frozenset({
        lunch,
        sequence_pair_token(Meal.LUNCH.value, ""),
        sequence_pair_token(Meal.DINNER.value, ""),
        sequence_pair_token(Meal.BREAKFAST.value, ""),
    })
    picker.close()


def test_saved_semantic_and_aggregate_tokens_are_removable_compatibility_items(app):
    present = sequence_present_token(Meal.DINNER.value)
    exclusive = sequence_exclusive_token(Meal.BREAKFAST.value)
    selected = frozenset((present, exclusive, ALL_SEQUENCES_EMPTY, "예전 행 값"))
    picker = SequenceValuePicker(values(), selected)
    result = emitted_by(picker)

    compatibility = next(
        picker.list.topLevelItem(index)
        for index in range(picker.list.topLevelItemCount())
        if picker.list.topLevelItem(index).text(0) == "기존 저장 조건"
    )
    assert compatibility.childCount() == 4
    assert not any(
        item.data(0, Qt.ItemDataRole.UserRole) == sequence_present_token(Meal.LUNCH.value)
        for item in picker._leaves
    )
    leaf(picker, present).setCheckState(0, Qt.CheckState.Unchecked)
    picker.apply.click()
    assert present not in result[-1][1]
    assert selected - {present} <= result[-1][1]
    picker.close()


def test_real_header_menu_focuses_search_and_stays_on_screen(app):
    table = QTableView()
    table.setModel(QStandardItemModel(1, 1, table))
    header = ColumnSearchHeader(
        table,
        values_provider=lambda _key: values(),
        selection_provider=lambda _key: frozenset((sequence_pair_token(Meal.LUNCH.value, "12"),)),
        search_provider=lambda _key: "12",
        picker_provider=lambda _key: SequenceValuePicker,
    )
    table.setHorizontalHeader(header)
    header.replace_columns((SimpleNamespace(key="sequence", label="순서"),))
    table.resize(640, 480)
    table.show()
    app.processEvents()

    header.open_value_filter("sequence")
    picker = header._menu.findChild(SequenceValuePicker)
    app.processEvents()
    assert picker.search.hasFocus()
    assert picker.search.text() == "12"
    assert header._menu.screen().availableGeometry().contains(header._menu.frameGeometry())
    table.close()
