"""Sequence projection/filter contracts and manifest editing integration."""
import os
from dataclasses import replace
from datetime import date
from types import SimpleNamespace
from uuid import uuid4

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QContextMenuEvent, QInputMethodEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from src.desktop_v2.column_search import (
    SequenceValuePicker,
    sequence_exclusive_token,
    sequence_pair_token,
    sequence_present_meal,
    sequence_present_token,
)
from src.desktop_v2.roster_window import RosterTableModel, RosterWindow
from src.domain.roster import (
    Meal,
    MealEnrollment,
    Person,
    PersonProfile,
    RosterSnapshot,
    DeliveryAssignment,
)
from src.domain.revisions import RevisionActor
from src.domain.status import MealStatus
from src.services.driver_households import DriverHouseholdRow


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch, tmp_path):
    import src.desktop_v2.inactive_roster as inactive
    settings = QSettings(str(tmp_path / "archive.ini"), QSettings.IniFormat)
    monkeypatch.setattr(inactive, "QSettings", lambda *a, **k: settings)


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def roster_fixture():
    people = tuple(Person(uuid4(), PersonProfile(name)) for name in ("하나", "둘", "열", "혼합", "빈값", "공통", "보존"))
    dinner, breakfast = Meal.DINNER, Meal.BREAKFAST
    enrollments = tuple(MealEnrollment(people[i].person_id, meal)
                        for i, meals in enumerate(((dinner,), (breakfast,), (dinner,), (dinner, breakfast), (dinner,), (), ()))
                        for meal in meals)
    assignments = tuple(DeliveryAssignment(people[i].person_id, meal, "기사", value)
                        for i, meal, value in ((0, None, "001"), (1, breakfast, "2"), (2, dinner, "10"),
                                               (3, dinner, "001"), (3, breakfast, "2"), (5, None, "X"), (6, breakfast, "9")))
    return RosterSnapshot(uuid4(), persons=people, enrollments=enrollments, assignments=assignments)


def column(model, key):
    return next(i for i, c in enumerate(model.columns) if c.key == key)


def test_projection_single_common_retained_blank_raw_sort_and_exact_pairs():
    roster = roster_fixture()
    model = RosterTableModel(roster)
    model.set_context(date(2026, 10, 13), Meal.DINNER)
    values = {model.person_at(i).profile.name: model.data(model.index(i, column(model, "sequence")))
              for i in range(model.rowCount())}
    assert values == {"하나": "석식: 001", "둘": "조식: 2", "열": "석식: 10", "혼합": "석식: 001\n조식: 2",
                      "빈값": "", "공통": "공통/식종 미지정: X", "보존": "조식: 9"}
    model.sort(column(model, "sequence"), Qt.AscendingOrder)
    assert [model.person_at(i).profile.name for i in range(7)] == ["하나", "혼합", "둘", "보존", "열", "공통", "빈값"]
    token = sequence_pair_token
    model.set_column_values("sequence", {token("석식", "001"), token("조식", "2")})
    assert {model.person_at(i).profile.name for i in range(model.rowCount())} == {"혼합"}
    model.set_column_search("name", "혼합")
    assert model.rowCount() == 1
    model.set_column_search("name", "")
    model.set_column_values("sequence", {token("석식", "1")})
    assert model.rowCount() == 0
    model.set_column_values("sequence", {token("석식", "")})
    assert model.rowCount() == 1 and model.person_at(0).profile.name == "빈값"
    model.set_column_values("sequence", set())
    assert model.rowCount() == 1 and model.person_at(0).profile.name == "빈값"
    # Old composite selections retain exact whole-row semantics.
    model.set_column_values("sequence", {"석식: 001\n조식: 2"})
    assert model.rowCount() == 1 and model.person_at(0).profile.name == "혼합"
    model.set_column_values("sequence", {"001"})
    assert model.rowCount() == 1 and model.person_at(0).profile.name == "하나"


def test_tree_parent_children_ime_empty_and_unavailable_selection(app):
    token = sequence_pair_token
    values = tuple(token("석식", x) for x in ("10", "2", "1", "001")) + (token("조식", "2"),)
    stale = token("중식", "099")
    picker = SequenceValuePicker(values, {token("석식", "2"), stale, "옛 전체 값"})
    parent = picker.list.topLevelItem(0)
    assert [parent.child(i).text(0) for i in range(4)] == ["001", "1", "2", "10"]
    assert parent.checkState(0) == Qt.PartiallyChecked
    parent.setCheckState(0, Qt.Checked)
    assert all(parent.child(i).checkState(0) == Qt.Checked for i in range(4))
    parent.child(0).setCheckState(0, Qt.Unchecked)
    assert parent.checkState(0) == Qt.PartiallyChecked
    parent.setExpanded(False)
    assert not parent.isExpanded()
    parent.setExpanded(True)
    commit = QInputMethodEvent()
    commit.setCommitString("조식")
    app.sendEvent(picker.search, commit)
    assert parent.isHidden()
    assert not picker.list.topLevelItem(1).isHidden()
    result = []
    picker.applied.connect(lambda *args: result.append(args))
    picker.select_none.click()
    picker.apply.click()
    assert result[-1] == ("조식", frozenset())
    picker.reset.click()
    assert result[-1] == ("", None)
    empty = SequenceValuePicker((), frozenset())
    empty.applied.connect(lambda *args: result.append(args))
    empty.apply.click()
    assert result[-1] == ("", frozenset())
    picker.close()
    empty.close()


def test_sequence_present_tokens_and_picker_do_not_broaden_literal_selection(app):
    literal = sequence_pair_token("석식", "순서 있음")
    present = sequence_present_token("석식")
    assert present != literal != "순서 있음"
    assert sequence_present_meal(present) == "석식"
    assert sequence_present_meal(literal) is None
    assert sequence_present_meal("순서 있음") is None
    assert sequence_present_meal("sequence-present:v1:야식") is None
    with pytest.raises(ValueError, match="지원하지 않는 식종"):
        sequence_present_token("야식")

    values = (literal, present, sequence_present_token("조식"), sequence_present_token("중식"))
    picker = SequenceValuePicker(values, {literal})
    dinner = next(
        picker.list.topLevelItem(i)
        for i in range(picker.list.topLevelItemCount())
        if picker.list.topLevelItem(i).text(0) == "석식"
    )
    literal_item = next(
        dinner.child(i) for i in range(dinner.childCount())
        if dinner.child(i).data(0, Qt.ItemDataRole.UserRole) == literal
    )
    assert literal_item.checkState(0) == Qt.CheckState.Checked
    assert "정확히 일치" in literal_item.toolTip(0)
    assert all(item.data(0, Qt.ItemDataRole.UserRole) != present for item in picker._leaves)

    result = []
    picker.applied.connect(lambda *args: result.append(args))
    picker.apply.click()
    assert result[-1] == ("", frozenset({literal}))
    picker.close()

    restored = SequenceValuePicker(values, {present})
    restored_result = []
    restored.applied.connect(lambda *args: restored_result.append(args))
    assert any(item.data(0, Qt.ItemDataRole.UserRole) == present for item in restored._leaves)
    restored.apply.click()
    assert restored_result[-1] == ("", frozenset({present}))
    restored.close()


def test_sequence_present_model_filters_all_meals_and_survives_new_values():
    roster = roster_fixture()
    model = RosterTableModel(roster)
    semantic_tokens = {sequence_present_token(meal.value) for meal in Meal}
    assert semantic_tokens.isdisjoint(model.column_filter_values("sequence"))

    expected_by_meal = {
        Meal.DINNER: {"하나", "열"},
        Meal.BREAKFAST: {"둘", "보존"},
        Meal.LUNCH: set(),
    }
    for meal, expected in expected_by_meal.items():
        model.set_column_values("sequence", {sequence_present_token(meal.value)})
        assert {model.person_at(i).profile.name for i in range(model.rowCount())} == expected

    target = roster.persons[4]
    model.set_column_values("sequence", {sequence_present_token(Meal.LUNCH.value)})
    assert model.rowCount() == 0
    updated = replace(
        roster,
        assignments=(*roster.assignments, DeliveryAssignment(
            target.person_id, Meal.LUNCH, "기사", "새 순서",
        )),
    )
    model.replace_roster(updated)
    assert model.selected_column_values("sequence") == frozenset({sequence_present_token("중식")})
    assert model.rowCount() == 1
    assert model.person_at(0).person_id == target.person_id


def test_sequence_present_filter_does_not_collide_with_literal_value():
    literal_person, other_person = (
        Person(uuid4(), PersonProfile(name)) for name in ("문자 그대로", "다른 순서")
    )
    roster = RosterSnapshot(
        uuid4(),
        persons=(literal_person, other_person),
        enrollments=tuple(
            MealEnrollment(person.person_id, Meal.DINNER)
            for person in (literal_person, other_person)
        ),
        assignments=(
            DeliveryAssignment(literal_person.person_id, Meal.DINNER, sequence="순서 있음"),
            DeliveryAssignment(other_person.person_id, Meal.DINNER, sequence="2"),
        ),
    )
    model = RosterTableModel(roster)
    model.set_column_values("sequence", {sequence_pair_token("석식", "순서 있음")})
    assert [model.person_at(i).profile.name for i in range(model.rowCount())] == ["문자 그대로"]
    model.set_column_values("sequence", {sequence_present_token("석식")})
    assert {model.person_at(i).profile.name for i in range(model.rowCount())} == {
        "문자 그대로", "다른 순서",
    }


def test_sequence_pair_and_legacy_filters_restore_without_broadening(app, tmp_path):
    roster = roster_fixture()
    settings = QSettings(str(tmp_path / "view.ini"), QSettings.IniFormat)
    for selected in (
        {sequence_pair_token("조식", "2"), sequence_pair_token("중식", "missing")},
        {sequence_present_token("조식")},
        {sequence_exclusive_token("조식")},
        {"석식: 001\n조식: 2"},
        set(),
    ):
        window = RosterWindow(roster, column_settings=settings, inactive_settings=settings, view_settings_scope="sequence-test")
        window._on_column_values_changed("sequence", selected)
        expected = window.table_model.rowCount()
        window.close()
        restored = RosterWindow(roster, column_settings=settings, inactive_settings=settings, view_settings_scope="sequence-test")
        assert restored.table_model.selected_column_values("sequence") == frozenset(selected)
        assert restored.table_model.rowCount() == expected
        restored._column_search_header.open_value_filter("sequence")
        picker = restored._column_search_header._menu.findChild(SequenceValuePicker)
        picker.select_all.click()
        picker.cancel.click()
        assert restored.table_model.selected_column_values("sequence") == frozenset(selected)
        restored.close()


def test_real_ctrl_click_and_right_click_keep_exact_targets(app, tmp_path, monkeypatch):
    roster = roster_fixture()
    settings = QSettings(str(tmp_path / "mouse.ini"), QSettings.IniFormat)
    window = RosterWindow(roster, column_settings=settings, inactive_settings=settings)
    window.show()
    app.processEvents()
    table, model = window._table, window.table_model
    dinner = model.index(0, column(model, "status:dinner"))
    breakfast = model.index(1, column(model, "status:breakfast"))
    QTest.mouseClick(table.viewport(), Qt.LeftButton, pos=table.visualRect(dinner).center())
    QTest.mouseClick(table.viewport(), Qt.LeftButton, Qt.ControlModifier, table.visualRect(breakfast).center())
    targets = window.selected_meal_targets()
    assert len(targets) == 2
    requested = []
    window.batch_status_action_requested.connect(lambda *args: requested.append(args))
    original = window._create_context_menu
    menus = []
    def menu_for(*args):
        menu = original(*args)
        menus.append(menu)
        return SimpleNamespace(exec=lambda _pos: menu.actions()[2].trigger())
    monkeypatch.setattr(window, "_create_context_menu", menu_for)
    point = table.visualRect(breakfast).center()
    QTest.mouseClick(table.viewport(), Qt.RightButton, pos=point)
    # QtTest does not synthesize the OS context-menu event on the offscreen plugin.
    event = QContextMenuEvent(QContextMenuEvent.Mouse, point, table.viewport().mapToGlobal(point))
    app.sendEvent(table.viewport(), event)
    assert window.selected_meal_targets() == targets
    assert requested[-1] == (targets, MealStatus.STOPPED)
    assert menus[-1].actions()[0].text().startswith("선택한 식종 2개")
    window.close()


def manifest_rows(roster):
    first, second = roster.persons[:2]
    household = uuid4()
    dinner = DriverHouseholdRow(household, None, "기사", (first.person_id,), (first.profile.name,), (Meal.DINNER,), "주소", (), ("001",))
    breakfast = replace(dinner, member_ids=(second.person_id,), member_names=(second.profile.name,), meals=(Meal.BREAKFAST,), sequences=("2",))
    merged = replace(dinner, member_ids=(first.person_id, second.person_id), member_names=(first.profile.name, second.profile.name), meals=(Meal.DINNER, Meal.BREAKFAST), sequences=("001", "2"), meal_rows=(dinner, breakfast))
    return merged, dinner, breakfast


def test_applicable_blank_meal_pairs_filter_restore_and_keep_legacy_display(app, tmp_path):
    dinner, breakfast = Meal.DINNER, Meal.BREAKFAST
    mixed, blank, unspecified = (Person(uuid4(), PersonProfile(name))
                                 for name in ("일부 순서", "가입 빈 순서", "식종 미지정"))
    roster = RosterSnapshot(
        uuid4(), persons=(mixed, blank, unspecified),
        enrollments=tuple(MealEnrollment(person.person_id, meal)
                          for person in (mixed, blank) for meal in (dinner, breakfast)),
        assignments=(DeliveryAssignment(mixed.person_id, dinner, sequence="001"),),
    )
    model = RosterTableModel(roster)
    sequence = column(model, "sequence")
    contexts = {person.person_id: model.person_delivery_view(person) for person in roster.persons}
    assert contexts[mixed.person_id].sequence_pairs == (("석식", "001"), ("조식", ""))
    assert contexts[blank.person_id].sequence_pairs == (("석식", ""), ("조식", ""))
    assert contexts[unspecified.person_id].sequence_pairs == (("(빈 값)", ""),)
    assert contexts[mixed.person_id].sequence == "석식: 001"
    assert contexts[blank.person_id].sequence == ""
    model.sort(sequence, Qt.AscendingOrder)
    assert model.person_at(0).person_id == mixed.person_id
    selected = {sequence_pair_token("조식", "")}
    model.set_column_values("sequence", selected)
    assert {model.person_at(i).person_id for i in range(model.rowCount())} == {
        blank.person_id, unspecified.person_id,
    }
    model.set_column_values("sequence", {sequence_pair_token("석식", "")})
    assert {model.person_at(i).person_id for i in range(model.rowCount())} == {
        blank.person_id, unspecified.person_id,
    }
    model.set_column_values("sequence", {sequence_pair_token("(빈 값)", "")})
    assert model.rowCount() == 1 and model.person_at(0).person_id == unspecified.person_id
    # Old whole-row empty values continue to match every previously empty row.
    model.set_column_values("sequence", {""})
    assert {model.person_at(i).person_id for i in range(model.rowCount())} == {blank.person_id, unspecified.person_id}
    model.set_column_values("sequence", {"001"})
    assert model.rowCount() == 1 and model.person_at(0).person_id == mixed.person_id

    settings = QSettings(str(tmp_path / "blank-pairs.ini"), QSettings.IniFormat)
    window = RosterWindow(roster, column_settings=settings, inactive_settings=settings)
    window._include_hidden_filter.setChecked(True)
    window._on_column_values_changed("sequence", selected)
    window.close()
    reopened = RosterWindow(roster, column_settings=settings, inactive_settings=settings)
    assert reopened.table_model.selected_column_values("sequence") == frozenset(selected)
    assert reopened.table_model.rowCount() == 2
    reopened._column_search_header.open_value_filter("sequence")
    picker = reopened._column_search_header._menu.findChild(SequenceValuePicker)
    breakfast_parent = next(picker.list.topLevelItem(i) for i in range(picker.list.topLevelItemCount())
                            if picker.list.topLevelItem(i).text(0) == "조식")
    blank_item = next(
        breakfast_parent.child(i) for i in range(breakfast_parent.childCount())
        if breakfast_parent.child(i).data(0, Qt.ItemDataRole.UserRole)
        == sequence_pair_token("조식", "")
    )
    assert blank_item.text(0) == "(빈 값)"
    assert blank_item.checkState(0) == Qt.Checked
    picker.cancel.click()
    reopened.close()
