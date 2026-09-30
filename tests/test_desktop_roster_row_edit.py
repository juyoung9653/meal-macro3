"""The row dialogs gate mutations until explicit valid acceptance."""

import os
from datetime import UTC, date, datetime
from uuid import UUID

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QDialog, QLabel, QPushButton
from PySide6.QtCore import QDate

from src.desktop_v2.roster_row_edit import RosterRowCreateDialog, RosterRowCreateRequest, RosterRowDeleteDialog, RosterRowEditController, RosterRowEditStatus
from src.desktop_v2.application import DesktopStartupController
from src.domain.delivery_routing import DRIVER_KIND, Driver, routing_state_from_snapshot
from src.domain.roster import CustomFieldDefinition, CustomFieldType, Meal, Person, PersonProfile
from src.domain.projections import roster_audit_changes
from src.domain.revisions import AuditChange, ChangeOperation, RevisionActor, RevisionMetadata, RevisionRecord, RevisionSource
from src.domain.roster import RosterSnapshot
from src.domain.status import MealStatus
from src.storage import DatabaseSession


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


def test_create_dialog_collects_full_profile_custom_fields_and_meal_routing(app):
    definitions = (
        CustomFieldDefinition(UUID("82000000-0000-0000-0000-000000000001"), "추가 전화", CustomFieldType.TEXT, 0),
        CustomFieldDefinition(UUID("82000000-0000-0000-0000-000000000002"), "수량", CustomFieldType.NUMBER, 1),
        CustomFieldDefinition(UUID("82000000-0000-0000-0000-000000000003"), "확인일", CustomFieldType.DATE, 2),
        CustomFieldDefinition(UUID("82000000-0000-0000-0000-000000000004"), "구분", CustomFieldType.CHOICE, 3, ("A", "B")),
    )
    first = Driver(UUID("82000000-0000-0000-0000-000000000005"), "김기사", "010-1111")
    second = Driver(UUID("82000000-0000-0000-0000-000000000006"), "김기사", "010-2222")
    dialog = RosterRowCreateDialog(Meal.LUNCH, date(2026, 10, 5), custom_fields=definitions, drivers=(first, second))
    for key, value in {
        "name": "새 대상", "birth_date": "20150302", "school_type": "초등", "school_name": "학교",
        "grade": "3", "class_name": "2", "household_head_name": "세대주", "relationship_to_child": "모",
        "jurisdiction": "정자동", "provider": "기관", "road_address": "도로 1", "detail_address": "101호",
        "telephone": "031-123", "mobile_phone": "010-123", "note": "메모",
    }.items():
        editor = dialog._editors[key]
        editor.setPlainText(value) if key == "note" else editor.setText(value)
    dialog._editors[f"custom:{definitions[0].definition_id}"].setText("010-456")
    dialog._editors[f"custom:{definitions[1].definition_id}"].setText("2.5")
    dialog._editors[f"custom:{definitions[2].definition_id}"].setText("2026-10-05")
    dialog._editors[f"custom:{definitions[3].definition_id}"].setCurrentIndex(2)
    dialog._meal_checks[Meal.DINNER].setChecked(True)
    dialog._driver_combos[Meal.DINNER].setCurrentIndex(2)
    dialog._sequence_edits[Meal.DINNER].setText("001")
    dialog._driver_combos[Meal.LUNCH].setCurrentIndex(1)
    dialog._sequence_edits[Meal.LUNCH].setText("013")
    dialog._status_combos[Meal.DINNER].setCurrentIndex(1)
    dialog._pause_end_edits[Meal.DINNER].setText("2026-10-07")
    dialog._accept_valid()
    assert dialog.result() == QDialog.DialogCode.Accepted
    request = dialog.request()
    assert request.profile.class_name == "2" and request.profile.provider == "기관"
    assert request.profile.birth_date == date(2015, 3, 2)
    assert request.profile.custom_values == {definitions[0].definition_id: "010-456", definitions[1].definition_id: 2.5, definitions[2].definition_id: date(2026, 10, 5), definitions[3].definition_id: "B"}
    assert request.assignments == {Meal.DINNER: (second.driver_id, "001"), Meal.LUNCH: (first.driver_id, "013")}
    assert request.statuses == {Meal.DINNER: (MealStatus.PAUSED, date(2026, 10, 7))}


def test_create_dialog_invalid_pause_date_remains_open(app):
    dialog = RosterRowCreateDialog(Meal.LUNCH, date(2026, 10, 5))
    dialog._editors["name"].setText("새 대상")
    dialog._status_combos[Meal.LUNCH].setCurrentIndex(1)
    dialog._pause_end_edits[Meal.LUNCH].setText("2026-10-04")
    dialog._accept_valid()
    assert dialog.result() != QDialog.DialogCode.Accepted
    assert "이후" in dialog._error.text()
    dialog.reject()


def test_create_dialog_inactive_meal_and_status_controls_preserve_values(app):
    driver = Driver(UUID("82000000-0000-0000-0000-000000000007"), "기사", "")
    dialog = RosterRowCreateDialog(Meal.LUNCH, date(2026, 10, 5), drivers=(driver,))
    dialog._editors["name"].setText("새 대상")
    dialog._driver_combos[Meal.LUNCH].setCurrentIndex(1)
    dialog._status_combos[Meal.LUNCH].setCurrentIndex(1)
    dialog._pause_end_edits[Meal.LUNCH].setText("2026-10-08")
    dialog._meal_checks[Meal.LUNCH].setChecked(False)
    assert not dialog._driver_combos[Meal.LUNCH].isEnabled()
    assert not dialog._legacy_driver_edits[Meal.LUNCH].isEnabled()
    dialog._meal_checks[Meal.LUNCH].setChecked(True)
    assert not dialog._legacy_driver_edits[Meal.LUNCH].isEnabled()
    assert dialog._pause_end_edits[Meal.LUNCH].isEnabled()
    dialog._status_combos[Meal.LUNCH].setCurrentIndex(0)
    assert not dialog._pause_end_edits[Meal.LUNCH].isEnabled()
    dialog._accept_valid()
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.request().statuses == {}
    assert dialog.request().assignments[Meal.LUNCH][0] == driver.driver_id


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
