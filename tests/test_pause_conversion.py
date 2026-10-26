"""Policy persistence and UI-to-calculation integration with synthetic records."""

import os
from datetime import UTC, date, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QDialog
from src.desktop_v2.application import DesktopStartupController
from src.desktop_v2.pause_conversion import PauseConversionController, PauseConversionDialog
from src.domain.pause_conversion import (
    PauseConversionSettings, build_pause_conversion_revision, pause_conversion_from_snapshot,
)
from src.domain.projections import roster_audit_changes, status_event_audit_change
from src.domain.revisions import RevisionActor, RevisionMetadata, RevisionRecord, RevisionSource
from src.domain.roster import InclusiveDateRange, Meal, MealEnrollment, Person, PersonProfile, RosterSnapshot
from src.domain.roster import Household, HouseholdMembership
from src.domain.status import MealStatus, StatusEvent
from src.services.database_export import _status
from src.services.sqlite_delivery_report_service import prepare_physical_delivery_data
from src.storage.database_session import DatabaseSession

STAMP = datetime(2026, 10, 26, tzinfo=UTC)
ACTOR = RevisionActor("테스트", "test-pause-conversion")
DAY = date(2026, 10, 10)


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def fail_on_unexpected_message(monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    def fail(parent, title, message, *args):
        pytest.fail(f"{title}: {message}")
    monkeypatch.setattr(QMessageBox, "critical", fail)
    monkeypatch.setattr(QMessageBox, "warning", fail)


@pytest.fixture
def session(tmp_path):
    (tmp_path / "backups").mkdir()
    session = DatabaseSession.create(tmp_path / "policy.db", "test-password",
                                     backup_directory=tmp_path / "backups")
    revision_id, person_id, household_id = uuid4(), uuid4(), uuid4()
    roster = RosterSnapshot(
        revision_id, persons=(Person(person_id, PersonProfile("합성 대상자")),),
        enrollments=(MealEnrollment(person_id, Meal.DINNER),),
        households=(Household(household_id),),
        memberships=(HouseholdMembership(person_id, household_id),),
    )
    events = tuple(
        StatusEvent(uuid4(), revision_id, person_id, Meal.DINNER, status,
                    InclusiveDateRange(day, None), STAMP)
        for status, day in ((MealStatus.STOPPED, date(2026, 10, 5)),
                            (MealStatus.DELIVERY, date(2026, 10, 20)))
    )
    session.database.append_revision(RevisionRecord(
        RevisionMetadata(revision_id=revision_id, parent_revision_id=None, recorded_at=STAMP,
                         effective_on=date(2026, 10, 1), actor=ACTOR, reason="합성 명단", source=RevisionSource.UI),
        (*roster_audit_changes(None, roster), *(status_event_audit_change(event) for event in events)),
    ))
    yield session
    session.close()


def test_saved_policy_reopens_and_keeps_historical_source(session):
    database = session.database
    roster = database.current_roster()
    person = roster.persons[0]
    source = database.snapshot(roster.revision_id)
    raw = database.status_events()
    assert pause_conversion_from_snapshot(source).same_month
    assert database.status_history(person.person_id, Meal.DINNER).status_on(DAY) is MealStatus.PAUSED
    assert build_pause_conversion_revision(source, PauseConversionSettings(),
        actor=ACTOR, recorded_at=STAMP, revision_id=uuid4()) is None
    record = build_pause_conversion_revision(source, PauseConversionSettings(False),
        actor=ACTOR, recorded_at=STAMP, revision_id=uuid4())
    result = session.append_revision(record)
    assert result.succeeded
    current = database.current_roster()
    assert database.status_events() == raw
    assert database.status_history(person.person_id, Meal.DINNER).status_on(DAY) is MealStatus.STOPPED
    histories = DesktopStartupController._histories(session, current)
    assert histories[person.person_id, Meal.DINNER].status_on(DAY) is MealStatus.STOPPED
    assert _status(source, person.person_id, Meal.DINNER, DAY, person.profile, None) == "일시중지"
    latest = database.snapshot(current.revision_id)
    assert _status(latest, person.person_id, Meal.DINNER, DAY, person.profile, None) == "미배송"
    original_report = prepare_physical_delivery_data(source, DAY, DAY, (Meal.DINNER,))
    updated_report = prepare_physical_delivery_data(latest, DAY, DAY, (Meal.DINNER,))
    assert original_report.calculation_manifest["exclusions"]["일시중지"] > 0
    assert updated_report.calculation_manifest["exclusions"]["중지"] > 0
    assert "일시중지" not in updated_report.calculation_manifest["exclusions"]
    assert original_report.shipments == updated_report.shipments
    path = session.path
    session.close()
    with_session = DatabaseSession.open(path, "test-password")
    try:
        assert with_session.database.status_history(person.person_id, Meal.DINNER).status_on(DAY) is MealStatus.STOPPED
        snapshot = with_session.database.snapshot(current.revision_id)
        assert not pause_conversion_from_snapshot(snapshot).same_month
        enable = build_pause_conversion_revision(snapshot, PauseConversionSettings(True),
            actor=ACTOR, recorded_at=STAMP, revision_id=uuid4())
        with_session.append_revision(enable)
        assert with_session.database.status_history(person.person_id, Meal.DINNER).status_on(DAY) is MealStatus.PAUSED
        assert with_session.database.status_events() == raw
    finally:
        with_session.close()


@pytest.mark.parametrize("accepted,changed", [(False, True), (True, False), (True, True)])
def test_settings_dialog_save_cancel_and_no_change(app, session, monkeypatch, accepted, changed):
    original = session.database.latest_revision_id()
    def execute(dialog):
        assert dialog.mode.count() == 2
        assert dialog.selection() == PauseConversionSettings(True)
        if changed:
            dialog.mode.setCurrentIndex(1)
        return QDialog.DialogCode.Accepted if accepted else QDialog.DialogCode.Rejected
    monkeypatch.setattr(PauseConversionDialog, "exec", execute)
    roster = PauseConversionController(session).run()
    saved = accepted and changed
    assert (roster is not None) == saved
    assert (session.database.latest_revision_id() != original) == saved
    snapshot = session.database.snapshot(session.database.latest_revision_id())
    assert pause_conversion_from_snapshot(snapshot).same_month == (not saved)


def test_settings_hub_dispatch_refreshes_roster(app, monkeypatch):
    from src.desktop_v2.settings_hub import SettingsAction
    hub = SimpleNamespace(selected_action=SettingsAction.PAUSE_CONVERSION,
                          exec=lambda: QDialog.DialogCode.Accepted)
    controller = DesktopStartupController(settings_hub_dialog_type=lambda **kwargs: hub)
    result = object()
    monkeypatch.setattr(PauseConversionController, "run", lambda self, parent=None: result)
    refreshed = []
    monkeypatch.setattr(controller, "_replace_roster_if_changed", lambda *args: refreshed.append(args))
    controller._run_settings_hub(SimpleNamespace(), None)
    assert refreshed and refreshed[0][-1] is result
