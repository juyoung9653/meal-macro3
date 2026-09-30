"""History tabs and real roster signals remain connected to the selected person."""
from datetime import datetime
from types import SimpleNamespace

from PySide6.QtWidgets import QApplication, QDialog, QTableView

from src.desktop_v2.application import DesktopStartupController
from src.desktop_v2.person_history import PersonHistoryDialog
from src.desktop_v2.roster_window import RosterWindow
from src.domain.roster import Meal
from tests.test_desktop_v2_roster import SERVICE_DAY, _roster


def test_history_tabs_show_rows_and_existing_period_view():
    app = QApplication.instance() or QApplication([])
    app.processEvents()
    person = _roster().persons[0]
    tables = {
        name: (("시각", "내용"), ((datetime(2026, 9, 21, 10), name + " 기록"),))  # noqa: DTZ001 - presentation rows are KST-naive
        for name in ("정보 변경", "식종 변경", "민원")
    }
    dialog = PersonHistoryDialog(person, tables, {}, Meal.BREAKFAST)
    assert [dialog.tabs.tabText(i) for i in range(dialog.tabs.count())] == [
        "정보 변경", "식종 변경", "민원", "상태 적용 기간",
    ]
    for name in tables:
        table = dialog.findChild(QTableView, f"history_{name}")
        assert table.model().data(table.model().index(0, 1)) == name + " 기록"
    periods = dialog.tabs.widget(3)
    assert periods.tabText(periods.currentIndex()) == "조식"
    dialog.close()


def test_history_provider_and_export_button_use_actual_window():
    app = QApplication.instance() or QApplication([])
    window = RosterWindow(_roster())
    opened, exports = [], []

    def provider(person, meal, parent):
        opened.append((person.person_id, meal, parent))
        return QDialog(parent)

    window.set_history_provider(provider)
    window.database_export_requested.connect(lambda: exports.append(True))
    person = window.roster.persons[0]
    menu = window._create_context_menu(person, Meal.BREAKFAST)
    next(action for action in menu.actions() if action.text() == "이력보기").trigger()
    assert opened == [(person.person_id, Meal.BREAKFAST, window)]
    assert len(window._history_dialogs) == 1
    window._database_export_action.trigger()
    assert exports == [True]
    for dialog in tuple(window._history_dialogs):
        dialog.close()
    app.processEvents()
    window.close()


def test_application_routes_complaint_and_export_after_password_setup(monkeypatch):
    roster = _roster()
    calls, refreshes = [], []
    session = object()
    window = SimpleNamespace(table_model=SimpleNamespace(service_day=SERVICE_DAY))

    class Complaint:
        def __init__(self, current):
            assert current is session
        def run(self, person_id, meal, parent, *, household_day):
            assert household_day == SERVICE_DAY
            calls.append(("complaint", person_id, meal, parent))
            return SimpleNamespace(completed=True, roster=roster)

    class Password:
        def __init__(self, current):
            assert current is session
        def ensure_configured(self, parent):
            return SimpleNamespace(completed=True, roster=None)

    class Export:
        def __init__(self, current):
            assert current is session
        def run(self, day, parent, *, directory):
            calls.append(("export", day, parent, directory))

    controller = DesktopStartupController(
        complaint_controller_type=Complaint, export_password_controller_type=Password,
        database_export_controller_type=Export,
        settings=SimpleNamespace(document_output_directory=None),
    )
    monkeypatch.setattr(controller, "_replace_roster_if_changed", lambda *args: refreshes.append(args))
    controller._run_complaint(session, window, roster.persons[0].person_id, Meal.LUNCH)
    assert refreshes == [(session, window, roster)]
    controller._run_database_export(session, window)
    assert calls == [
        ("complaint", roster.persons[0].person_id, Meal.LUNCH, window),
        ("export", SERVICE_DAY, window, None),
    ]


def test_existing_status_period_panel_displays_korean_recorded_time():
    from src.desktop_v2.roster_window import StatusHistoryDialog
    from tests.test_desktop_v2_roster import FIRST_PERSON_ID, _histories
    app = QApplication.instance() or QApplication([])
    history = _histories()[(FIRST_PERSON_ID, Meal.LUNCH)]
    dialog = StatusHistoryDialog(_roster().persons[0], history)
    app.processEvents()
    tables = dialog.findChildren(QTableView)
    raw = next(table for table in tables if table.model().columnCount() == 5)
    assert raw.model().data(raw.model().index(0, 0)) == "2026-08-25 01:00"
    dialog.close()
