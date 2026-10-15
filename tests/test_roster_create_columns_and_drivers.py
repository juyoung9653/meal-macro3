"""Real creation dialog fields and effective-date driver assignments."""

import os
from datetime import UTC, date, datetime
from uuid import uuid4

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QWidget

from src.desktop_v2.roster_row_edit import RosterRowCreateDialog, RosterRowEditController
from src.desktop_v2.roster_window import RosterTableModel
from src.domain.delivery_routing import DRIVER_KIND, Driver, RoutingState, routing_state_from_snapshot
from src.domain.projections import roster_audit_changes
from src.domain.revisions import AuditChange, ChangeOperation, RevisionActor, RevisionMetadata, RevisionRecord, RevisionSource
from src.domain.roster import CustomFieldDefinition, CustomFieldType, Household, HouseholdMembership, Meal, Person, PersonProfile, RosterSnapshot
from src.importing.households import HouseholdMatchCriterion, HouseholdMatchingRule, StoredHouseholdMatchingRule, household_matching_rule_audit_change
from src.storage.database_session import DatabaseSession


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_create_only_collects_available_columns_with_aliases_and_no_driver(app):
    field = CustomFieldDefinition(uuid4(), "핸드폰2", CustomFieldType.TEXT, 0)
    omitted = CustomFieldDefinition(uuid4(), "미표시 항목", CustomFieldType.TEXT, 1)
    columns = {"name": "대상자", "mobile": "연락처1", f"custom:{field.definition_id}": "핸드폰2"}
    view = RosterRowCreateDialog(custom_fields=(field, omitted), column_labels=columns)
    try:
        assert set(view._editors) == {"name", "mobile_phone", f"custom:{field.definition_id}"}
        labels = {label.text() for label in view.findChildren(QLabel)}
        assert {"대상자 *", "연락처1", "핸드폰2"} <= labels
        view._editors["name"].setText("가상 대상")
        view._editors["mobile_phone"].setText("010-1111-1111")
        view._accept_valid()
        assert view.result() == QDialog.DialogCode.Accepted
        assert view.request().assignments == {}
        assert view.request().profile.road_address is None
    finally:
        view.close()


def test_driver_choices_are_ids_and_unchecked_meals_never_submit_stale_choice(app):
    drivers = (Driver(uuid4(), "동명 기사", "010-1111"), Driver(uuid4(), "동명 기사", "010-2222"))
    view = RosterRowCreateDialog(Meal.DINNER, routing_state=RoutingState(drivers))
    try:
        view._editors["name"].setText("가상 대상")
        view._driver_combos[Meal.DINNER].setCurrentIndex(2)
        view._meal_checks[Meal.BREAKFAST].setChecked(True)
        view._driver_combos[Meal.BREAKFAST].setCurrentIndex(1)
        view._meal_checks[Meal.BREAKFAST].setChecked(False)
        assert not view._driver_combos[Meal.BREAKFAST].isEnabled()
        view._accept_valid()
        assert view.request().assignments == {Meal.DINNER: (drivers[1].driver_id, None)}
    finally:
        view.close()


def test_actual_dialog_controller_saves_profile_household_and_dated_drivers_atomically(app, tmp_path, monkeypatch):
    revision_id, new_revision, new_person = uuid4(), uuid4(), uuid4()
    household = Household(uuid4())
    existing = Person(uuid4(), PersonProfile("기존 대상", road_address="가상로 1", detail_address="101호"))
    custom = CustomFieldDefinition(uuid4(), "핸드폰2", CustomFieldType.TEXT, 0)
    roster = RosterSnapshot(revision_id, persons=(existing,), households=(household,), memberships=(HouseholdMembership(existing.person_id, household.household_id),), custom_fields=(custom,))
    drivers = (Driver(uuid4(), "기사 가"), Driver(uuid4(), "기사 나"))
    rule = StoredHouseholdMatchingRule(HouseholdMatchingRule(uuid4(), 1, (HouseholdMatchCriterion.ADDRESS,)))
    actor = RevisionActor("tester", "synthetic")
    recorded = datetime(2026, 10, 15, tzinfo=UTC)
    initial = RevisionRecord(
        RevisionMetadata(revision_id, None, recorded, None, actor, "테스트 명단", RevisionSource.UI),
        (*roster_audit_changes(None, roster), household_matching_rule_audit_change(None, rule),
         *(AuditChange(DRIVER_KIND, driver.driver_id, ChangeOperation.INSERT, None, {"name": driver.name, "telephone": driver.telephone}) for driver in drivers)),
    )
    starts_on = date(2026, 11, 1)
    parent = QWidget()
    parent.table_model = RosterTableModel(roster, column_aliases={"school": "학교 이름", "mobile": "연락처"})

    def accept_actual(view):
        labels = {label.text() for label in view.findChildren(QLabel)}
        assert {"학교 이름", "연락처"} <= labels
        assert not {"road_address", "school_type", "household_head_name"} & set(view._editors)
        view._editors["name"].setText("신규 대상")
        view._editors["address"].setText("가상로 1")
        view._editors["detail_address"].setText("101호")
        view._editors[f"custom:{custom.definition_id}"].setText("010-2222-2222")
        view._meal_checks[Meal.LUNCH].setChecked(True)
        view._driver_combos[Meal.DINNER].setCurrentIndex(1)
        view._driver_combos[Meal.LUNCH].setCurrentIndex(2)
        view._accept_valid()
        assert view.result() == QDialog.DialogCode.Accepted, view._error.text()
        return view.result()

    path = tmp_path / "operations.sqlite3"
    backups = tmp_path / "backups"
    backups.mkdir()
    try:
        with DatabaseSession.create(path, "password", "recovery", backup_directory=backups) as session:
            session.append_revision(initial)
            controller = RosterRowEditController(session, actor_provider=lambda: actor, recorded_at_provider=lambda: recorded, revision_id_factory=lambda: new_revision, person_id_factory=lambda: new_person)
            monkeypatch.setattr(RosterRowCreateDialog, "exec", lambda view: QDialog.DialogCode.Rejected)
            assert not controller.add(Meal.DINNER, parent, service_day=starts_on).completed
            assert len(session.database.revision_summaries()) == 1
            monkeypatch.setattr(RosterRowCreateDialog, "exec", accept_actual)
            outcome = controller.add(Meal.DINNER, parent, service_day=starts_on)
            assert outcome.completed and outcome.backup_result.succeeded
            assert len(session.database.revision_summaries()) == 2
        with DatabaseSession.open(path, "password") as session:
            updated = session.database.current_roster()
            profile = updated.person(new_person).profile
            assert profile.road_address == "가상로 1"
            assert profile.detail_address == "101호"
            assert profile.custom_values[custom.definition_id] == "010-2222-2222"
            assert updated.household_for(new_person, on=starts_on).household_id == household.household_id
            state = routing_state_from_snapshot(session.database.snapshot(updated.revision_id))
            assert all(value is None for value in state.resolve(updated, new_person, date(2026, 10, 31)).driver_ids.values())
            assert state.resolve(updated, new_person, starts_on).driver_ids == {Meal.DINNER: drivers[0].driver_id, Meal.BREAKFAST: None, Meal.LUNCH: drivers[1].driver_id}
            assert updated.meals_for(new_person, on=date(2026, 10, 31)) == ()
    finally:
        parent.close()
