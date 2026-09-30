"""Real form and controller boundaries for driver registration and dated moves."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from types import SimpleNamespace
from uuid import uuid4

import pytest
from PySide6.QtCore import QDate
from PySide6.QtWidgets import QApplication, QDialog

from src.desktop_v2.application import DesktopStartupController
from src.desktop_v2.delivery_routing import (
    DeliveryRoutingController,
    DeliveryRoutingOutcome,
    DriverRegistryDialog,
    RoutingChangeDialog,
    RoutingRequest,
)
from src.domain.delivery_routing import Driver, RoutingState
from src.domain.roster import Meal
from src.storage import DatabaseSession
from tests.test_delivery_routing import BEFORE, DAY, base_roster, initialize, loaded


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


class Messages:
    def __init__(self):
        self.criticals = []
        self.warnings = []

    def critical(self, *args):
        self.criticals.append(args)

    def warning(self, *args):
        self.warnings.append(args)


def test_registry_has_name_phone_only_and_validates_blank_name(app):
    original = Driver(uuid4(), "홍기사", "010-0000-0001")
    dialog = DriverRegistryDialog(RoutingState((original,)))
    assert dialog.table.columnCount() == 2
    assert [dialog.table.horizontalHeaderItem(i).text() for i in range(2)] == [
        "이름",
        "전화번호",
    ]
    dialog._save(False)
    assert dialog.result() != QDialog.DialogCode.Accepted
    dialog.name.setText("김기사")
    dialog.phone.setText("010-0000-0002")
    dialog._save(False)
    assert dialog.request().name == "김기사"
    assert dialog.request().telephone == "010-0000-0002"
    dialog.close()


def test_actual_relocation_form_uses_date_and_registered_uuid_for_each_meal(app):
    roster = base_roster()
    driver = Driver(uuid4(), "등록 기사", "010-0000-0001")
    dialog = RoutingChangeDialog(
        roster, RoutingState((driver,)), roster.persons[0].person_id, BEFORE
    )
    dialog.day.setDate(QDate(DAY.year, DAY.month, DAY.day))
    dialog.road.setText("이사 후 도로")
    dialog.detail.setText("505호")
    assert tuple(dialog.drivers) == (Meal.DINNER, Meal.BREAKFAST, Meal.LUNCH)
    for combo in dialog.drivers.values():
        combo.setCurrentIndex(1)
    app.processEvents()
    dialog._save()
    result = dialog.request()
    assert result.effective_on == DAY
    assert result.road_address == "이사 후 도로"
    assert result.driver_ids == {meal: driver.driver_id for meal in Meal}
    dialog.close()


def test_relocation_form_shows_effective_date_household_targets(app):
    from src.domain import Household, HouseholdMembership, InclusiveDateRange, Person, PersonProfile, RosterSnapshot

    selected = Person(uuid4(), PersonProfile("선택", road_address="이전 주소"))
    member = Person(uuid4(), PersonProfile("구성원", road_address="이전 주소"))
    household = Household(uuid4())
    roster = RosterSnapshot(
        uuid4(),
        persons=(selected, member),
        households=(household,),
        memberships=(
            HouseholdMembership(selected.person_id, household.household_id, InclusiveDateRange(DAY, None)),
            HouseholdMembership(member.person_id, household.household_id, InclusiveDateRange(DAY, None)),
        ),
    )
    dialog = RoutingChangeDialog(roster, RoutingState(), selected.person_id, BEFORE)
    assert not dialog.whole_household.isEnabled()
    dialog.day.setDate(QDate(DAY.year, DAY.month, DAY.day))
    assert dialog.whole_household.isEnabled()
    assert "선택, 구성원" in dialog.household_targets.text()
    dialog.whole_household.setChecked(True)
    dialog.road.setText("새 주소")
    dialog._save()
    assert dialog.request().whole_household is True
    dialog.close()


def test_assignment_form_changes_only_clicked_meal_and_can_unassign(app):
    roster = base_roster()
    dialog = RoutingChangeDialog(
        roster, RoutingState(), roster.persons[0].person_id, DAY, meal=Meal.DINNER
    )
    assert "기존 기사" in dialog.drivers[Meal.DINNER].currentText()
    dialog._save()
    assert dialog.result() != QDialog.DialogCode.Accepted
    dialog.drivers[Meal.DINNER].setCurrentIndex(0)
    dialog._save()
    result = dialog.request()
    assert result.driver_ids == {Meal.DINNER: None}
    assert result.road_address is None
    assert result.detail_address is None
    dialog.close()


def test_controller_registration_relocation_cancel_and_backup_warning(tmp_path):
    path = tmp_path / "workflow.sqlite3"
    roster = base_roster()
    person_id = roster.persons[0].person_id
    driver = Driver(uuid4(), "등록 기사", "010-0000-0001")
    messages = Messages()
    pending = [driver, None]

    class Registry:
        def __init__(self, state, parent):
            self.result = pending.pop(0)

        def exec(self):
            return (
                QDialog.DialogCode.Accepted
                if self.result
                else QDialog.DialogCode.Rejected
            )

        def request(self):
            return self.result

    class Move:
        def __init__(self, *args, **kwargs):
            pass

        def exec(self):
            return QDialog.DialogCode.Accepted

        def request(self):
            return RoutingRequest(
                DAY, {meal: driver.driver_id for meal in Meal}, "새 주소", "202호"
            )

    class Cancel(Move):
        def exec(self):
            return QDialog.DialogCode.Rejected

    with DatabaseSession.create(
        path, "user-password", "institution-recovery"
    ) as session:
        initialize(session, roster)
        controller = DeliveryRoutingController(
            session,
            registry_dialog_type=Registry,
            change_dialog_type=Move,
            message_box=messages,
        )
        outcome = controller.run_drivers()
        assert outcome.routing_state.drivers == (driver,)
        original_append = session.append_revision

        def append_with_backup_failure(record):
            original_append(record)
            return SimpleNamespace(error=OSError("backup unavailable"))

        session.append_revision = append_with_backup_failure
        moved = controller.run_relocation(person_id, BEFORE)
        assert (
            moved.routing_state.resolve(moved.roster, person_id, DAY).road_address
            == "새 주소"
        )
        assert (
            moved.routing_state.resolve(moved.roster, person_id, BEFORE).road_address
            == "이전 도로"
        )
        assert len(messages.warnings) == 1
        revision = moved.roster.revision_id
        cancelled = DeliveryRoutingController(
            session, change_dialog_type=Cancel, message_box=messages
        ).run_relocation(person_id, DAY)
        assert cancelled.roster is None
        assert session.database.current_roster().revision_id == revision
        assert not messages.criticals


def test_controller_invalid_driver_does_not_write(tmp_path):
    roster = base_roster()
    messages = Messages()

    class Invalid:
        def __init__(self, *args, **kwargs):
            pass

        def exec(self):
            return QDialog.DialogCode.Accepted

        def request(self):
            return RoutingRequest(DAY, {Meal.DINNER: uuid4()})

    with DatabaseSession.create(
        tmp_path / "invalid.sqlite3", "user-password", "institution-recovery"
    ) as session:
        initialize(session, roster)
        outcome = DeliveryRoutingController(
            session, change_dialog_type=Invalid, message_box=messages
        ).run_assignment(roster.persons[0].person_id, Meal.DINNER, DAY)
        assert outcome.roster is None
        assert session.database.current_roster().revision_id == roster.revision_id
        assert len(messages.criticals) == 1


def test_application_routing_actions_use_selected_day_and_refresh(monkeypatch):
    roster = base_roster()
    state = RoutingState()
    calls = []

    class Controller:
        def __init__(self, session):
            pass

        def run_assignment(self, person, meal, day, *, parent):
            calls.append((person, meal, day))
            return DeliveryRoutingOutcome(roster, state)

        def run_relocation(self, person, day, *, parent):
            calls.append((person, day))
            return DeliveryRoutingOutcome()

    app_controller = DesktopStartupController(routing_controller_type=Controller)
    refreshed = []
    window = SimpleNamespace(
        table_model=SimpleNamespace(service_day=DAY),
        set_routing_state=lambda value: refreshed.append(value),
    )
    monkeypatch.setattr(
        app_controller, "_replace_roster_if_changed", lambda *args: None
    )
    person = roster.persons[0].person_id
    app_controller._run_driver_assignment(object(), window, person, Meal.DINNER)
    app_controller._run_relocation(object(), window, person)
    assert calls == [(person, Meal.DINNER, DAY), (person, DAY)]
    assert refreshed == [state]



def test_assignment_multi_meal_shared_driver_preserves_unchecked_meal(app):
    roster = base_roster()
    driver = Driver(uuid4(), "새 기사")
    dialog = RoutingChangeDialog(
        roster, RoutingState((driver,)), roster.persons[0].person_id,
        BEFORE, meal=Meal.BREAKFAST,
    )
    dialog.day.setDate(QDate(DAY.year, DAY.month, DAY.day))
    dialog.meal_checks[Meal.DINNER].setChecked(True)
    dialog.shared_driver.setCurrentIndex(1)
    dialog.apply_shared.click()
    dialog._save()
    assert dialog.request().driver_ids == {
        Meal.BREAKFAST: driver.driver_id, Meal.DINNER: driver.driver_id,
    }
    assert dialog.request().effective_on == DAY
    assert dialog.request().road_address is None
    empty = RoutingChangeDialog(roster, RoutingState(), roster.persons[0].person_id,
                                DAY, meal=Meal.LUNCH)
    empty.meal_checks[Meal.LUNCH].setChecked(False)
    empty._save()
    assert empty.result() != QDialog.DialogCode.Accepted
    assert "하나 이상" in empty.error.text()


def test_replacement_preview_tracks_day_meals_and_same_driver(app):
    from src.desktop_v2.delivery_routing import DriverReplacementDialog
    from src.domain.delivery_routing import RoutingEvent

    roster = base_roster()
    old = Driver(uuid4(), "기존 기사")
    new = Driver(uuid4(), "새 기사")
    state = RoutingState((old, new), (RoutingEvent(
        uuid4(), roster.persons[0].person_id, DAY, 1, {Meal.LUNCH: new.driver_id},
    ),))
    dialog = DriverReplacementDialog(roster, state, BEFORE)
    dialog.source.setCurrentIndex(dialog.source.findData(old.driver_id))
    dialog.target.setCurrentIndex(dialog.target.findData(str(new.driver_id)))
    assert "3건" in dialog.summary.text()
    dialog.day.setDate(QDate(DAY.year, DAY.month, DAY.day))
    assert "2건" in dialog.summary.text()
    assert "중식" not in dialog.table.item(0, 1).text()
    dialog.meal_checks[Meal.DINNER].setChecked(False)
    assert "1건" in dialog.summary.text()
    dialog.target.setCurrentIndex(dialog.target.findData(str(old.driver_id)))
    assert dialog.table.rowCount() == 0
    dialog._save()
    assert dialog.result() != QDialog.DialogCode.Accepted
    dialog.target.setCurrentIndex(dialog.target.findData(str(new.driver_id)))
    dialog._save()
    assert dialog.request().effective_on == DAY
    assert dialog.request().meals == (Meal.BREAKFAST, Meal.LUNCH)


def test_batch_controller_real_form_saves_one_revision_and_cancel_does_not_write(app, tmp_path):
    from src.desktop_v2.delivery_routing import DriverReplacementDialog
    from src.domain.delivery_routing import build_driver_revision
    from tests.test_delivery_routing import args

    roster = base_roster()
    person = roster.persons[0].person_id
    new = Driver(uuid4(), "새 기사")
    notices = Messages()

    class Filled(DriverReplacementDialog):
        def exec(self):
            self.source.setCurrentIndex(self.source.findData("기존 기사"))
            self.target.setCurrentIndex(self.target.findData(str(new.driver_id)))
            self.meal_checks[Meal.LUNCH].setChecked(False)
            self.day.setDate(QDate(DAY.year, DAY.month, DAY.day))
            self._save()
            return self.result()

    class Cancel(Filled):
        def exec(self):
            return QDialog.DialogCode.Rejected

    path = tmp_path / "batch-ui.sqlite3"
    with DatabaseSession.create(path, "password", "recovery") as session:
        initialize(session, roster)
        session.append_revision(build_driver_revision(
            roster, RoutingState(), driver_id=new.driver_id, name=new.name,
            telephone="", **args(),
        ))
        count = len(session.database.revision_summaries())
        controller = DeliveryRoutingController(session, replacement_dialog_type=Filled,
                                               message_box=notices)
        outcome = controller.run_replacement(BEFORE)
        assert outcome.roster is not None and not notices.criticals
        assert len(session.database.revision_summaries()) == count + 1
        assert outcome.routing_state.resolve(outcome.roster, person, BEFORE).drivers[Meal.BREAKFAST] == "기존 기사"
        view = outcome.routing_state.resolve(outcome.roster, person, DAY)
        assert view.drivers[Meal.BREAKFAST] == view.drivers[Meal.DINNER] == new.name
        assert view.drivers[Meal.LUNCH] == "기존 기사"
        cancelled = DeliveryRoutingController(session, replacement_dialog_type=Cancel).run_replacement(DAY)
        assert cancelled.roster is None
        assert len(session.database.revision_summaries()) == count + 1
    with DatabaseSession.open(path, "password") as session:
        current, state = loaded(session)
        assert state.resolve(current, person, DAY).drivers[Meal.DINNER] == new.name

        from src.desktop_v2.ledger_reader import load_revision_chain
        from src.services.database_export import build_database_workbook
        from src.services.person_history import build_history_tables

        snapshot = session.database.snapshot(current.revision_id)
        records = load_revision_chain(session.database, current.revision_id)
        for day, expected in ((BEFORE, {"기존 기사"}), (DAY, {"기존 기사", "새 기사"})):
            book = build_database_workbook(current, snapshot, records, day=day, sections=("roster",))
            rows = list(book["명단"].values)
            index = next(i for i, row in enumerate(rows) if "사람 UUID" in row)
            column = rows[index].index("배송담당")
            assert {row[column] for row in rows[index + 1:]} == expected
            book.close()
        tables = build_history_tables(current, snapshot, records, person_id=person)
        assert any("배송기사 일괄 교체" in row for row in tables["식종 변경"][1])


def test_batch_controller_failure_does_not_return_success():
    from src.desktop_v2.delivery_routing import DriverReplacementRequest
    roster = base_roster()
    new = Driver(uuid4(), "새 기사")
    state = RoutingState((new,))
    attempted = []
    notices = Messages()

    class Filled:
        def __init__(self, *args):
            pass
        def exec(self):
            return QDialog.DialogCode.Accepted
        def request(self):
            return DriverReplacementRequest("기존 기사", new.driver_id, DAY, tuple(Meal))

    def fail(record):
        attempted.append(record)
        raise OSError("write failed")

    controller = DeliveryRoutingController(SimpleNamespace(append_revision=fail),
                                           replacement_dialog_type=Filled, message_box=notices)
    controller._load = lambda: (roster, state)
    outcome = controller.run_replacement(DAY)
    assert outcome.roster is None and notices.criticals
    assert len(attempted) == 1


def test_roster_button_requests_bulk_replacement_on_selected_day(app, monkeypatch):
    from src.desktop_v2.roster_window import RosterWindow

    roster = base_roster()
    window = RosterWindow(roster)
    window._set_day(DAY)
    calls = []

    class Routing:
        def __init__(self, session):
            pass
        def run_replacement(self, day, parent):
            calls.append((day, parent))
            return DeliveryRoutingOutcome()

    controller = DesktopStartupController(routing_controller_type=Routing)
    window.driver_replacement_requested.connect(
        lambda: controller._run_driver_replacement(object(), window)
    )
    window._driver_replacement_action.trigger()
    assert calls == [(DAY, window)]
    window.close()



def test_multi_meal_assignment_real_controller_preserves_before_date_and_other_meal(app, tmp_path):
    from src.domain.delivery_routing import build_driver_revision
    from tests.test_delivery_routing import args

    roster = base_roster()
    person = roster.persons[0].person_id
    new = Driver(uuid4(), "새 기사")
    notices = Messages()

    class Filled(RoutingChangeDialog):
        def exec(self):
            self.day.setDate(QDate(DAY.year, DAY.month, DAY.day))
            self.meal_checks[Meal.DINNER].setChecked(True)
            self.shared_driver.setCurrentIndex(1)
            self.apply_shared.click()
            self._save()
            return self.result()

    with DatabaseSession.create(tmp_path / "multi.sqlite3", "password", "recovery") as session:
        initialize(session, roster)
        session.append_revision(build_driver_revision(roster, RoutingState(),
            driver_id=new.driver_id, name=new.name, telephone="", **args()))
        controller = DeliveryRoutingController(session, change_dialog_type=Filled, message_box=notices)
        result = controller.run_assignment(person, Meal.BREAKFAST, BEFORE)
        assert result.roster is not None and not notices.criticals
        previous = result.routing_state.resolve(result.roster, person, BEFORE)
        current = result.routing_state.resolve(result.roster, person, DAY)
        assert set(previous.drivers.values()) == {"기존 기사"}
        assert current.drivers[Meal.BREAKFAST] == current.drivers[Meal.DINNER] == "새 기사"
        assert current.drivers[Meal.LUNCH] == "기존 기사"
        assert current.road_address == previous.road_address



def test_relocation_retains_legacy_assignments_when_only_address_changes(app):
    roster = base_roster()
    dialog = RoutingChangeDialog(roster, RoutingState(), roster.persons[0].person_id, DAY)
    dialog.road.setText("변경할 주소")
    dialog._save()
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.request().driver_ids == {}
    assert dialog.request().road_address == "변경할 주소"
