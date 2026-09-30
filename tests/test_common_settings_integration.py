"""Common settings must reach both the live roster and historical documents."""

from dataclasses import replace
from datetime import date
from types import SimpleNamespace
from uuid import uuid4

import pytest
from PySide6.QtWidgets import QApplication, QDialog

from src.desktop_v2.application import DesktopStartupController
from src.desktop_v2.roster_window import RosterWindow
from src.desktop_v2.settings_hub import SettingsAction
from src.domain.projections import roster_entity_states, roster_from_historical_snapshot
from src.domain.provider_settings import PROVIDER_SETTING_ID, PROVIDER_SETTING_KIND
from src.domain.revisions import EntityRef, HistoricalSnapshot
from src.domain.roster import Meal, MealEnrollment
from src.domain.school_calendar import (
    SCHOOL_LUNCH_SCHEDULE_KIND,
    school_lunch_school_schedule_state,
)
from src.services.sqlite_document_service import prepare_monthly_document_data
from tests.test_desktop_v2_roster import SERVICE_DAY, _calendar, _histories, _roster
from tests.test_sqlite_document_service import _source_snapshot


@pytest.mark.parametrize("provider", [None, "공통센터", ""])
def test_documents_use_shared_provider_for_every_row_from_selected_snapshot(provider):
    source = _source_snapshot()
    roster = roster_from_historical_snapshot(source)
    other = replace(
        roster.persons[0],
        person_id=uuid4(),
        profile=replace(roster.persons[0].profile, name="둘째", provider="다른센터"),
    )
    roster = replace(
        roster,
        persons=(*roster.persons, other),
        memberships=(
            *roster.memberships,
            replace(roster.memberships[0], person_id=other.person_id),
        ),
        enrollments=(
            *roster.enrollments,
            MealEnrollment(other.person_id, Meal.BREAKFAST),
        ),
    )
    states = dict(source.entities)
    states.update(roster_entity_states(roster))
    if provider is not None:
        states[EntityRef(PROVIDER_SETTING_KIND, PROVIDER_SETTING_ID)] = {
            "provider": provider
        }
    selected = HistoricalSnapshot(uuid4(), states)
    prepared = prepare_monthly_document_data(
        selected, date(2026, 8, 1), date(2026, 8, 31), (Meal.BREAKFAST,)
    )
    assert len(prepared.batch.deliveries) == 2
    assert [d.roster.extra["제공기관"] for d in prepared.batch.deliveries] == [
        provider or ""
    ] * 2
    # Preparing a newer view cannot overwrite the old immutable document source.
    original = prepare_monthly_document_data(
        source, date(2026, 8, 1), date(2026, 8, 31), (Meal.BREAKFAST,)
    )
    assert original.batch.deliveries[0].roster.extra["제공기관"] == "센터"


def test_settings_provider_action_refreshes_the_saved_roster(monkeypatch):
    roster = _roster()
    session, window = object(), object()

    class Hub:
        selected_action = SettingsAction.PROVIDER

        def __init__(self, parent):
            assert parent is window

        def exec(self):
            return QDialog.DialogCode.Accepted

    class Provider:
        def __init__(self, actual):
            assert actual is session

        def run(self, *, parent):
            assert parent is window
            return SimpleNamespace(completed=True, roster=roster)

    controller = DesktopStartupController(
        settings_hub_dialog_type=Hub, provider_settings_controller_type=Provider
    )
    calls = []
    monkeypatch.setattr(
        controller, "_replace_roster_if_changed", lambda *args: calls.append(args)
    )
    controller._run_settings_hub(session, window)
    assert calls == [(session, window, roster)]


def test_calendar_saved_in_new_revision_refreshes_lunch_and_removal_resets_it(
    monkeypatch,
):
    app = QApplication.instance() or QApplication([])
    roster = _roster()
    window = RosterWindow(roster, _histories())
    window._set_day(SERVICE_DAY)
    window._set_meal(Meal.LUNCH)

    def status():
        model = window.table_model
        col = next(i for i, c in enumerate(model.columns) if c.key == "status:lunch")
        return model.data(model.index(0, col))

    controller = DesktopStartupController()
    monkeypatch.setattr(controller, "_histories", lambda *_: _histories())
    monkeypatch.setattr(controller, "_revision_display_code", lambda *_: "변경 2")
    schedule = _calendar().schedules[0]
    # Calendar entities use the normalized school key as their identifier.
    from src.domain.school_calendar import school_lunch_schedule_entity_id

    state = {
        EntityRef(
            SCHOOL_LUNCH_SCHEDULE_KIND,
            school_lunch_schedule_entity_id(schedule.school_name),
        ): school_lunch_school_schedule_state(schedule)
    }
    current = [HistoricalSnapshot(uuid4(), state)]
    session = SimpleNamespace(database=SimpleNamespace(snapshot=lambda _: current[0]))
    try:
        assert status() == "기사A\n-"
        controller._replace_roster_if_changed(
            session, window, replace(roster, revision_id=current[0].revision_id)
        )
        app.processEvents()
        assert status() == "기사A\n일시중지"
        current[0] = HistoricalSnapshot(uuid4(), {})
        controller._replace_roster_if_changed(
            session, window, replace(roster, revision_id=current[0].revision_id)
        )
        assert status() == "기사A\n-"
    finally:
        window.close()


def test_legacy_custom_provider_cannot_override_common_document_setting():
    from src.domain.roster import CustomFieldDefinition, CustomFieldType
    from src.services.sqlite_document_service import _roster_entry

    roster = _roster()
    definition = CustomFieldDefinition(uuid4(), "제공기관", CustomFieldType.TEXT, 99)
    person = replace(
        roster.persons[0],
        profile=replace(
            roster.persons[0].profile,
            custom_values={definition.definition_id: "개인별 기관"},
        ),
    )
    roster = replace(roster, custom_fields=(*roster.custom_fields, definition))
    assert _roster_entry(person, roster, 2, "공통센터").extra["제공기관"] == "공통센터"
    assert _roster_entry(person, roster, 2, "").extra["제공기관"] == ""
