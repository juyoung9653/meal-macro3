"""Stopped meal foregrounds follow effective intervals and the selected roster day."""

from __future__ import annotations

import os
from datetime import UTC, date, datetime
from uuid import UUID

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QApplication, QDialog

from src.desktop_v2.roster_window import RosterTableModel, RosterWindow
from src.desktop_v2.stopped_highlight import (
    StoppedHighlightDialog,
    StoppedHighlightMode,
    StoppedHighlightPreference,
    load_preference,
)
from src.desktop_v2.theme import NOT_ENROLLED, STOPPED
from src.domain.roster import InclusiveDateRange, Meal, MealEnrollment, Person, PersonProfile, RosterSnapshot
from src.domain.status import MealStatus, StatusEvent, resolve_status_history


PERSON_ID = UUID("a0000000-0000-0000-0000-000000000001")
REVISION_ID = UUID("a0000000-0000-0000-0000-000000000002")


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _roster():
    return RosterSnapshot(
        REVISION_ID,
        persons=(Person(PERSON_ID, PersonProfile("가상 대상자")),),
        enrollments=(MealEnrollment(PERSON_ID, Meal.BREAKFAST),),
    )


def _histories(*periods):
    events = tuple(
        StatusEvent(
            UUID(int=index + 1), REVISION_ID, PERSON_ID, Meal.BREAKFAST,
            MealStatus.STOPPED, InclusiveDateRange(*period),
            datetime(2026, 1, 1, tzinfo=UTC),
        )
        for index, period in enumerate(periods)
    )
    return {(PERSON_ID, Meal.BREAKFAST): resolve_status_history(PERSON_ID, Meal.BREAKFAST, events)}


def _color(model, day):
    model.set_context(day, Meal.BREAKFAST)
    column = next(i for i, item in enumerate(model.columns) if item.meal is Meal.BREAKFAST)
    return model.data(model.index(0, column), Qt.ItemDataRole.ForegroundRole).name().upper(), column


def test_month_and_year_rollover_and_separate_stopped_episodes(app):
    model = RosterTableModel(
        _roster(),
        _histories((date(2025, 12, 30), date(2026, 1, 2)),
                   (date(2026, 1, 5), None)),
    )
    assert _color(model, date(2025, 12, 31))[0] == STOPPED.upper()
    assert _color(model, date(2026, 1, 1))[0] == NOT_ENROLLED.upper()
    assert _color(model, date(2026, 1, 5))[0] == STOPPED.upper()
    assert _color(model, date(2026, 2, 1))[0] == NOT_ENROLLED.upper()
    assert model.status_counts()[MealStatus.STOPPED.value] == 1


def test_adjacent_stopped_events_keep_first_effective_start(app):
    model = RosterTableModel(
        _roster(),
        _histories((date(2026, 3, 1), date(2026, 3, 3)),
                   (date(2026, 3, 4), None)),
    )
    model.set_stopped_highlight(StoppedHighlightPreference(StoppedHighlightMode.DAYS, 3))
    assert _color(model, date(2026, 3, 4))[0] == NOT_ENROLLED.upper()


def test_day_and_n_days_include_start_and_turn_gray_on_next_day(app):
    start = date(2026, 1, 31)
    model = RosterTableModel(_roster(), _histories((start, None)))
    model.set_stopped_highlight(StoppedHighlightPreference(StoppedHighlightMode.DAY))
    assert _color(model, start)[0] == STOPPED.upper()
    assert _color(model, date(2026, 2, 1))[0] == NOT_ENROLLED.upper()
    model.set_stopped_highlight(StoppedHighlightPreference(StoppedHighlightMode.DAYS, 1))
    assert _color(model, start)[0] == STOPPED.upper()
    assert _color(model, date(2026, 2, 1))[0] == NOT_ENROLLED.upper()
    model.set_stopped_highlight(StoppedHighlightPreference(StoppedHighlightMode.DAYS, 3))
    assert _color(model, date(2026, 2, 2))[0] == STOPPED.upper()
    assert _color(model, date(2026, 2, 3))[0] == NOT_ENROLLED.upper()


def test_unknown_effective_start_is_gray(app):
    model = RosterTableModel(_roster(), _histories((None, date(2026, 4, 20))))
    color, column = _color(model, date(2026, 4, 1))
    assert color == NOT_ENROLLED.upper()
    assert model.data(model.index(0, column), model.StatusRole) == MealStatus.STOPPED.value


def test_setting_updates_foreground_without_reset_or_changing_status(app):
    model = RosterTableModel(_roster(), _histories((date(2026, 3, 1), None)))
    assert _color(model, date(2026, 3, 10))[0] == STOPPED.upper()
    before = model.status_counts()
    resets = []
    changed = []
    model.modelReset.connect(lambda: resets.append(True))
    model.dataChanged.connect(lambda *_: changed.append(True))
    model.set_stopped_highlight(StoppedHighlightPreference(StoppedHighlightMode.DAY))
    assert changed and not resets
    assert _color(model, date(2026, 3, 10))[0] == NOT_ENROLLED.upper()
    assert model.status_counts() == before


def test_dialog_save_reopen_scope_cancel_and_invalid_fallback(app, tmp_path, monkeypatch):
    import src.desktop_v2.roster_window as roster_module

    store = QSettings(str(tmp_path / "view.ini"), QSettings.Format.IniFormat)
    roster = _roster()
    histories = _histories((date(2026, 3, 1), None))
    first = RosterWindow(roster, histories, column_settings=store, view_settings_scope="file-a")
    first.table_model.set_context(date(2026, 3, 10), Meal.BREAKFAST)
    assert _color(first.table_model, date(2026, 3, 10))[0] == STOPPED.upper()

    class SaveDialog:
        def __init__(self, current, parent=None):
            assert current.mode is StoppedHighlightMode.MONTH

        def exec(self):
            return QDialog.DialogCode.Accepted

        def selected_preference(self):
            return StoppedHighlightPreference(StoppedHighlightMode.DAYS, 3)

    monkeypatch.setattr(roster_module, "StoppedHighlightDialog", SaveDialog)
    resets = []
    first.table_model.modelReset.connect(lambda: resets.append(True))
    first.open_stopped_highlight_settings()
    assert not resets
    assert _color(first.table_model, date(2026, 3, 10))[0] == NOT_ENROLLED.upper()

    reopened = RosterWindow(roster, histories, column_settings=store, view_settings_scope="file-a")
    other = RosterWindow(roster, histories, column_settings=store, view_settings_scope="file-b")
    assert reopened._stopped_highlight == StoppedHighlightPreference(StoppedHighlightMode.DAYS, 3)
    assert other._stopped_highlight == StoppedHighlightPreference()
    assert reopened._stopped_highlight_key != other._stopped_highlight_key

    class CancelDialog(SaveDialog):
        def __init__(self, current, parent=None):
            pass

        def exec(self):
            return QDialog.DialogCode.Rejected

    monkeypatch.setattr(roster_module, "StoppedHighlightDialog", CancelDialog)
    reopened.open_stopped_highlight_settings()
    assert load_preference(store, reopened._stopped_highlight_key).days == 3
    store.setValue(other._stopped_highlight_key, '{"mode": "days", "days": 0}')
    assert load_preference(store, other._stopped_highlight_key) == StoppedHighlightPreference()
    first.close()
    reopened.close()
    other.close()


def test_standalone_window_does_not_write_global_preference(app, monkeypatch):
    import src.desktop_v2.roster_window as roster_module

    class SaveDialog:
        def __init__(self, current, parent=None):
            pass

        def exec(self):
            return QDialog.DialogCode.Accepted

        def selected_preference(self):
            return StoppedHighlightPreference(StoppedHighlightMode.DAY)

    monkeypatch.setattr(roster_module, "StoppedHighlightDialog", SaveDialog)
    window = RosterWindow(_roster())
    assert window._stopped_highlight_key is None
    window.open_stopped_highlight_settings()
    assert window._stopped_highlight.mode is StoppedHighlightMode.DAY
    window.close()


def test_dialog_selection_and_settings_hub_route(app):
    from src.desktop_v2.application import DesktopStartupController
    from src.desktop_v2.settings_hub import SettingsAction

    dialog = StoppedHighlightDialog(StoppedHighlightPreference())
    dialog.mode.setCurrentIndex(dialog.mode.findData(StoppedHighlightMode.DAYS))
    dialog.days.setValue(1)
    assert dialog.days.isEnabled()
    assert dialog.selected_preference() == StoppedHighlightPreference(StoppedHighlightMode.DAYS, 1)
    dialog.mode.setCurrentIndex(dialog.mode.findData(StoppedHighlightMode.DAY))
    assert not dialog.days.isEnabled()
    dialog.close()

    class Hub:
        selected_action = SettingsAction.STOPPED_HIGHLIGHT

        def __init__(self, parent=None):
            pass

        def exec(self):
            return QDialog.DialogCode.Accepted

    class Window:
        opened = False

        def open_stopped_highlight_settings(self):
            self.opened = True

    window = Window()
    controller = DesktopStartupController(settings=object(), settings_hub_dialog_type=Hub)
    controller._run_settings_hub(object(), window)
    assert window.opened
