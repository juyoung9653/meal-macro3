"""Offscreen checks for the PySide6 date-spine roster shell."""

import os
from datetime import UTC, date, datetime
from uuid import UUID

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QAbstractItemView, QApplication, QDialogButtonBox

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


def test_model_filters_by_selected_meal_and_exposes_dynamic_columns(app):
    model = RosterTableModel(_roster(), _histories())
    model.set_context(SERVICE_DAY, Meal.LUNCH)

    assert model.rowCount() == 1
    assert model.data(model.index(0, 0)) == "일시중지"
    custom_column = next(
        index
        for index, column in enumerate(model.columns)
        if column.label == "배송 구역"
    )
    assert model.data(model.index(0, custom_column)) == "A구역"

    model.set_active_only(False)
    assert model.rowCount() == 2


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


def test_context_menu_has_exact_five_actions_and_emits_status_request(app):
    window = RosterWindow(_roster(), _histories())
    window._set_day(SERVICE_DAY)
    window._set_meal(Meal.LUNCH)
    person = window.table_model.person_at(0)
    requested = []
    window.status_action_requested.connect(lambda *args: requested.append(args))

    menu = window._create_context_menu(person)

    assert [action.text() for action in menu.actions()] == [
        "배송",
        "일시중지",
        "중지",
        "이력보기",
        "민원",
    ]
    menu.actions()[1].trigger()
    assert requested == [(FIRST_PERSON_ID, Meal.LUNCH, MealStatus.PAUSED)]
    window.close()


def test_unit_price_button_emits_global_management_request(app):
    window = RosterWindow(_roster(), _histories())
    requested = []
    window.unit_price_requested.connect(lambda: requested.append(True))

    window._unit_price_button.click()
    app.processEvents()

    assert requested == [True]
    window.close()


def test_service_adjustment_button_emits_vendor_schedule_request(app):
    window = RosterWindow(_roster(), _histories())
    requested = []
    window.service_adjustment_requested.connect(lambda: requested.append(True))

    window._service_adjustment_button.click()
    app.processEvents()

    assert requested == [True]
    assert "청구 제외" in window._service_adjustment_button.toolTip()
    window.close()


def test_settings_button_emits_excel_password_request(app):
    window = RosterWindow(_roster(), _histories())
    requested = []
    window.settings_requested.connect(lambda: requested.append(True))

    window._settings_button.click()
    app.processEvents()

    assert requested == [True]
    assert "Excel" in window._settings_button.toolTip()
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


def test_minimum_width_header_keeps_search_above_complete_action_row(app):
    window = RosterWindow(_roster(), _histories())
    window.resize(1080, 680)
    window.show()
    app.processEvents()

    assert window._documents_button.geometry().top() > window._search.geometry().top()
    assert (
        window._documents_button.geometry().right()
        < window._documents_button.parentWidget().width()
    )
    window.close()


def test_row_selection_populates_household_inspector(app):
    window = RosterWindow(_roster(), _histories())
    window._set_day(SERVICE_DAY)
    window._set_meal(Meal.LUNCH)
    edit_requests = []
    window.person_edit_requested.connect(edit_requests.append)
    assert not window._edit_person_button.isEnabled()
    window.show()
    window._table.selectRow(0)
    app.processEvents()

    assert window._inspector_name.text() == "홍길동"
    assert window._inspector_status.text() == "일시중지"
    assert window._household_list.count() == 2
    assert "김아침" in window._household_list.item(1).text()
    assert (
        window._table.selectionBehavior()
        is QAbstractItemView.SelectionBehavior.SelectRows
    )
    assert window._edit_person_button.isEnabled()
    window._edit_person_button.click()
    assert edit_requests == [FIRST_PERSON_ID]
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

    assert window.table_model.rowCount() == 2
    assert window._revision_label.text() == "변경번호\n20260826-002"
    names = {
        window.table_model.person_at(row).profile.name
        for row in range(window.table_model.rowCount())
    }
    assert names == {"홍길동", "나중에 추가된 아동"}
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

    assert combo.currentText() == "기존 가구 선택 필요…"
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
