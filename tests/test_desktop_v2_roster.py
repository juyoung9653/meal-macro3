"""Offscreen checks for the PySide6 date-spine roster shell."""

import os
from datetime import UTC, date, datetime, timedelta
from uuid import UUID

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QAbstractItemView, QApplication, QDialogButtonBox, QLabel

from src.desktop_v2 import (
    ImportReviewDialog,
    RosterTableModel,
    RosterWindow,
    StatusPeriodDialog,
)
from src.domain import (
    CustomFieldDefinition,
    CustomFieldType,
    DeliveryAssignment,
    Household,
    HouseholdMembership,
    InclusiveDateRange,
    Meal,
    MealEnrollment,
    MealStatus,
    Person,
    PersonProfile,
    RosterSnapshot,
    StatusEvent,
    resolve_status_history,
)
from src.domain.delivery_routing import Driver, RoutingEvent, RoutingState
from src.domain.entitlements import LunchDayRule, SchoolLunchMark
from src.domain.school_calendar import (
    SchoolLunchCalendar,
    SchoolLunchDay,
    SchoolLunchSchoolSchedule,
)
from src.importing import (
    HouseholdMatchCriterion,
    HouseholdMatchingRule,
    IdentityResolution,
    PersonObservation,
    ProfileField,
    SourceReference,
    stage_household_grouping,
    stage_reconciliation,
)

REVISION_ID = UUID("80000000-0000-0000-0000-000000000001")
FIRST_PERSON_ID = UUID("80000000-0000-0000-0000-000000000002")
SECOND_PERSON_ID = UUID("80000000-0000-0000-0000-000000000003")
HOUSEHOLD_ID = UUID("80000000-0000-0000-0000-000000000004")
CUSTOM_FIELD_ID = UUID("80000000-0000-0000-0000-000000000005")
SECOND_MOBILE_FIELD_ID = UUID("80000000-0000-0000-0000-000000000015")
SECOND_TELEPHONE_FIELD_ID = UUID("80000000-0000-0000-0000-000000000016")
EVENT_ID = UUID("80000000-0000-0000-0000-000000000006")
FIRST_OBSERVATION_ID = UUID("80000000-0000-0000-0000-000000000007")
SECOND_OBSERVATION_ID = UUID("80000000-0000-0000-0000-000000000008")
THIRD_OBSERVATION_ID = UUID("80000000-0000-0000-0000-000000000009")
SERVICE_DAY = date(2026, 8, 17)


@pytest.fixture(scope="module")
def app():
    application = QApplication.instance() or QApplication([])
    yield application


def _roster() -> RosterSnapshot:
    return RosterSnapshot(
        REVISION_ID,
        persons=(
            Person(
                FIRST_PERSON_ID,
                PersonProfile(
                    "홍길동",
                    birth_date=date(2015, 3, 2),
                    school_name="샘플초",
                    grade="6학년",
                    mobile_phone="010-1234-5678",
                    road_address="성남대로 1",
                    custom_values={CUSTOM_FIELD_ID: "A구역"},
                ),
            ),
            Person(
                SECOND_PERSON_ID,
                PersonProfile("김아침", birth_date=date(2016, 4, 3)),
            ),
        ),
        households=(Household(HOUSEHOLD_ID),),
        memberships=(
            HouseholdMembership(FIRST_PERSON_ID, HOUSEHOLD_ID),
            HouseholdMembership(SECOND_PERSON_ID, HOUSEHOLD_ID),
        ),
        enrollments=(
            MealEnrollment(FIRST_PERSON_ID, Meal.LUNCH),
            MealEnrollment(SECOND_PERSON_ID, Meal.BREAKFAST),
        ),
        assignments=(
            DeliveryAssignment(FIRST_PERSON_ID, Meal.LUNCH, "기사A", "001"),
        ),
        custom_fields=(
            CustomFieldDefinition(
                CUSTOM_FIELD_ID,
                "배송 구역",
                CustomFieldType.TEXT,
                0,
            ),
        ),
    )


def _histories():
    event = StatusEvent(
        EVENT_ID,
        REVISION_ID,
        FIRST_PERSON_ID,
        Meal.LUNCH,
        MealStatus.PAUSED,
        InclusiveDateRange(SERVICE_DAY, SERVICE_DAY),
        datetime(2026, 8, 24, 16, 0, tzinfo=UTC),
        "17일 일시중지",
    )
    return {
        (FIRST_PERSON_ID, Meal.LUNCH): resolve_status_history(
            FIRST_PERSON_ID,
            Meal.LUNCH,
            (event,),
        )
    }


def _calendar() -> SchoolLunchCalendar:
    coverage = InclusiveDateRange(SERVICE_DAY, SERVICE_DAY + timedelta(days=1))
    return SchoolLunchCalendar((
        SchoolLunchSchoolSchedule(
            "샘플초",
            (coverage,),
            tuple(
                SchoolLunchDay(day, LunchDayRule(SchoolLunchMark.VENDOR_REQUIRED))
                for day in (SERVICE_DAY, SERVICE_DAY + timedelta(days=1))
            ),
        ),
    ))


def test_model_filters_by_selected_meal_and_exposes_dynamic_columns(app):
    model = RosterTableModel(_roster(), _histories(), school_calendar=_calendar())
    model.set_context(SERVICE_DAY, Meal.LUNCH)

    assert model.rowCount() == 2
    lunch_column = next(
        index for index, column in enumerate(model.columns) if column.key == "status:lunch"
    )
    assert model.data(model.index(0, lunch_column)) == "기사A\n일시중지"
    assert model.data(model.index(0, lunch_column), model.StatusRole) == "일시중지"
    custom_column = next(
        index
        for index, column in enumerate(model.columns)
        if column.label == "배송 구역"
    )
    assert model.data(model.index(0, custom_column)) == "A구역"

    model.set_active_only(True)
    assert model.rowCount() == 1


def test_lunch_without_a_reviewed_school_schedule_displays_dash_with_driver(app):
    model = RosterTableModel(_roster(), _histories())
    model.set_context(SERVICE_DAY, Meal.LUNCH)
    lunch_column = next(
        index for index, column in enumerate(model.columns) if column.key == "status:lunch"
    )

    assert model.data(model.index(0, lunch_column)) == "기사A\n-"
    assert model.data(model.index(0, lunch_column), model.StatusRole) == "-"
    model.set_school_calendar(_calendar())
    assert model.data(model.index(0, lunch_column)) == "기사A\n일시중지"


def test_meal_columns_cache_independent_statuses_drivers_and_replacements(app):
    from dataclasses import replace
    from datetime import timedelta

    roster = _roster()
    breakfast_event = StatusEvent(
        UUID("80000000-0000-0000-0000-000000000021"),
        REVISION_ID,
        FIRST_PERSON_ID,
        Meal.BREAKFAST,
        MealStatus.STOPPED,
        InclusiveDateRange(SERVICE_DAY, SERVICE_DAY),
        datetime(2026, 8, 24, 16, 0, tzinfo=UTC),
        "조식 중지",
    )
    dinner_event = StatusEvent(
        UUID("80000000-0000-0000-0000-000000000022"),
        REVISION_ID,
        FIRST_PERSON_ID,
        Meal.DINNER,
        MealStatus.PAUSED,
        InclusiveDateRange(SERVICE_DAY, SERVICE_DAY),
        datetime(2026, 8, 24, 16, 0, tzinfo=UTC),
        "석식 일시중지",
    )
    three_meal_roster = replace(
        roster,
        enrollments=(
            *roster.enrollments,
            MealEnrollment(FIRST_PERSON_ID, Meal.BREAKFAST),
            MealEnrollment(FIRST_PERSON_ID, Meal.DINNER),
        ),
        assignments=(
            *roster.assignments,
            DeliveryAssignment(FIRST_PERSON_ID, Meal.BREAKFAST, "기사B", "010"),
            DeliveryAssignment(FIRST_PERSON_ID, Meal.DINNER, "기사C", "020"),
        ),
    )
    histories = {
        **_histories(),
        (FIRST_PERSON_ID, Meal.BREAKFAST): resolve_status_history(
            FIRST_PERSON_ID, Meal.BREAKFAST, (breakfast_event,)
        ),
        (FIRST_PERSON_ID, Meal.DINNER): resolve_status_history(
            FIRST_PERSON_ID, Meal.DINNER, (dinner_event,)
        ),
    }
    model = RosterTableModel(three_meal_roster, histories, school_calendar=_calendar())
    model.set_context(SERVICE_DAY, Meal.LUNCH)
    model.set_active_only(False)
    columns = {column.key: index for index, column in enumerate(model.columns)}

    assert tuple(columns)[:5] == (
        "name", "birth_date", "status:dinner", "status:breakfast", "status:lunch"
    )
    assert model.data(model.index(0, columns["status:breakfast"])) == "기사B\n미배송"
    assert model.data(model.index(0, columns["status:lunch"])) == "기사A\n일시중지"
    assert model.data(model.index(0, columns["status:dinner"])) == "기사C\n일시중지"
    assert model.data(model.index(0, columns["status:breakfast"]), model.StatusRole) == "중지"
    assert model.data(model.index(1, columns["status:lunch"])) == "미지정\n-"

    model.set_context(SERVICE_DAY + timedelta(days=1), Meal.LUNCH)
    assert model.data(model.index(0, columns["status:breakfast"])) == "기사B\n배송"
    model.sort(columns["status:dinner"])
    model.replace_roster(three_meal_roster, {})
    assert model.columns[model._sort_column].key == "status:dinner"
    first_row = next(
        row for row in range(model.rowCount())
        if model.person_at(row).person_id == FIRST_PERSON_ID
    )
    assert model.data(model.index(first_row, columns["status:dinner"]), model.StatusRole) == "배송"


def test_numbered_contact_columns_are_adjacent_searchable_and_inspected(app):
    roster = _roster()
    first = roster.person(FIRST_PERSON_ID)
    phone_roster = RosterSnapshot(
        roster.revision_id,
        persons=(
            Person(
                first.person_id,
                PersonProfile(
                    first.profile.name,
                    birth_date=first.profile.birth_date,
                    school_name=first.profile.school_name,
                    grade=first.profile.grade,
                    telephone="031-123-4567",
                    mobile_phone=first.profile.mobile_phone,
                    road_address=first.profile.road_address,
                    custom_values={
                        **first.profile.custom_values,
                        SECOND_MOBILE_FIELD_ID: "010-9876-5432",
                        SECOND_TELEPHONE_FIELD_ID: "031-987-6543",
                    },
                ),
            ),
            roster.person(SECOND_PERSON_ID),
        ),
        households=roster.households,
        memberships=roster.memberships,
        enrollments=roster.enrollments,
        assignments=roster.assignments,
        custom_fields=(
            *roster.custom_fields,
            CustomFieldDefinition(
                SECOND_TELEPHONE_FIELD_ID,
                "전화번호2",
                CustomFieldType.TEXT,
                1,
            ),
            CustomFieldDefinition(
                SECOND_MOBILE_FIELD_ID,
                "핸드폰2",
                CustomFieldType.TEXT,
                2,
            ),
        ),
    )
    model = RosterTableModel(phone_roster, _histories())
    labels = tuple(column.label for column in model.columns)
    telephone1_index = labels.index("전화번호1")
    phone1_index = labels.index("핸드폰1")

    assert labels[telephone1_index + 1] == "전화번호2"
    assert model.data(model.index(0, telephone1_index)) == "031-123-4567"
    assert model.data(model.index(0, telephone1_index + 1)) == "031-987-6543"
    assert labels[phone1_index + 1] == "핸드폰2"
    assert model.data(model.index(0, phone1_index)) == "010-1234-5678"
    assert model.data(model.index(0, phone1_index + 1)) == "010-9876-5432"

    model.set_search("9876-5432")
    assert model.rowCount() == 1
    assert model.person_at(0).person_id == FIRST_PERSON_ID

    model.set_search("987-6543")
    assert model.rowCount() == 1
    assert model.person_at(0).person_id == FIRST_PERSON_ID

    window = RosterWindow(phone_roster, _histories())
    window._set_day(SERVICE_DAY)
    window._set_meal(Meal.LUNCH)
    window.show()
    window._table.selectRow(0)
    app.processEvents()
    assert "핸드폰 010-1234-5678\n핸드폰 010-9876-5432" in window._inspector_detail.text()
    assert "전화 031-123-4567\n전화 031-987-6543" in window._inspector_detail.text()
    window.close()


def test_model_sorts_blank_cells_to_one_side(app):
    model = RosterTableModel(_roster(), _histories())
    model.set_context(SERVICE_DAY, Meal.LUNCH)
    model.set_active_only(False)
    mobile_column = next(
        index for index, column in enumerate(model.columns) if column.key == "mobile"
    )

    model.sort(mobile_column, Qt.SortOrder.DescendingOrder)

    assert model.data(model.index(0, mobile_column)) == ""
    assert model.person_at(0).person_id == SECOND_PERSON_ID
    assert model.person_at(1).person_id == FIRST_PERSON_ID


def test_roster_table_enables_header_sorting(app):
    window = RosterWindow(_roster(), _histories())

    assert window._table.isSortingEnabled()
    assert "정렬" in window._table.horizontalHeader().toolTip()
    window.close()


def test_meal_context_menu_emits_status_and_assignment_requests(app):
    window = RosterWindow(_roster(), _histories())
    window._set_day(SERVICE_DAY)
    window._set_meal(Meal.LUNCH)
    person = window.table_model.person_at(0)
    requested = []
    assignments = []
    window.status_action_requested.connect(lambda *args: requested.append(args))
    window.driver_assignment_requested.connect(lambda *args: assignments.append(args))

    menu = window._create_context_menu(person, Meal.LUNCH)

    assert [action.text() for action in menu.actions()] == [
        "중식 배송",
        "중식 일시중지",
        "중식 미배송",
        "배송담당 변경",
        "이력보기",
        "민원",
        "",
        "명단 행 삭제",
    ]
    menu.actions()[1].trigger()
    assert requested == [(FIRST_PERSON_ID, Meal.LUNCH, MealStatus.PAUSED)]
    menu.actions()[3].trigger()
    assert assignments == [(FIRST_PERSON_ID, Meal.LUNCH)]
    window.close()


def test_routing_state_updates_effective_delivery_view_and_driver_filter(app):
    from dataclasses import replace

    driver_id = UUID("80000000-0000-0000-0000-000000000031")
    state = RoutingState(
        drivers=(Driver(driver_id, "기사변경", "010-9999-0000"),),
        events=(
            RoutingEvent(
                UUID("80000000-0000-0000-0000-000000000032"),
                FIRST_PERSON_ID,
                SERVICE_DAY,
                1,
                {Meal.LUNCH: driver_id},
                "새 주소로 2",
                "101호",
            ),
        ),
    )
    roster = replace(
        _roster(),
        assignments=(
            *_roster().assignments,
            DeliveryAssignment(SECOND_PERSON_ID, Meal.LUNCH, "기사변경", "099"),
        ),
    )
    model = RosterTableModel(
        roster, _histories(), routing_state=state, school_calendar=_calendar()
    )
    lunch_column = next(
        index for index, column in enumerate(model.columns) if column.key == "status:lunch"
    )
    address_column = next(
        index for index, column in enumerate(model.columns) if column.key == "address"
    )

    model.set_context(date.fromordinal(SERVICE_DAY.toordinal() - 1), Meal.LUNCH)
    assert model.data(model.index(0, lunch_column)) == "기사A\n-"
    assert model.data(model.index(0, address_column)) == "성남대로 1"
    model.set_context(SERVICE_DAY, Meal.LUNCH)

    assert model.data(model.index(0, lunch_column)) == "기사변경\n일시중지"
    assert model.data(model.index(0, address_column)) == "새 주소로 2 101호"
    model.set_active_only(False)
    model.set_driver_filter(driver_id)
    assert model.rowCount() == 2
    model.set_driver_filter(UUID("80000000-0000-0000-0000-000000000033"))
    assert model.rowCount() == 0

    window = RosterWindow(
        roster, _histories(), routing_state=state, school_calendar=_calendar()
    )
    window._set_day(SERVICE_DAY)
    window._set_meal(Meal.LUNCH)
    window.show()
    window._table.selectRow(0)
    app.processEvents()
    assert "기사변경" not in window._inspector_detail.text()
    assert "새 주소로 2 101호" in window._inspector_detail.text()
    relocation_requests = []
    window.relocation_requested.connect(relocation_requests.append)
    person = window.table_model.person_at(0)
    ordinary = window._create_context_menu(person, column_key="name")
    assert [action.text() for action in ordinary.actions()] == ["정보 수정", "이력보기", "민원", "", "명단 행 삭제"]
    address = window._create_context_menu(person, column_key="address")
    assert [action.text() for action in address.actions()] == ["정보 수정", "이사 등록", "이력보기", "민원", "", "명단 행 삭제"]
    address.actions()[1].trigger()
    assert relocation_requests == [FIRST_PERSON_ID]
    driver_index = window._driver_filter.findData(driver_id)
    assert driver_index > 0
    assert window._driver_filter.itemData(driver_index) == driver_id
    assert isinstance(window._driver_filter.itemData(driver_index), UUID)
    window.close()


def test_unit_price_action_emits_global_management_request(app):
    window = RosterWindow(_roster(), _histories(), school_calendar=_calendar())
    requested = []
    window.unit_price_requested.connect(lambda: requested.append(True))

    window._unit_price_action.trigger()
    app.processEvents()

    assert requested == [True]
    window.close()


def test_service_adjustment_action_emits_vendor_schedule_request(app):
    window = RosterWindow(_roster(), _histories(), school_calendar=_calendar())
    requested = []
    window.service_adjustment_requested.connect(lambda: requested.append(True))

    window._service_adjustment_action.trigger()
    app.processEvents()

    assert requested == [True]
    assert "청구 제외" in window._service_adjustment_action.toolTip()
    window.close()


def test_settings_action_emits_excel_password_request(app):
    window = RosterWindow(_roster(), _histories())
    requested = []
    window.settings_requested.connect(lambda: requested.append(True))

    window._settings_action.trigger()
    app.processEvents()

    assert requested == [True]
    assert "Excel" in window._settings_action.toolTip()
    window.close()


def test_single_document_button_emits_output_hub_request(app):
    window = RosterWindow(_roster(), _histories())
    requested = []
    window.documents_requested.connect(lambda: requested.append(True))

    window._documents_button.click()
    app.processEvents()

    assert requested == [True]
    assert window._documents_button.text() == "문서 만들기"
    assert "청구서" in window._documents_button.toolTip()
    window.close()


def test_minimum_width_header_keeps_actions_and_filters_on_separate_rows(app):
    window = RosterWindow(_roster(), _histories())
    window.resize(1080, 680)
    window.show()
    app.processEvents()

    assert (
        window._documents_button.geometry().right()
        < window._documents_button.parentWidget().width()
    )
    assert window._column_search_header.buttons["name"].isVisible()
    assert window._driver_filter.geometry().right() < window._driver_households_button.geometry().left()
    window.close()


def test_special_household_filter_shows_details_only_while_checked(app):
    from dataclasses import replace
    from src.domain.roster import HouseholdDeliverySetting

    roster = _roster()
    roster = replace(roster, households=(Household(HOUSEHOLD_ID, (
        HouseholdDeliverySetting(Meal.DINNER, (0, 1, 2, 3, 4), "도시락"),
        HouseholdDeliverySetting(Meal.BREAKFAST, (0, 1, 2, 3, 4, 5, 6), "보냉 상자"),
        HouseholdDeliverySetting(Meal.LUNCH, form="식재료 박스"),
    )),))
    window = RosterWindow(roster, _histories())
    window._set_day(SERVICE_DAY)
    assert "delivery_settings" not in {column.key for column in window.table_model.columns}
    window._special_only_filter.setChecked(True)
    assert window.table_model.rowCount() == 2
    column = next(i for i, item in enumerate(window.table_model.columns) if item.key == "delivery_settings")
    detail = window.table_model.index(0, column).data()
    assert detail.splitlines()[0].startswith("석식:")
    assert detail.splitlines()[1].startswith("조식:")
    assert detail.splitlines()[2].startswith("중식:")
    assert "도시락" in detail
    window.replace_roster(replace(roster, households=(Household(HOUSEHOLD_ID),)), _histories())
    assert window.table_model.rowCount() == 0
    window._special_only_filter.setChecked(False)
    assert window.table_model.rowCount() == 2
    assert "delivery_settings" not in {column.key for column in window.table_model.columns}
    window.close()


def test_roster_status_respects_explicit_weekdays_without_changing_default(app):
    from dataclasses import replace
    from src.domain.roster import HouseholdDeliverySetting

    roster = _roster()
    roster = replace(roster, enrollments=(*roster.enrollments, MealEnrollment(FIRST_PERSON_ID, Meal.DINNER)))
    model = RosterTableModel(roster)
    saturday = date(2026, 8, 22)
    model.set_context(saturday, Meal.DINNER)
    status_column = next(i for i, column in enumerate(model.columns) if column.key == "status:dinner")
    assert model.index(0, status_column).data(model.StatusRole) == MealStatus.DELIVERY.value
    weekdays_only = replace(roster, households=(Household(HOUSEHOLD_ID, (
        HouseholdDeliverySetting(Meal.DINNER, (0, 1, 2, 3, 4)),
    )),))
    model.replace_roster(weekdays_only)
    assert model.index(0, status_column).data(model.StatusRole) == "-"
    saturday_service = replace(roster, households=(Household(HOUSEHOLD_ID, (
        HouseholdDeliverySetting(Meal.DINNER, (5,)),
    )),))
    model.replace_roster(saturday_service)
    assert model.index(0, status_column).data(model.StatusRole) == MealStatus.DELIVERY.value


def test_row_selection_populates_household_inspector(app):
    window = RosterWindow(_roster(), _histories(), school_calendar=_calendar())
    window._set_day(SERVICE_DAY)
    window._set_meal(Meal.LUNCH)
    edit_requests = []
    delivery_requests = []
    window.person_edit_requested.connect(edit_requests.append)
    window.delivery_settings_requested.connect(delivery_requests.append)
    assert not window._edit_person_button.isEnabled()
    assert not window._delivery_settings_button.isEnabled()
    window.show()
    window._table.selectRow(0)
    app.processEvents()

    assert window._inspector_name.text() == "홍길동"
    assert window.findChild(QLabel, "statusBadge") is None
    assert window._inspector_detail.text() == (
        "[연락처]\n핸드폰 010-1234-5678\n[주소]\n성남대로 1"
    )
    assert window._household_list.count() == 2
    assert "김아침" in window._household_list.item(1).text()
    assert window._meal_history_list.count() == 1
    assert window._meal_history_list.item(0).text() == (
        "2026-08-25 01:00  중식 · 17일 일시중지"
    )
    assert (
        window._table.selectionBehavior()
        is QAbstractItemView.SelectionBehavior.SelectItems
    )
    assert (
        window._table.selectionMode()
        is QAbstractItemView.SelectionMode.ExtendedSelection
    )
    assert window._edit_person_button.isEnabled()
    window._edit_person_button.click()
    assert edit_requests == [FIRST_PERSON_ID]
    assert window._delivery_settings_button.isEnabled()
    window._delivery_settings_button.click()
    assert delivery_requests == [FIRST_PERSON_ID]
    window._clear_inspector()
    assert not window._delivery_settings_button.isEnabled()
    window.close()


def test_inspector_uses_meal_history_provider_in_a_scrollable_list(app):
    calls = []

    def provider(person):
        calls.append(person.person_id)
        return tuple(
            (
                f"{person.profile.name}의 아주 긴 식종 변경 이력 {index}",
                f"전체 이력 {index}",
            )
            for index in range(6, 0, -1)
        )

    window = RosterWindow(_roster(), _histories(), school_calendar=_calendar())
    window._set_day(SERVICE_DAY)
    window._set_meal(Meal.LUNCH)
    window._set_active_only(False)
    window.show()
    window._table.selectRow(0)
    app.processEvents()
    window.set_meal_change_history_provider(provider)
    app.processEvents()

    assert window._meal_history_list.count() == 6
    assert window._meal_history_list.item(0).text() == "홍길동의 아주 긴 식종 변경 이력 6"
    assert window._meal_history_list.item(0).toolTip() == "전체 이력 6"
    assert window._meal_history_list.textElideMode() is Qt.TextElideMode.ElideNone
    assert window._meal_history_list.wordWrap()
    assert window._meal_history_all_button.isEnabled()
    assert window._household_list.height() <= 92
    assert (
        window._meal_history_list.visualItemRect(
            window._meal_history_list.item(5),
        ).bottom()
        > window._meal_history_list.viewport().rect().bottom()
    )
    assert window._meal_history_list.verticalScrollBar().maximum() > 0
    window._table.selectRow(1)
    app.processEvents()
    assert window._meal_history_list.item(0).text() == "김아침의 아주 긴 식종 변경 이력 6"
    assert calls == [FIRST_PERSON_ID, SECOND_PERSON_ID]
    opened = []
    window.set_history_provider(lambda person, meal, parent: opened.append((person.person_id, meal)) or None)
    window._meal_history_all_button.click()
    assert opened == [(SECOND_PERSON_ID, None)]
    window.close()


def test_inspector_long_history_wraps_and_separates_dates(app):
    from src.desktop_v2.inspector_history import MealHistoryRow

    window = RosterWindow(_roster(), _histories(), school_calendar=_calendar())
    window.resize(1080, 680)
    window.show()
    window._table.selectRow(0)
    app.processEvents()
    row = MealHistoryRow(
        "중식 · 상태: 이전 배송 상태에서 일시중지로 변경된 긴 이력 항목",
        "전체 변경 상세",
        "2026-09-24",
        "2026-09-22 10:25:43",
    )
    window.set_meal_change_history_provider(lambda person: (row,) * 6)
    app.processEvents()
    item = window._meal_history_list.item(0)
    entry = window._meal_history_list.itemWidget(item)
    labels = entry.findChildren(QLabel)
    assert len(labels) == 3
    assert labels[0].wordWrap() and labels[1].wordWrap() and labels[2].wordWrap()
    assert labels[1].text() == "적용 2026-09-24"
    assert labels[2].text() == "기록 2026-09-22 10:25"
    assert item.sizeHint().height() >= sum(label.heightForWidth(window._meal_history_list.viewport().width() - 16) for label in labels)
    assert window._meal_history_list.verticalScrollBar().maximum() > 0
    window.close()


def test_roster_window_refreshes_after_later_import(app):
    roster = _roster()
    later_person_id = UUID("80000000-0000-0000-0000-000000000041")
    later_revision_id = UUID("8a000000-0000-0000-0000-000000000042")
    later_roster = RosterSnapshot(
        later_revision_id,
        persons=(
            *roster.persons,
            Person(
                later_person_id,
                PersonProfile(
                    "나중에 추가된 아동",
                    birth_date=date(2019, 5, 4),
                    mobile_phone="01012345678",
                ),
            ),
        ),
        households=roster.households,
        memberships=(
            *roster.memberships,
            HouseholdMembership(later_person_id, HOUSEHOLD_ID),
        ),
        enrollments=(
            *roster.enrollments,
            MealEnrollment(later_person_id, Meal.LUNCH),
        ),
        assignments=roster.assignments,
        custom_fields=roster.custom_fields,
    )
    window = RosterWindow(roster, _histories())
    window._set_day(SERVICE_DAY)
    window._set_meal(Meal.LUNCH)

    window.replace_roster(
        later_roster,
        _histories(),
        revision_display_code="20260826-002",
    )

    assert window.table_model.rowCount() == 3
    assert window._revision_label.text() == "변경번호\n20260826-002"
    names = {
        window.table_model.person_at(row).profile.name
        for row in range(window.table_model.rowCount())
    }
    assert names == {"홍길동", "김아침", "나중에 추가된 아동"}
    window.close()


def test_import_review_enables_commit_only_after_ambiguous_identity_choice(app):
    observations = (
        PersonObservation(
            FIRST_OBSERVATION_ID,
            SourceReference("latest.xlsx", "명단", (2,)),
            {
                ProfileField.NAME: "홍길동",
                ProfileField.BIRTH_DATE: date(2015, 3, 2),
            },
            (Meal.LUNCH,),
        ),
        PersonObservation(
            SECOND_OBSERVATION_ID,
            SourceReference("latest.xlsx", "명단", (3,)),
            {
                ProfileField.NAME: "홍길동",
                ProfileField.BIRTH_DATE: date(2017, 3, 2),
            },
            (Meal.LUNCH,),
        ),
        PersonObservation(
            THIRD_OBSERVATION_ID,
            SourceReference("latest.xlsx", "명단", (4,)),
            {
                ProfileField.NAME: "새아동",
                ProfileField.BIRTH_DATE: date(2018, 4, 3),
            },
            (Meal.BREAKFAST,),
        ),
    )
    roster = _roster()
    plan = stage_reconciliation(
        roster,
        observations,
        effective_on=SERVICE_DAY,
    )
    dialog = ImportReviewDialog(roster, plan)
    ok_button = dialog._buttons.button(QDialogButtonBox.StandardButton.Ok)

    assert not ok_button.isEnabled()
    review_combo = dialog._combos[SECOND_OBSERVATION_ID]
    review_combo.setCurrentIndex(review_combo.findData(FIRST_PERSON_ID))
    app.processEvents()

    assert ok_button.isEnabled()
    decisions = dialog.decisions()
    assert decisions[SECOND_OBSERVATION_ID].resolution is IdentityResolution.LINK_EXISTING
    assert decisions[SECOND_OBSERVATION_ID].person_id == FIRST_PERSON_ID
    assert decisions[THIRD_OBSERVATION_ID].resolution is IdentityResolution.CREATE_NEW
    assert FIRST_OBSERVATION_ID not in decisions
    dialog.close()


def test_initial_import_review_defaults_every_valid_row_to_new(app):
    observation = PersonObservation(
        THIRD_OBSERVATION_ID,
        SourceReference("existing.xlsx", "명단", (2,)),
        {
            ProfileField.NAME: "첫아동",
            ProfileField.BIRTH_DATE: date(2018, 4, 3),
        },
        (Meal.BREAKFAST,),
    )
    plan = stage_reconciliation(None, (observation,), effective_on=SERVICE_DAY)
    dialog = ImportReviewDialog(None, plan)

    ok_button = dialog._buttons.button(QDialogButtonBox.StandardButton.Ok)
    assert ok_button.isEnabled()
    decision = dialog.decisions()[THIRD_OBSERVATION_ID]
    assert decision.resolution is IdentityResolution.CREATE_NEW
    dialog.close()


def test_import_review_can_split_one_person_from_proposed_household(app):
    observations = (
        PersonObservation(
            FIRST_OBSERVATION_ID,
            SourceReference("existing.xlsx", "명단", (2,)),
            {
                ProfileField.NAME: "첫아동",
                ProfileField.BIRTH_DATE: date(2017, 1, 2),
                ProfileField.MOBILE_PHONE: "010-1234-5678",
            },
            (Meal.LUNCH,),
        ),
        PersonObservation(
            SECOND_OBSERVATION_ID,
            SourceReference("existing.xlsx", "명단", (3,)),
            {
                ProfileField.NAME: "둘째아동",
                ProfileField.BIRTH_DATE: date(2018, 2, 3),
                ProfileField.MOBILE_PHONE: "01012345678",
            },
            (Meal.LUNCH,),
        ),
    )
    plan = stage_reconciliation(None, observations, effective_on=SERVICE_DAY)
    household_ids = iter(
        (
            UUID("80000000-0000-0000-0000-000000000021"),
            UUID("80000000-0000-0000-0000-000000000022"),
            UUID("80000000-0000-0000-0000-000000000023"),
        )
    )
    household_plan = stage_household_grouping(
        observations,
        HouseholdMatchingRule(
            UUID("80000000-0000-0000-0000-000000000024"),
            1,
            (HouseholdMatchCriterion.MOBILE_PHONE,),
        ),
        effective_on=SERVICE_DAY,
        household_id_factory=lambda: next(household_ids),
    )
    dialog = ImportReviewDialog(None, plan, household_plan=household_plan)

    first_combo = dialog._household_combos[FIRST_OBSERVATION_ID]
    second_combo = dialog._household_combos[SECOND_OBSERVATION_ID]
    assert first_combo.currentData() == second_combo.currentData()
    assert "2명" in first_combo.currentText()
    assert "핸드폰" in first_combo.toolTip()

    second_combo.setCurrentIndex(1)
    decisions = dialog.household_decisions()
    assert decisions[FIRST_OBSERVATION_ID].household_id != decisions[
        SECOND_OBSERVATION_ID
    ].household_id
    assert second_combo.currentText() == "이 사람만 별도 가구"
    dialog.close()


def test_import_review_defaults_later_new_person_to_matching_existing_household(app):
    roster = _roster()
    observation = PersonObservation(
        THIRD_OBSERVATION_ID,
        SourceReference("later.xlsx", "명단", (2,)),
        {
            ProfileField.NAME: "나중에 추가된 아동",
            ProfileField.BIRTH_DATE: date(2019, 5, 4),
            ProfileField.MOBILE_PHONE: "01012345678",
        },
        (Meal.LUNCH,),
    )
    plan = stage_reconciliation(roster, (observation,), effective_on=SERVICE_DAY)
    household_plan = stage_household_grouping(
        (observation,),
        HouseholdMatchingRule(
            UUID("80000000-0000-0000-0000-000000000031"),
            1,
        ),
        effective_on=SERVICE_DAY,
        roster=roster,
        household_id_factory=lambda: UUID(
            "80000000-0000-0000-0000-000000000032"
        ),
    )
    dialog = ImportReviewDialog(roster, plan, household_plan=household_plan)

    combo = dialog._household_combos[THIRD_OBSERVATION_ID]
    assert combo.currentData() == HOUSEHOLD_ID
    assert combo.currentText().startswith("기존 가구에 포함")
    assert "홍길동" in combo.toolTip()
    assert "핸드폰" in combo.toolTip()

    combo.setCurrentIndex(combo.findData(
        household_plan.separate_household_ids[THIRD_OBSERVATION_ID]
    ))
    assert dialog.household_decisions()[THIRD_OBSERVATION_ID].household_id != (
        HOUSEHOLD_ID
    )
    dialog.close()


def test_import_review_requires_choice_when_two_existing_households_match(app):
    roster = _roster()
    other_person_id = UUID("80000000-0000-0000-0000-000000000033")
    other_household_id = UUID("80000000-0000-0000-0000-000000000034")
    ambiguous_roster = RosterSnapshot(
        roster.revision_id,
        persons=(
            *roster.persons,
            Person(
                other_person_id,
                PersonProfile(
                    "다른 가구 보호자",
                    birth_date=date(1980, 1, 1),
                    mobile_phone="01012345678",
                ),
            ),
        ),
        households=(*roster.households, Household(other_household_id)),
        memberships=(
            *roster.memberships,
            HouseholdMembership(other_person_id, other_household_id),
        ),
        enrollments=roster.enrollments,
        assignments=roster.assignments,
        custom_fields=roster.custom_fields,
    )
    observation = PersonObservation(
        THIRD_OBSERVATION_ID,
        SourceReference("later.xlsx", "명단", (2,)),
        {
            ProfileField.NAME: "후보가 둘인 아동",
            ProfileField.BIRTH_DATE: date(2019, 5, 4),
            ProfileField.MOBILE_PHONE: "01012345678",
        },
        (Meal.LUNCH,),
    )
    plan = stage_reconciliation(
        ambiguous_roster,
        (observation,),
        effective_on=SERVICE_DAY,
    )
    household_plan = stage_household_grouping(
        (observation,),
        HouseholdMatchingRule(
            UUID("80000000-0000-0000-0000-000000000035"),
            1,
        ),
        effective_on=SERVICE_DAY,
        roster=ambiguous_roster,
        household_id_factory=lambda: UUID(
            "80000000-0000-0000-0000-000000000036"
        ),
    )
    dialog = ImportReviewDialog(
        ambiguous_roster,
        plan,
        household_plan=household_plan,
    )
    ok_button = dialog._buttons.button(QDialogButtonBox.StandardButton.Ok)
    combo = dialog._household_combos[THIRD_OBSERVATION_ID]

    assert combo.currentText() == "기존 가구 선택 필요"
    assert not ok_button.isEnabled()
    combo.setCurrentIndex(combo.findData(HOUSEHOLD_ID))
    app.processEvents()
    assert ok_button.isEnabled()
    assert dialog.household_decisions()[THIRD_OBSERVATION_ID].household_id == (
        HOUSEHOLD_ID
    )
    dialog.close()


def test_status_period_dialog_supports_closed_pause_and_open_stop(app):
    pause = StatusPeriodDialog(MealStatus.PAUSED, SERVICE_DAY)
    stopped = StatusPeriodDialog(MealStatus.STOPPED, SERVICE_DAY)

    assert pause.selected_period() == InclusiveDateRange(SERVICE_DAY, SERVICE_DAY)
    assert stopped.selected_period() == InclusiveDateRange(SERVICE_DAY, None)

    stopped._open_start.setChecked(True)
    with pytest.raises(ValueError, match="동시에"):
        stopped.selected_period()
    pause.close()
    stopped.close()


def test_scrolling_uses_projection_without_requerying_roster(app, monkeypatch):
    from PySide6.QtWidgets import QTableView

    model = RosterTableModel(_roster(), _histories(), school_calendar=_calendar())
    model.set_context(SERVICE_DAY, Meal.LUNCH)
    model.set_active_only(False)
    expected = [
        [model.data(model.index(row, col)) for col in range(model.columnCount())]
        for row in range(model.rowCount())
    ]

    def unexpected_query(*args, **kwargs):
        raise AssertionError('Painting must not query roster or resolve history')

    monkeypatch.setattr(RosterSnapshot, 'meals_for', unexpected_query)
    monkeypatch.setattr(RosterSnapshot, 'resolve_assignment', unexpected_query)
    history_type = type(next(iter(_histories().values())))
    monkeypatch.setattr(history_type, 'status_on', unexpected_query)
    roles = (
        Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole,
        Qt.ItemDataRole.ForegroundRole, Qt.ItemDataRole.FontRole,
        Qt.ItemDataRole.TextAlignmentRole, Qt.ItemDataRole.BackgroundRole,
        Qt.ItemDataRole.DecorationRole, model.PersonIdRole, model.StatusRole,
    )
    for row in range(model.rowCount()):
        for col in range(model.columnCount()):
            for role in roles:
                model.data(model.index(row, col), role)
            assert model.data(model.index(row, col)) == expected[row][col]
    view = QTableView()
    view.setModel(model)
    view.resize(320, 100)
    try:
        view.show()
        app.processEvents()
        view.verticalScrollBar().setValue(view.verticalScrollBar().maximum())
        view.horizontalScrollBar().setValue(view.horizontalScrollBar().maximum())
        app.processEvents()
        assert sum(model.status_counts().values()) == 2
    finally:
        view.close()


def test_projection_updates_after_context_and_roster_changes(app):
    from dataclasses import replace
    from datetime import timedelta

    roster = _roster()
    model = RosterTableModel(roster, _histories(), school_calendar=_calendar())
    model.set_context(SERVICE_DAY, Meal.LUNCH)
    lunch_column = next(
        index for index, column in enumerate(model.columns) if column.key == "status:lunch"
    )
    name_column = next(
        index for index, column in enumerate(model.columns) if column.key == "name"
    )
    assert model.data(model.index(0, lunch_column)) == "기사A\n일시중지"
    model.set_context(SERVICE_DAY + timedelta(days=1), Meal.LUNCH)
    assert model.data(model.index(0, lunch_column)) == "기사A\n배송"
    model.set_context(SERVICE_DAY, Meal.BREAKFAST)
    assert model.person_at(0).person_id == FIRST_PERSON_ID
    model.set_context(SERVICE_DAY, Meal.LUNCH)
    changed = replace(roster.persons[0], profile=replace(roster.persons[0].profile, name='수정된 이름'))
    model.replace_roster(replace(roster, persons=(changed, roster.persons[1])), {})
    assert model.data(model.index(0, lunch_column)) == "기사A\n배송"
    assert model.data(model.index(0, name_column)) == '수정된 이름'
    model.set_search('수정된')
    assert model.rowCount() == 1
    model.set_search('홍길동')
    assert model.rowCount() == 0


def test_every_column_context_menu_targets_clicked_person(app, monkeypatch):
    window = RosterWindow(_roster(), _histories())
    window._set_meal(Meal.DINNER)
    window.show()
    app.processEvents()
    histories, complaints, opened = [], [], []
    monkeypatch.setattr(window, "_show_history", lambda person, meal: histories.append((person.person_id, meal)))
    window.complaint_requested.connect(lambda *args: complaints.append(args))

    def choose_actions(menu, position):
        actions = {action.text(): action for action in menu.actions()}
        opened.append(actions)
        actions["이력보기"].trigger()
        actions["민원"].trigger()

    original_create = window._create_context_menu

    def create_menu(*args):
        menu = original_create(*args)
        menu.exec = lambda position: choose_actions(menu, position)
        return menu

    monkeypatch.setattr(window, "_create_context_menu", create_menu)
    try:
        # Exercise the real context-menu handler and indexAt mapping for every column.
        for row in range(window.table_model.rowCount()):
            person = window.table_model.person_at(row)
            for col, column in enumerate(window.table_model.columns):
                window._table.setColumnHidden(col, False)
                index = window.table_model.index(row, col)
                window._table.scrollTo(index)
                app.processEvents()
                point = window._table.visualRect(index).center()
                assert window._table.indexAt(point) == index
                window._open_context_menu(point)
                expected = (person.person_id, column.meal)
                assert histories[-1] == expected
                assert complaints[-1] == expected
                if column.meal is None:
                    expected_edit = "순서 수정" if column.key == "sequence" else "정보 수정"
                    assert expected_edit in opened[-1]
                else:
                    assert "배송담당 변경" in opened[-1]
        assert len(opened) == window.table_model.rowCount() * len(window.table_model.columns)
    finally:
        window.close()


def test_birth_month_combines_filters_and_updates_after_profile_edit(app):
    from dataclasses import replace

    roster = _roster()
    model = RosterTableModel(roster)
    model.set_birth_month(3)
    assert model.rowCount() == 1
    assert model.person_at(0).person_id == FIRST_PERSON_ID
    model.set_search("김아침")
    assert model.rowCount() == 0
    model.set_birth_month(4)
    assert model.rowCount() == 1
    model.set_search("")
    model.set_context(SERVICE_DAY, Meal.LUNCH)
    model.set_active_only(True)
    assert model.rowCount() == 0
    model.set_context(SERVICE_DAY, Meal.BREAKFAST)
    assert model.rowCount() == 1
    model.set_active_only(False)
    second = roster.persons[1]
    model.replace_roster(replace(roster, persons=(roster.persons[0], replace(
        second, profile=replace(second.profile, birth_date=None),
    ))))
    assert model.rowCount() == 0
    model.set_birth_month(None)
    assert model.rowCount() == 2
    model.set_birth_month(2)
    model.replace_roster(replace(roster, persons=(replace(
        second, profile=replace(second.profile, birth_date=date(2016, 2, 29)),
    ),), enrollments=(), assignments=(), memberships=()))
    assert model.rowCount() == 1


def test_birth_month_sidebar_dropdown_filters_below_meals(app):
    window = RosterWindow(_roster(), _histories())
    window.resize(1080, 680)
    window.show()
    app.processEvents()
    dropdown = window._birth_month_filter
    assert dropdown.currentData() is None
    assert [dropdown.itemData(index) for index in range(dropdown.count())] == [
        None, 0, *range(1, 13),
    ]
    dropdown.setCurrentIndex(dropdown.findData(3))
    assert window._model.rowCount() == 1
    assert window._model.person_at(0).person_id == FIRST_PERSON_ID
    dropdown.setCurrentIndex(dropdown.findData(4))
    assert window._model.person_at(0).person_id == SECOND_PERSON_ID
    dropdown.setCurrentIndex(dropdown.findData(0))
    assert window._model.rowCount() == sum(
        person.profile.birth_date.month == date.today().month
        for person in _roster().persons
    )
    dropdown.setCurrentIndex(0)
    assert window._model.rowCount() == 2
    assert dropdown.geometry().top() > max(
        summary.geometry().bottom()
        for summary in window._meal_summary_widgets.values()
    )
    assert dropdown.geometry().bottom() < window._count_metric.geometry().top()
    assert window._revision_label.geometry().bottom() < window.height()
    window.close()


def test_numeric_and_date_custom_columns_are_centered_including_empty_cells(app):
    from dataclasses import replace

    definitions = tuple(
        CustomFieldDefinition(UUID(int=900 + index), label, field_type, index)
        for index, (label, field_type) in enumerate((
            ("숫자", CustomFieldType.NUMBER),
            ("날짜", CustomFieldType.DATE),
            ("문자", CustomFieldType.TEXT),
        ))
    )
    roster = _roster()
    first = roster.persons[0]
    roster = replace(roster, custom_fields=definitions, persons=(replace(
        first, profile=replace(first.profile, custom_values={
            definitions[0].definition_id: 0,
            definitions[1].definition_id: date(2026, 9, 21),
            definitions[2].definition_id: "문자값",
        }),
    ), roster.persons[1]))
    model = RosterTableModel(roster)
    for row in range(model.rowCount()):
        for index, column in enumerate(model.columns):
            alignment = model.data(model.index(row, index), Qt.ItemDataRole.TextAlignmentRole)
            if column.key in {"birth_date", "grade", "sequence"} or (
                column.custom_field is not None
                and column.custom_field.field_type in {CustomFieldType.NUMBER, CustomFieldType.DATE}
            ):
                assert alignment == Qt.AlignmentFlag.AlignCenter
            elif column.key == "name" or column.custom_field is not None:
                assert alignment is None


def test_delivery_weekday_only_override_is_visible_in_special_household_summary(app):
    from dataclasses import replace
    from src.domain.roster import HouseholdDeliverySetting

    roster = _roster()
    setting = HouseholdDeliverySetting(
        Meal.BREAKFAST, delivery_weekdays=(0, 1, 2, 3, 3, 3, 3),
    )
    roster = replace(roster, households=(Household(HOUSEHOLD_ID, (setting,)),))
    window = RosterWindow(roster, _histories())
    window._set_day(SERVICE_DAY)
    window._special_only_filter.setChecked(True)
    assert window.table_model.rowCount() == 2
    column = next(i for i, item in enumerate(window.table_model.columns) if item.key == "delivery_settings")
    assert window.table_model.index(0, column).data() == "조식: 금·토·일 → 목 배송"
    window.close()


def test_individual_delivery_settings_filter_only_changed_household_member(app):
    from dataclasses import replace
    from src.domain.roster import HouseholdDeliverySetting

    roster = _roster()
    personal = (HouseholdDeliverySetting(Meal.BREAKFAST, form="개인 도시락"),)
    roster = replace(roster, persons=tuple(
        replace(person, delivery_settings=personal) if person.person_id == FIRST_PERSON_ID else person
        for person in roster.persons
    ))
    window = RosterWindow(roster, _histories())
    window._set_day(SERVICE_DAY)
    window._special_only_filter.setChecked(True)
    assert window.table_model.rowCount() == 1
    assert window.table_model.person_at(0).person_id == FIRST_PERSON_ID
    column = next(i for i, item in enumerate(window.table_model.columns) if item.key == "delivery_settings")
    assert "개인 도시락" in window.table_model.index(0, column).data()
    window.close()


def test_delivery_settings_can_open_without_linked_household(app):
    from dataclasses import replace

    roster = replace(_roster(), memberships=(), households=())
    window = RosterWindow(roster, _histories())
    window._set_day(SERVICE_DAY)
    window._set_meal(Meal.LUNCH)
    requested = []
    window.delivery_settings_requested.connect(requested.append)
    window.show()
    window._table.selectRow(0)
    app.processEvents()
    assert window._delivery_settings_button.isEnabled()
    window._delivery_settings_button.click()
    assert requested == [FIRST_PERSON_ID]
    window.close()
