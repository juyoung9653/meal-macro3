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
    assert dialog.scope_label.text() == "선택일 기준 배송·일시중지 식종"
    assert dialog.empty.text() == "표시할 가입·배정 가구가 없습니다."
    dialog.close()


def test_roster_provider_filters_meal_status_before_grouping_and_export(app):
    from dataclasses import replace
    from datetime import UTC, datetime

    from src.desktop_v2.roster_window import RosterWindow
    from src.domain.roster import InclusiveDateRange
    from src.domain.status import MealStatus, StatusEvent, resolve_status_history

    roster, state, driver, visible, hidden = _household_fixture()
    assignment_only = person(12, "가입 기록 없음", phone="010-8888")
    roster = replace(
        roster,
        persons=(*roster.persons, assignment_only),
        memberships=(*roster.memberships, HouseholdMembership(
            assignment_only.person_id, roster.households[0].household_id,
        )),
        enrollments=(*roster.enrollments,
            MealEnrollment(visible.person_id, Meal.BREAKFAST),
            MealEnrollment(visible.person_id, Meal.LUNCH),
        ),
        assignments=(*roster.assignments,
            DeliveryAssignment(visible.person_id, Meal.BREAKFAST, "기사", "2"),
            DeliveryAssignment(visible.person_id, Meal.LUNCH, "기사", "3"),
            DeliveryAssignment(assignment_only.person_id, Meal.LUNCH, "기사", "8"),
        ),
    )
    histories = {}
    for index, (target, meal, status) in enumerate((
        (visible, Meal.BREAKFAST, MealStatus.PAUSED),
        (hidden, Meal.DINNER, MealStatus.STOPPED),
    )):
        event = StatusEvent(
            uid(200 + index), roster.revision_id, target.person_id, meal, status,
            InclusiveDateRange(DAY, DAY), datetime(2026, 10, 14, tzinfo=UTC),
        )
        histories[(target.person_id, meal)] = resolve_status_history(
            target.person_id, meal, (event,),
        )
    window = RosterWindow(roster, histories, routing_state=state)
    window._set_day(DAY)
    dialog = DriverHouseholdsDialog(
        window.driver_household_rows(), service_day=DAY, drivers=(driver,),
        rows_provider=lambda include_hidden: window.driver_household_rows(
            include_hidden=include_hidden,
        ),
    )
    exported = []
    dialog.export_requested.connect(exported.append)
    try:
        # A recent stop must be excluded even before long-term archiving applies.
        assert hidden.person_id not in window.table_model._archive_ids
        (row,) = dialog._rows
        assert row.member_ids == (visible.person_id,)
        assert row.meals == (Meal.DINNER, Meal.BREAKFAST)
        assert row.contacts == ("010-1111",)
        assert row.sequences == ("1", "2")
        assert all(projection.member_ids == (visible.person_id,) for projection in row.meal_rows)
        dialog.template_export_button.click()
        assert [sheet.sheet_name for sheet in exported[-1].sheets] == ["기사-석식", "기사-조식"]
        assert dialog.current_view_snapshot().rows[0][1:4] == ("보이는 사람", "석식, 조식", "1, 2")

        dialog.include_hidden_checkbox.click()
        (row,) = dialog._rows
        assert set(row.member_ids) == {visible.person_id, hidden.person_id, assignment_only.person_id}
        assert row.meals == (Meal.DINNER, Meal.BREAKFAST, Meal.LUNCH)
        assert "중지·현재 상태 없음 포함" in dialog.scope_label.text()
        dialog.template_export_button.click()
        sheets = {sheet.sheet_name: sheet for sheet in exported[-1].sheets}
        assert set(sheets) == {"기사-석식", "기사-조식", "기사-중식"}
        assert sheets["기사-석식"].rows[0][1] == "보이는 사람, 숨긴 사람"
        assert sheets["기사-중식"].rows[0][1] == "보이는 사람, 가입 기록 없음"

        dialog.include_hidden_checkbox.click()
        dialog.template_export_button.click()
        assert [sheet.sheet_name for sheet in exported[-1].sheets] == ["기사-석식", "기사-조식"]
        assert dialog._rows[0].contacts == ("010-1111",)
    finally:
        dialog.close()
        window.close()
