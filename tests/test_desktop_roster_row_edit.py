"""The row dialogs gate mutations until explicit valid acceptance."""

import os
from datetime import UTC, date, datetime
from uuid import UUID

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QDate, QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QPushButton, QWidget

from src.desktop_v2.roster_row_edit import RosterRowCreateDialog, RosterRowCreateRequest, RosterRowDeleteDialog, RosterRowEditController, RosterRowEditStatus
from src.desktop_v2.application import DesktopStartupController
from src.domain.delivery_routing import DRIVER_KIND, routing_state_from_snapshot
from src.domain.roster import CustomFieldDefinition, CustomFieldType, Household, HouseholdDeliverySetting, HouseholdMembership, Meal, Person, PersonProfile, standard_delivery_defaults
from src.domain.projections import roster_audit_changes
from src.domain.revisions import AuditChange, ChangeOperation, RevisionActor, RevisionMetadata, RevisionRecord, RevisionSource
from src.domain.roster import RosterSnapshot
from src.domain.status import MealStatus
from src.importing.households import (
    HouseholdMatchCriterion,
    HouseholdMatchingRule,
    StoredHouseholdMatchingRule,
    household_matching_rule_audit_change,
)
from src.storage.database_session import DatabaseSession


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_create_dialog_requires_name_and_meal(app):
    dialog = RosterRowCreateDialog(Meal.LUNCH, date(2026, 10, 5))
    assert dialog.start_date_edit.date() == QDate(2026, 10, 5)
    dialog._accept_valid()
    assert dialog.result() != QDialog.DialogCode.Accepted
    assert "성명" in dialog._error.text()
    dialog._editors["name"].setText("새 대상")
    dialog._meal_checks[Meal.LUNCH].setChecked(False)
    dialog._accept_valid()
    assert dialog.result() != QDialog.DialogCode.Accepted
    assert "식종" in dialog._error.text()
    dialog._meal_checks[Meal.DINNER].setChecked(True)
    dialog._accept_valid()
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.values() == (PersonProfile("새 대상"), (Meal.DINNER,), date(2026, 10, 5))


def test_create_dialog_collects_simplified_profile_custom_fields_and_meals(app):
    definitions = (
        CustomFieldDefinition(UUID("82000000-0000-0000-0000-000000000001"), "추가 전화", CustomFieldType.TEXT, 0),
        CustomFieldDefinition(UUID("82000000-0000-0000-0000-000000000002"), "수량", CustomFieldType.NUMBER, 1),
        CustomFieldDefinition(UUID("82000000-0000-0000-0000-000000000003"), "확인일", CustomFieldType.DATE, 2),
        CustomFieldDefinition(UUID("82000000-0000-0000-0000-000000000004"), "구분", CustomFieldType.CHOICE, 3, ("A", "B")),
    )
    dialog = RosterRowCreateDialog(Meal.LUNCH, date(2026, 10, 5), custom_fields=definitions)
    for key, value in {
        "name": "새 대상", "birth_date": "20150302", "school_name": "학교",
        "grade": "3", "jurisdiction": "정자동", "address": "도로 1",
        "detail_address": "101호",
        "telephone": "031-123-4567", "mobile_phone": "010-1234-5678", "note": "메모",
    }.items():
        editor = dialog._editors[key]
        editor.setPlainText(value) if key == "note" else editor.setText(value)
    dialog._editors[f"custom:{definitions[0].definition_id}"].setText("010-456")
    dialog._editors[f"custom:{definitions[1].definition_id}"].setText("2.5")
    dialog._editors[f"custom:{definitions[2].definition_id}"].setText("2026-10-05")
    dialog._editors[f"custom:{definitions[3].definition_id}"].setCurrentIndex(2)
    dialog._meal_checks[Meal.DINNER].setChecked(True)
    dialog._accept_valid()
    assert dialog.result() == QDialog.DialogCode.Accepted
    request = dialog.request()
    assert request.profile.class_name is None and request.profile.provider is None
    assert request.profile.birth_date == date(2015, 3, 2)
    assert request.profile.road_address == "도로 1"
    assert request.profile.detail_address == "101호"
    assert request.profile.custom_values == {definitions[0].definition_id: "010-456", definitions[1].definition_id: 2.5, definitions[2].definition_id: date(2026, 10, 5), definitions[3].definition_id: "B"}
    assert request.meals == (Meal.DINNER, Meal.LUNCH)
    assert request.assignments == {}
    assert request.household_id is None
    assert request.statuses == {}


def test_create_dialog_excludes_internal_profile_fields_and_keeps_driver_selection(app):
    dialog = RosterRowCreateDialog(Meal.LUNCH, date(2026, 10, 5))
    removed_names = {
        "newPerson_class_name", "newPerson_provider", "newPerson_household",
        "newPerson_road_address", "newPerson_school_type",
        "newPerson_household_head_name", "newPerson_relationship_to_child",
        *(f"newPerson_sequence_{meal.name}" for meal in Meal),
        *(f"newPerson_status_{meal.name}" for meal in Meal),
    }
    assert all(dialog.findChild(QWidget, name) is None for name in removed_names)
    assert all(dialog.findChild(QWidget, f"newPerson_driver_{meal.name}") is not None for meal in Meal)


def test_start_date_ignores_wheel_but_keeps_keyboard_and_calendar(app):
    dialog = RosterRowCreateDialog(Meal.LUNCH, date(2026, 10, 5))
    dialog.show()
    app.processEvents()
    editor = dialog.start_date_edit
    original = editor.date()
    wheel = QWheelEvent(
        QPointF(4, 4), QPointF(4, 4), QPoint(), QPoint(0, 120),
        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.ScrollUpdate, False,
    )
    QApplication.sendEvent(editor, wheel)
    assert editor.date() == original
    editor.setFocus()
    QTest.keyClick(editor, Qt.Key.Key_Up)
    assert editor.date() != original
    assert editor.calendarPopup()
    dialog.close()


def test_create_dialog_uses_per_meal_actual_delivery_dates_and_earliest_household_match(app):
    matched_on = []
    dialog = RosterRowCreateDialog(
        Meal.BREAKFAST,
        date(2026, 10, 7),
        household_matcher=lambda _profile, starts_on: matched_on.append(starts_on),
    )
    dialog._editors["name"].setText("식종별 시작")
    dialog._meal_checks[Meal.DINNER].setChecked(True)
    assert dialog._start_date_edits[Meal.DINNER].date() == QDate(2026, 10, 7)
    assert dialog._start_date_edits[Meal.BREAKFAST].date() == QDate(2026, 10, 9)
    dialog._start_date_edits[Meal.DINNER].setDate(QDate(2026, 10, 8))

    dialog._accept_valid()

    assert dialog.result() == QDialog.DialogCode.Accepted
    assert matched_on == [date(2026, 10, 8)]
    assert dialog.request().meal_starts_on == {
        Meal.DINNER: date(2026, 10, 8),
        Meal.BREAKFAST: date(2026, 10, 9),
    }
    assert dialog.request().starts_on == date(2026, 10, 8)


def test_create_dialog_rejects_invalid_manual_meal_date_and_uses_reconfigured_mapping(app):
    defaults = tuple(
        HouseholdDeliverySetting(
            item.meal,
            (5, 6) if item.meal is Meal.BREAKFAST else item.weekdays,
            delivery_weekdays=(0, 1, 2, 3, 4, 0, 0)
            if item.meal is Meal.BREAKFAST
            else item.delivery_weekdays,
        )
        for item in standard_delivery_defaults()
    )
    dialog = RosterRowCreateDialog(
        Meal.BREAKFAST,
        date(2026, 10, 7),
        delivery_defaults=defaults,
    )
    dialog._editors["name"].setText("재설정 요일")
    assert dialog.start_date_edit.date() == QDate(2026, 10, 12)
    dialog.start_date_edit.setDate(QDate(2026, 10, 8))

    dialog._accept_valid()

    assert dialog.result() != QDialog.DialogCode.Accepted
    assert "월요일" in dialog._error.text()


def test_create_dialog_keeps_typed_values_open_when_household_match_is_ambiguous(app):
    def ambiguous(_profile, _starts_on):
        raise ValueError("입력한 정보와 일치하는 기존 가구가 둘 이상입니다.")

    household_id = UUID("82000000-0000-0000-0000-000000000008")
    dialog = RosterRowCreateDialog(
        Meal.LUNCH,
        date(2026, 10, 5),
        household_matcher=ambiguous,
    )
    dialog._editors["name"].setText("입력 유지 대상")
    dialog._editors["mobile_phone"].setText("010-1234-5678")
    dialog._accept_valid()
    assert dialog.result() != QDialog.DialogCode.Accepted
    assert "기존 가구가 둘 이상" in dialog._error.text()
    assert dialog._editors["name"].text() == "입력 유지 대상"

    dialog._household_matcher = lambda _profile, _starts_on: household_id
    dialog._accept_valid()
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.request().household_id == household_id


def test_delete_dialog_has_explicit_delete_and_cancel(app):
    from uuid import uuid4

    dialog = RosterRowDeleteDialog(Person(uuid4(), PersonProfile("삭제 대상")))
    labels = {button.text() for button in dialog.findChildren(QPushButton)}
    assert labels == {"삭제", "취소"}
    assert any("모든 식종" in label.text() for label in dialog.findChildren(QLabel))
    dialog.reject()
    assert dialog.result() != QDialog.DialogCode.Accepted


def test_controller_cancel_does_not_create_revision_or_backup(app):
    class CancelDialog:
        def __init__(self, *args):
            pass

        def exec(self):
            return QDialog.DialogCode.Rejected

    class Database:
        def __init__(self):
            from uuid import uuid4
            from src.domain.roster import RosterSnapshot
            self.roster = RosterSnapshot(uuid4(), persons=(Person(uuid4(), PersonProfile("대상")),))

        def current_roster(self):
            return self.roster

    class Session:
        database = Database()

        def append_revision(self, record):
            raise AssertionError("취소 시 revision이나 백업을 만들면 안 됩니다.")

    session = Session()
    controller = RosterRowEditController(session, dialog_type=CancelDialog, delete_dialog_type=CancelDialog)
    assert controller.add(Meal.LUNCH).status is RosterRowEditStatus.CANCELLED
    person_id = session.database.current_roster().persons[0].person_id
    assert controller.delete(person_id).status is RosterRowEditStatus.CANCELLED


def test_controller_auto_links_one_household_using_saved_rule_in_same_revision(app, tmp_path):
    initial_id = UUID("8b000000-0000-0000-0000-000000000001")
    existing_id = UUID("8b000000-0000-0000-0000-000000000002")
    household_id = UUID("8b000000-0000-0000-0000-000000000003")
    added_id = UUID("8b000000-0000-0000-0000-000000000004")
    revision_id = UUID("8b000000-0000-0000-0000-000000000005")
    actor = RevisionActor("tester", "test")
    recorded_at = datetime(2026, 10, 5, tzinfo=UTC)
    original = RosterSnapshot(
        initial_id,
        persons=(Person(existing_id, PersonProfile(
            "기존",
            mobile_phone="010-9999-9999",
            household_head_name="홍 보호자",
            road_address="성남대로 1",
            detail_address="101호",
        )),),
        households=(Household(household_id),),
        memberships=(HouseholdMembership(existing_id, household_id),),
    )
    stored_rule = StoredHouseholdMatchingRule(HouseholdMatchingRule(
        UUID("8b000000-0000-0000-0000-000000000006"),
        1,
        (HouseholdMatchCriterion.HOUSEHOLD_HEAD_NAME, HouseholdMatchCriterion.ADDRESS),
    ))
    initial = RevisionRecord(
        RevisionMetadata(initial_id, None, recorded_at, date(2026, 10, 5), actor, "초기 명단", RevisionSource.UI),
        (*roster_audit_changes(None, original), household_matching_rule_audit_change(None, stored_rule)),
    )

    class AcceptCreate:
        def __init__(self, *args):
            pass

        def exec(self):
            return QDialog.DialogCode.Accepted

        def request(self):
            return RosterRowCreateRequest(
                PersonProfile(
                    "신규",
                    mobile_phone="010-0000-0000",
                    household_head_name="  홍 보호자 ",
                    road_address="성남대로 1",
                    detail_address="101호",
                ),
                (Meal.LUNCH,), date(2026, 10, 5), {}, None, {},
            )

    class FailOnMessage:
        @staticmethod
        def warning(_parent, title, message):
            raise AssertionError(f"unexpected warning: {title}: {message}")

        @staticmethod
        def critical(_parent, title, message):
            raise AssertionError(f"unexpected failure: {title}: {message}")

    (tmp_path / "backups").mkdir()
    with DatabaseSession.create(tmp_path / "matching.sqlite3", "password", "recovery", backup_directory=tmp_path / "backups") as session:
        session.append_revision(initial)
        before = len(session.database.revision_summaries())
        outcome = RosterRowEditController(
            session,
            dialog_type=AcceptCreate,
            message_box=FailOnMessage,
            actor_provider=lambda: actor,
            recorded_at_provider=lambda: recorded_at,
            effective_date_provider=lambda: date(2026, 10, 5),
            revision_id_factory=lambda: revision_id,
            person_id_factory=lambda: added_id,
        ).add(Meal.LUNCH, service_day=date(2026, 10, 5))
        assert outcome.completed and outcome.backup_result.succeeded
        assert len(session.database.revision_summaries()) == before + 1
        assert any(
            item.person_id == added_id and item.household_id == household_id
            for item in session.database.current_roster().memberships
        )


def test_controller_ambiguous_household_reports_error_without_saving(app):
    household_ids = (UUID(int=101), UUID(int=102))
    persons = (
        Person(UUID(int=201), PersonProfile("첫째", mobile_phone="010-1234-5678")),
        Person(UUID(int=202), PersonProfile("둘째", mobile_phone="01012345678")),
    )
    roster = RosterSnapshot(
        UUID(int=301),
        persons=persons,
        households=tuple(Household(item) for item in household_ids),
        memberships=tuple(
            HouseholdMembership(person.person_id, household_id)
            for person, household_id in zip(persons, household_ids, strict=True)
        ),
    )

    class AcceptCreate:
        def __init__(self, *args):
            pass

        def exec(self):
            return QDialog.DialogCode.Accepted

        def request(self):
            return RosterRowCreateRequest(
                PersonProfile("신규", mobile_phone="010-1234-5678"),
                (Meal.LUNCH,), date(2026, 10, 5), {}, None, {},
            )

    class Database:
        def current_roster(self):
            return roster

        def snapshot(self, _revision_id):
            return None

    class Session:
        database = Database()

        def append_revision(self, _record):
            raise AssertionError("모호한 가구 연결은 저장하면 안 됩니다.")

    errors = []

    class Messages:
        @staticmethod
        def critical(_parent, title, message):
            errors.append((title, message))

    outcome = RosterRowEditController(
        Session(), dialog_type=AcceptCreate, message_box=Messages,
    ).add(Meal.LUNCH, service_day=date(2026, 10, 5))
    assert outcome.status is RosterRowEditStatus.FAILED
    assert "기존 가구가 둘 이상" in errors[0][1]


def test_controller_add_and_delete_commit_revisions_and_backups(app, tmp_path):
    initial_id = UUID("8a000000-0000-0000-0000-000000000001")
    person_id = UUID("8a000000-0000-0000-0000-000000000002")
    added_id = UUID("8a000000-0000-0000-0000-000000000003")
    add_revision_id = UUID("8a000000-0000-0000-0000-000000000004")
    delete_revision_id = UUID("8a000000-0000-0000-0000-000000000005")
    recorded_at = datetime(2026, 9, 29, tzinfo=UTC)
    actor = RevisionActor("tester", "test")
    initial_roster = RosterSnapshot(initial_id, persons=(Person(person_id, PersonProfile("기존 대상")),))
    initial = RevisionRecord(
        RevisionMetadata(initial_id, None, recorded_at, date(2026, 9, 29), actor, "초기 명단", RevisionSource.UI),
        roster_audit_changes(None, initial_roster),
    )

    class AcceptCreate:
        def __init__(self, *args):
            pass

        def exec(self):
            return QDialog.DialogCode.Accepted

        def values(self):
            return PersonProfile("신규 대상"), (Meal.LUNCH,), date(2026, 10, 5)

    class AcceptDelete:
        def __init__(self, *args):
            pass

        def exec(self):
            return QDialog.DialogCode.Accepted

    class FailOnMessage:
        @staticmethod
        def warning(_parent, title, message):
            raise AssertionError(f"unexpected warning: {title}: {message}")

        @staticmethod
        def critical(_parent, title, message):
            raise AssertionError(f"unexpected failure: {title}: {message}")

    revision_ids = iter((add_revision_id, delete_revision_id))
    (tmp_path / "backups").mkdir()
    with DatabaseSession.create(tmp_path / "roster.sqlite3", "password", "recovery", backup_directory=tmp_path / "backups") as session:
        session.append_revision(initial)
        controller = RosterRowEditController(
            session, dialog_type=AcceptCreate, delete_dialog_type=AcceptDelete,
            message_box=FailOnMessage,
            actor_provider=lambda: actor, recorded_at_provider=lambda: recorded_at,
            effective_date_provider=lambda: date(2026, 9, 29), revision_id_factory=lambda: next(revision_ids),
            person_id_factory=lambda: added_id,
        )
        added = controller.add(Meal.LUNCH, service_day=date(2026, 10, 5))
        assert added.completed
        assert added.backup_result is not None and added.backup_result.succeeded
        assert session.database.current_roster().person(added_id).profile.name == "신규 대상"
        assert session.database.current_roster().meals_for(added_id, on=date(2026, 10, 4)) == ()
        assert session.database.current_roster().meals_for(added_id, on=date(2026, 10, 5)) == (Meal.LUNCH,)
        assert session.database.current_roster().meals_for(added_id, on=date(2026, 10, 6)) == (Meal.LUNCH,)
        deleted = controller.delete(added_id)
        assert deleted.completed
        assert deleted.backup_result is not None and deleted.backup_result.succeeded
        assert session.database.current_roster().persons == initial_roster.persons
        assert session.database.snapshot(add_revision_id).entity("roster.person", added_id) is not None


def test_controller_commits_full_request_in_one_revision(app, tmp_path):
    initial_id = UUID("83000000-0000-0000-0000-000000000001")
    new_id = UUID("83000000-0000-0000-0000-000000000002")
    revision_id = UUID("83000000-0000-0000-0000-000000000003")
    driver_id = UUID("83000000-0000-0000-0000-000000000004")
    actor = RevisionActor("tester", "test")
    recorded_at = datetime(2026, 9, 29, tzinfo=UTC)
    field = CustomFieldDefinition(UUID("83000000-0000-0000-0000-000000000005"), "추가 전화", CustomFieldType.TEXT, 0)
    original = RosterSnapshot(initial_id, custom_fields=(field,))
    initial = RevisionRecord(
        RevisionMetadata(initial_id, None, recorded_at, date(2026, 9, 29), actor, "초기 명단", RevisionSource.UI),
        (*roster_audit_changes(None, original), AuditChange(DRIVER_KIND, driver_id, ChangeOperation.INSERT, None, {"name": "기사", "telephone": "010-1"})),
    )

    class AcceptFull:
        def __init__(self, *args):
            pass

        def exec(self):
            return QDialog.DialogCode.Accepted

        def request(self):
            return RosterRowCreateRequest(
                PersonProfile("새 대상", class_name="2", provider="기관", custom_values={field.definition_id: "010-2"}),
                (Meal.DINNER, Meal.LUNCH), date(2026, 10, 5),
                {Meal.DINNER: (driver_id, "001"), Meal.LUNCH: ("미등록 기사", "002")}, None,
                {Meal.DINNER: (MealStatus.STOPPED, None)},
            )

    class FailOnMessage:
        @staticmethod
        def warning(_parent, title, message):
            raise AssertionError(f"unexpected warning: {title}: {message}")

        @staticmethod
        def critical(_parent, title, message):
            raise AssertionError(f"unexpected failure: {title}: {message}")

    (tmp_path / "backups").mkdir()
    with DatabaseSession.create(tmp_path / "full.sqlite3", "password", "recovery", backup_directory=tmp_path / "backups") as session:
        session.append_revision(initial)
        before = len(session.database.revision_summaries())
        controller = RosterRowEditController(
            session, dialog_type=AcceptFull, message_box=FailOnMessage, actor_provider=lambda: actor,
            recorded_at_provider=lambda: recorded_at, effective_date_provider=lambda: date(2026, 9, 29),
            revision_id_factory=lambda: revision_id, person_id_factory=lambda: new_id,
        )
        outcome = controller.add(Meal.LUNCH, service_day=date(2026, 10, 5))
        assert outcome.completed and outcome.backup_result.succeeded
        assert len(session.database.revision_summaries()) == before + 1
        roster = session.database.current_roster()
        assert roster.person(new_id).profile.custom_values[field.definition_id] == "010-2"
        assert roster.resolve_assignment(new_id, Meal.DINNER).sequence == "001"
        assert roster.resolve_assignment(new_id, Meal.LUNCH).driver == "미등록 기사"
        assert routing_state_from_snapshot(session.database.snapshot(revision_id)).resolve(roster, new_id, date(2026, 10, 5)).driver_ids[Meal.DINNER] == driver_id
        assert session.database.status_events(person_id=new_id, meal=Meal.DINNER)[0].status is MealStatus.STOPPED

        class Window:
            def __init__(self):
                self.routing = None

            def replace_roster(self, *_args, **_kwargs):
                pass

            def set_routing_state(self, state):
                self.routing = state

        window = Window()
        startup = DesktopStartupController.__new__(DesktopStartupController)
        startup._apply_roster_row_outcome(session, window, outcome)
        assert window.routing.resolve(roster, new_id, date(2026, 10, 5)).driver_ids[Meal.DINNER] == driver_id
