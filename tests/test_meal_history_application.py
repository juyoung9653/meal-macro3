"""Keep main and personal views synchronized after an audited meal cancellation."""
from types import SimpleNamespace
from uuid import uuid4

import pytest

import src.desktop_v2.application as application
from src.domain.roster import RosterSnapshot


@pytest.mark.parametrize("saved", [True, False])
@pytest.mark.parametrize("with_dialog", [True, False])
def test_meal_cancellation_refreshes_views_only_after_save(monkeypatch, saved, with_dialog):
    startup = object.__new__(application.DesktopStartupController)
    session, window, target = object(), object(), object()
    expected_revision_id = uuid4()
    roster = RosterSnapshot(uuid4()) if saved else None
    calls = []
    dialog = SimpleNamespace(reload_meal_history=lambda actual_session: calls.append(("history", actual_session))) if with_dialog else None

    class Controller:
        def __init__(self, actual_session):
            assert actual_session is session

        def run(self, actual_target, actual_revision, parent=None):
            assert actual_target is target
            assert actual_revision == expected_revision_id
            assert parent is (dialog if with_dialog else window)
            return roster

    monkeypatch.setattr(application, "MealHistoryCancelController", Controller)
    startup._replace_roster_if_changed = lambda actual_session, actual_window, actual_roster: calls.append(
        ("roster", actual_session, actual_window, actual_roster)
    )
    startup._run_meal_history_cancel(session, window, target, expected_revision_id, source_dialog=dialog)
    expected = [("roster", session, window, roster)] if saved else []
    if saved and with_dialog:
        expected.append(("history", session))
    assert calls == expected


def test_saved_cancellation_reports_refresh_failure_as_saved(monkeypatch):
    startup = object.__new__(application.DesktopStartupController)
    roster = RosterSnapshot(uuid4())
    refreshed, warnings = [], []
    startup._replace_roster_if_changed = lambda *args: refreshed.append(args[-1])
    startup._message_box = SimpleNamespace(warning=lambda *args: warnings.append(args))
    monkeypatch.setattr(application, "MealHistoryCancelController", lambda session: SimpleNamespace(run=lambda *args, **kwargs: roster))

    def fail_reload(session):
        raise RuntimeError("refresh failed")

    dialog = SimpleNamespace(reload_meal_history=fail_reload)
    startup._run_meal_history_cancel(object(), object(), object(), uuid4(), source_dialog=dialog)
    assert refreshed == [roster]
    assert len(warnings) == 1
    assert "취소는 저장됐지만" in warnings[0][1]
    assert "다시 열어주세요" in warnings[0][2]
