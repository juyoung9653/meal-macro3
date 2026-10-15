"""Integration coverage for all-meal household browsing and hidden members."""

import os
from datetime import date
from uuid import UUID

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QHeaderView, QLineEdit

from src.desktop_v2.driver_households import DriverHouseholdsDialog
from src.domain.delivery_routing import Driver, RoutingEvent, RoutingState
from src.domain.roster import (
    DeliveryAssignment,
    Household,
    HouseholdMembership,
    Meal,
    MealEnrollment,
    Person,
    PersonProfile,
    RosterSnapshot,
)
from src.services.driver_households import build_driver_household_rows

DAY = date(2026, 10, 14)


def uid(value: int) -> UUID:
    return UUID(f"20000000-0000-0000-0000-{value:012d}")


def person(value: int, name: str, *, phone: str = "") -> Person:
    return Person(
        uid(value),
        PersonProfile(
            name,
            road_address="성남대로 1",
            mobile_phone=phone or None,
        ),
    )


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_all_meal_browse_unions_explicit_sources_without_phantom_common_meals():
    enrolled = person(1, "현재 가입")
    future_only = person(2, "미래 배정")
    common_only = person(3, "공통 배정")
    roster = RosterSnapshot(
        uid(900),
        persons=(enrolled, future_only, common_only),
        enrollments=(MealEnrollment(enrolled.person_id, Meal.DINNER),),
        assignments=(
            DeliveryAssignment(enrolled.person_id, Meal.LUNCH, sequence="12"),
            DeliveryAssignment(common_only.person_id, None, sequence="99"),
        ),
    )
    state = RoutingState(
        events=(
            RoutingEvent(
                uid(100), enrolled.person_id, DAY, 1, {Meal.BREAKFAST: None}
            ),
            RoutingEvent(
                uid(101), future_only.person_id, date(2026, 10, 15), 2,
                {Meal.LUNCH: None},
            ),
        )
    )

    default_rows = build_driver_household_rows(roster, state, DAY)
    browse_rows = build_driver_household_rows(
        roster, state, DAY, browse_all_meals=True
    )

    assert len(default_rows) == 1
    assert default_rows[0].member_ids == (enrolled.person_id,)
    assert default_rows[0].meals == (Meal.DINNER,)
    assert len(browse_rows) == 1
    assert browse_rows[0].member_ids == (enrolled.person_id,)
    assert browse_rows[0].meals == (Meal.DINNER, Meal.BREAKFAST, Meal.LUNCH)

    later_rows = build_driver_household_rows(
        roster, state, date(2026, 10, 15), browse_all_meals=True
    )
    assert {row.member_ids for row in later_rows} == {
        (enrolled.person_id,),
        (future_only.person_id,),
    }
    assert common_only.person_id not in {
        person_id for row in later_rows for person_id in row.member_ids
    }


def _household_fixture():
    visible = person(10, "보이는 사람", phone="010-1111")
    hidden = person(11, "숨긴 사람", phone="010-9999")
    household = Household(uid(50))
    driver = Driver(uid(60), "기사")
    roster = RosterSnapshot(
        uid(901),
        persons=(visible, hidden),
        households=(household,),
        memberships=(
            HouseholdMembership(visible.person_id, household.household_id),
            HouseholdMembership(hidden.person_id, household.household_id),
        ),
        enrollments=(
            MealEnrollment(visible.person_id, Meal.DINNER),
            MealEnrollment(hidden.person_id, Meal.DINNER),
        ),
        assignments=(
            DeliveryAssignment(visible.person_id, Meal.DINNER, "기사", "1"),
            DeliveryAssignment(hidden.person_id, Meal.DINNER, "기사", "9"),
        ),
    )
    return roster, RoutingState((driver,)), driver, visible, hidden


def test_hidden_people_are_excluded_before_household_and_edit_target_projection():
    roster, state, _driver, visible, hidden = _household_fixture()

    (row,) = build_driver_household_rows(
        roster,
        state,
        DAY,
        browse_all_meals=True,
        excluded_person_ids={hidden.person_id},
    )

    assert row.member_ids == (visible.person_id,)
    assert row.member_names == ("보이는 사람",)
    assert row.contacts == ("010-1111",)
    assert row.sequences == ("1",)
    assert row.meal_rows[0].member_ids == (visible.person_id,)
    assert hidden.person_id not in row.meal_rows[0].member_ids


def _start_editor(dialog: DriverHouseholdsDialog, app: QApplication) -> QLineEdit:
    item = dialog.table.item(0, 3)
    dialog.table.scrollToItem(item)
    app.processEvents()
    point = dialog.table.visualItemRect(item).center()
    QTest.mouseClick(dialog.table.viewport(), Qt.MouseButton.LeftButton, pos=point)
    QTest.mouseDClick(dialog.table.viewport(), Qt.MouseButton.LeftButton, pos=point)
    app.processEvents()
    editor = next(
        (
            child
            for child in dialog.table.findChildren(
                QLineEdit, "householdSequenceEditor"
            )
            if child.isVisible()
        ),
        None,
    )
    assert editor is not None
    return editor


def test_include_hidden_toggle_rebuilds_and_preserves_browse_state(app):
    roster, state, driver, visible, hidden = _household_fixture()
    calls: list[bool] = []

    def rows_provider(include_hidden: bool):
        calls.append(include_hidden)
        return build_driver_household_rows(
            roster,
            state,
            DAY,
            browse_all_meals=True,
            excluded_person_ids=() if include_hidden else {hidden.person_id},
        )

    dialog = DriverHouseholdsDialog(
        rows_provider(False),
        service_day=DAY,
        drivers=(driver,),
        selected_driver_id=driver.driver_id,
        rows_provider=rows_provider,
    )
    dialog.show()
    assert (
        dialog.table.verticalHeader().sectionResizeMode(0)
        == QHeaderView.ResizeMode.ResizeToContents
    )
    dialog._set_column_search("address", "성남대로")
    dialog.table.sortItems(3, Qt.SortOrder.DescendingOrder)
    dialog.split_button.click()
    editor = _start_editor(dialog, app)
    assert editor._sequence_targets == ((visible.person_id, Meal.DINNER),)

    dialog.include_hidden_checkbox.click()
    app.processEvents()

    assert calls[-1] is True
    assert dialog.include_hidden is True
    assert not editor.isVisible()
    assert dialog._split_by_meal is True
    assert dialog._column_searches == {"address": "성남대로"}
    assert dialog.table.horizontalHeader().sortIndicatorOrder() == Qt.SortOrder.DescendingOrder
    combined = dialog.table.item(0, 3).data(Qt.ItemDataRole.UserRole)
    assert combined.member_ids == (visible.person_id, hidden.person_id)
    assert combined.contacts == ("010-1111", "010-9999")
    assert combined.sequences == ("1", "9")

    # Saving replaces rows through the same provider without changing the toggle
    # or the current split/filter/sort view.
    dialog.replace_rows(rows_provider(dialog.include_hidden))
    assert dialog.include_hidden is True
    assert dialog._split_by_meal is True
    assert dialog._column_searches == {"address": "성남대로"}

    dialog.include_hidden_checkbox.click()
    app.processEvents()
    assert calls[-1] is False
    assert dialog.include_hidden is False
    visible_again = dialog.table.item(0, 3).data(Qt.ItemDataRole.UserRole)
    assert visible_again.member_ids == (visible.person_id,)
    assert hidden.person_id not in visible_again.member_ids
    dialog.close()


def test_checkbox_without_rows_provider_stays_unchecked_and_disabled(app):
    dialog = DriverHouseholdsDialog((), service_day=DAY, drivers=())
    assert dialog.include_hidden is False
    assert not dialog.include_hidden_checkbox.isEnabled()
    assert dialog.table.textElideMode() == Qt.TextElideMode.ElideNone
    assert dialog.table.wordWrap() is True
    assert dialog.scope_label.text() == "가입·배정된 모든 식종 · 일시중지·중지 포함"
    assert dialog.empty.text() == "표시할 가입·배정 가구가 없습니다."
    dialog.close()
