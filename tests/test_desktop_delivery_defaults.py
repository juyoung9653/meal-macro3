
from src.storage.database_session import AutomaticBackupResult
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from dataclasses import replace
from datetime import UTC, date, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from PySide6.QtWidgets import QApplication, QDialog
from src.desktop_v2.delivery_defaults import DeliveryDefaultsController, DeliveryDefaultsDialog
from src.desktop_v2.person_edit import PersonProfileEditDialog
from src.desktop_v2.application import DesktopStartupController
from src.desktop_v2.settings_hub import SettingsAction
from src.domain.projections import roster_entity_states
from src.domain.revisions import HistoricalSnapshot, RevisionActor
from src.domain.roster import HouseholdDeliverySetting, Meal, Person, PersonProfile, RosterSnapshot, standard_delivery_defaults

@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _defaults():
    return tuple(replace(item, weekdays=(0,), delivery_weekdays=(6,) * 7) if item.meal is Meal.BREAKFAST else item for item in standard_delivery_defaults())


def test_dialog_uses_current_values_validates_and_resets(app):
    dialog = DeliveryDefaultsDialog(_defaults())
    assert dialog.settings() == _defaults()
    assert not dialog._save.isEnabled()
    dialog._days[Meal.BREAKFAST][0].setChecked(False)
    assert not dialog._save.isEnabled()
    dialog._days[Meal.BREAKFAST][1].setChecked(True)
    assert dialog._save.isEnabled()
    dialog._reset(Meal.BREAKFAST)
    assert dialog.settings() == standard_delivery_defaults()
    dialog.reject()
    assert dialog.result() == QDialog.DialogCode.Rejected
    dialog.close()


def test_person_reset_inherits_saved_defaults_and_special_filter_stays_raw(app):
    from src.desktop_v2.roster_window import RosterTableModel
    person = Person(uuid4(), PersonProfile("합성 아동"), (HouseholdDeliverySetting(Meal.BREAKFAST, (1,), delivery_weekdays=(0,) * 7),))
    roster = RosterSnapshot(uuid4(), (person,), delivery_defaults=_defaults())
    dialog = PersonProfileEditDialog(person, defaults_roster=roster, delivery_only=True)
    dialog._reset_delivery_meal(Meal.BREAKFAST)
    assert dialog._delivery_values(Meal.BREAKFAST) == ((0,), (6,) * 7, "")
    assert dialog.delivery_settings() == ()
    inherited = replace(person, delivery_settings=())
    model = RosterTableModel(replace(roster, persons=(inherited,)))
    model.set_special_only(True)
    assert model.rowCount() == 0
    dialog.close()


@pytest.mark.parametrize("mode", ("cancel", "unchanged", "save", "failure", "backup_failure"))
def test_controller_noop_failure_and_backup_result(mode):
    roster = RosterSnapshot(uuid4())
    snapshot = HistoricalSnapshot(roster.revision_id, roster_entity_states(roster))
    saved, errors, warnings = [], [], []
    database = SimpleNamespace(current_roster=lambda: roster, snapshot=lambda _: snapshot)
    def append(record):
        if mode == "failure":
            raise OSError("save failed")
        saved.append(record)
        return AutomaticBackupResult(error=OSError("backup failed") if mode == "backup_failure" else None)
    class Dialog:
        def __init__(self, current, parent):
            assert current == standard_delivery_defaults()
        def exec(self):
            return QDialog.DialogCode.Rejected if mode == "cancel" else QDialog.DialogCode.Accepted
        def settings(self):
            return standard_delivery_defaults() if mode == "unchanged" else _defaults()
    outcome = DeliveryDefaultsController(SimpleNamespace(database=database, append_revision=append), dialog_type=Dialog,
        actor_provider=lambda: RevisionActor("test", "sid"), recorded_at_provider=lambda: datetime.now(UTC),
        message_box=SimpleNamespace(critical=lambda *args: errors.append(args), warning=lambda *args: warnings.append(args))).run()
    assert len(saved) == int(mode in ("save", "backup_failure"))
    assert outcome.completed == bool(saved)
    assert len(errors) == int(mode == "failure")
    assert not warnings


def test_settings_dispatch_refreshes_only_after_save():
    session, window = object(), object()
    roster = RosterSnapshot(uuid4(), delivery_defaults=_defaults())
    class Hub:
        selected_action = SettingsAction.DELIVERY_DEFAULTS
        def __init__(self, parent):
            assert parent is window
        def exec(self):
            return QDialog.DialogCode.Accepted
    class Controller:
        def __init__(self, current):
            assert current is session
        def run(self, parent):
            assert parent is window
            return SimpleNamespace(completed=True, roster=roster)
    controller = DesktopStartupController(settings=SimpleNamespace(), settings_hub_dialog_type=Hub, delivery_defaults_controller_type=Controller)
    refreshed = []
    controller._replace_roster_if_changed = lambda *args: refreshed.append(args)
    controller._run_settings_hub(session, window)
    assert refreshed == [(session, window, roster)]
