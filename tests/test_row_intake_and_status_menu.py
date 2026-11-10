"""Row creation records intake consistently; menus offer valid current-state actions."""
from datetime import date
from uuid import uuid4

import pytest
from PySide6.QtWidgets import QApplication, QDialog

from src.desktop_v2.meal_history import build_person_meal_history_rows
from src.desktop_v2.meal_status_edit import MealStatusChangeDialog
from src.desktop_v2.roster_row_edit import RosterRowCreateDialog, RosterRowEditController
from src.desktop_v2.roster_window import RosterWindow
from src.domain.intake import IntakeChannel
from src.domain.roster import InclusiveDateRange, Meal
from src.domain.status import MealStatus, StatusEvent, resolve_status_history
from src.storage.database_session import DatabaseSession
from tests.test_meal_history_cancellation import ACTOR, DAY, STAMP, Messages, initialize, load_revision_chain, session
from tests.test_roster_batch_selection import _window, FIRST, SECOND


def test_profile_only_registration_has_no_intake_or_meal_history(session):
    app = QApplication.instance() or QApplication([])
    person = initialize(session)
    roster = session.database.current_roster()
    dialog = RosterRowCreateDialog(Meal.DINNER, DAY, delivery_defaults=roster.delivery_defaults)
    status_dialog = MealStatusChangeDialog(roster.person(person), roster, {}, DAY, Meal.DINNER, MealStatus.STOPPED)
    assert not hasattr(dialog, "_intake_channel")
    dialog._editors["name"].setText("접수 신규")
    dialog._accept_valid()
    request = dialog.request()
    assert request.intake_channel is None
    class Accepted:
        def __init__(self, *args):
            pass
        def exec(self):
            return QDialog.Accepted
        def request(self):
            return request
    messages = Messages()
    controller = RosterRowEditController(session, dialog_type=Accepted, message_box=messages,
        actor_provider=lambda: ACTOR, effective_date_provider=lambda: DAY, recorded_at_provider=lambda: STAMP)
    outcome = controller.add(Meal.DINNER)
    assert outcome.completed and not messages.errors
    added = next(p for p in outcome.roster.persons if p.profile.name == "접수 신규")
    revision_id = outcome.roster.revision_id
    path = session.path
    session.close()
    with DatabaseSession.open(path, "test-password") as reopened:
        rows = build_person_meal_history_rows(load_revision_chain(reopened.database, revision_id), added.person_id)
        assert rows == ()
    dialog.close()
    status_dialog.close()
    app.processEvents()


@pytest.mark.parametrize("current, labels", [
    (MealStatus.DELIVERY, ("조식 일시중지", "조식 중지")),
    (MealStatus.STOPPED, ("조식 배송",)),
    (MealStatus.PAUSED, ("조식 배송", "조식 일시중지", "조식 중지")),
])
def test_menu_options_follow_current_state_and_keep_other_actions(current, labels):
    app = QApplication.instance() or QApplication([])
    original = _window()
    day = date(2026, 8, 17)
    event = StatusEvent(uuid4(), original.roster.revision_id, FIRST, Meal.BREAKFAST, current,
                        InclusiveDateRange(day, day if current is MealStatus.PAUSED else None), STAMP)
    window = RosterWindow(original.roster, {(FIRST, Meal.BREAKFAST): resolve_status_history(FIRST, Meal.BREAKFAST, (event,))})
    window._set_day(day)
    menu = window._create_context_menu(window.roster.person(FIRST), Meal.BREAKFAST)
    texts = [action.text() for action in menu.actions()]
    assert tuple(t for t in texts if t.startswith("조식 ")) == labels
    assert all(label in texts for label in ("이력보기", "민원", "배송담당 변경"))
    emitted = []
    window.status_action_requested.connect(lambda *args: emitted.append(args))
    menu.actions()[0].trigger()
    assert emitted[0][:2] == (FIRST, Meal.BREAKFAST)
    window.close()
    original.close()


def test_mixed_batch_offers_only_transitions_allowed_for_every_selected_cell():
    app = QApplication.instance() or QApplication([])
    original = _window()
    day = date(2026, 8, 17)
    stopped = StatusEvent(uuid4(), original.roster.revision_id, FIRST, Meal.BREAKFAST, MealStatus.STOPPED,
                          InclusiveDateRange(day), STAMP)
    window = RosterWindow(original.roster, {(FIRST, Meal.BREAKFAST): resolve_status_history(FIRST, Meal.BREAKFAST, (stopped,))})
    window._set_day(day)
    targets = ((FIRST, Meal.BREAKFAST), (SECOND, Meal.BREAKFAST))
    menu = window._create_context_menu(window.roster.person(FIRST), Meal.BREAKFAST, batch_targets=targets)
    assert [a.text() for a in menu.actions() if a.text().startswith("선택한 식종")] == ["선택한 식종 2개 · 배송담당 변경"]
    window.close()
    original.close()
