"""Saved meal details, duplicate prevention and invoice-only period merging."""
from datetime import date
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QApplication, QDialog, QTableView

from src.desktop_v2.inspector_history import InspectorMealHistory
from src.desktop_v2.meal_history import build_person_meal_history_rows
from src.desktop_v2.meal_status_edit import MealStatusChangeController, MealStatusChangeDialog
from src.desktop_v2.monthly_meal_history import MonthlyMealHistoryDialog
from src.desktop_v2.person_history import create_person_history_dialog
from src.domain.intake import IntakeChannel
from src.domain.roster import InclusiveDateRange, Meal
from src.domain.status import MealStatus
from src.services.invoice_remarks import invoice_history_notes
from src.storage.database_session import DatabaseSession
from tests.test_monthly_meal_history import _initial, _event, _result, ACTOR, STAMP

DAY = date(2026, 10, 1)
NOTE = "보호자 요청\n10월 일정 확인 <메모>"


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("channel", [None, IntakeChannel.PHONE])
@pytest.mark.parametrize("cancel", [False, True])
def test_details_reopen_personal_inspector_monthly_and_cancel(app, tmp_path, batch, channel, cancel):
    roster, records, _ = _initial(people=2)
    people = roster.persons if batch else roster.persons[:1]
    targets = tuple((person.person_id, Meal.DINNER) for person in people)

    class Reviewed:
        def __init__(self, *args, **kwargs):
            pass

        def exec(self):
            return QDialog.Rejected if cancel else QDialog.Accepted

        def selected_meals(self):
            return (Meal.DINNER,)

        def selected_targets(self):
            return targets

        def selected_status(self):
            return MealStatus.STOPPED

        def selected_period(self):
            return InclusiveDateRange(DAY, None)

        def selected_intake_channel(self):
            return channel

        def selected_note(self):
            return NOTE

    errors = []
    messages = SimpleNamespace(critical=lambda *args: errors.append(args), warning=lambda *args: pytest.fail(str(args)))
    path = tmp_path / "meal-details.sqlite3"
    with DatabaseSession.create(path, "password") as session:
        session.append_revision(records[0])
        controller = MealStatusChangeController(session, dialog_type=Reviewed, message_box=messages,
            actor_provider=lambda: ACTOR, recorded_at_provider=lambda: STAMP)
        outcome = (controller.run_batch(targets, MealStatus.STOPPED, DAY) if batch else
                   controller.run(targets[0][0], Meal.DINNER, MealStatus.STOPPED, DAY))
        assert outcome.completed is not cancel
        assert not errors
        saved_id = session.database.current_roster().revision_id
        if cancel:
            assert saved_id == roster.revision_id
            return
        # Repeating the same stop must not write another revision or backup.
        session.try_backup_now = lambda: pytest.fail("duplicate change attempted backup")
        repeated = (controller.run_batch(targets, MealStatus.STOPPED, DAY) if batch else
                    controller.run(targets[0][0], Meal.DINNER, MealStatus.STOPPED, DAY))
        assert not repeated.completed and "이미 중지" in str(errors)
        assert session.database.current_roster().revision_id == saved_id

    with DatabaseSession.open(path, "password") as session:
        saved = session.database.current_roster()
        records.append(session.database.latest_revision())
        expected_channel = channel.value if channel else "-"
        key = "batch_meal_status_change" if batch else "meal_status_change"
        assert records[-1].metadata.decisions[key]["note"] == NOTE
        snapshot = session.database.snapshot(saved_id)
        from src.services.monthly_meal_history import build_monthly_meal_history
        monthly = build_monthly_meal_history(snapshot, tuple(records), 2026, 10)
        assert len(monthly.entries) == len(targets)
        for person in people:
            row = build_person_meal_history_rows(tuple(records), person.person_id)[0]
            assert row[2:5] == (expected_channel, "2026-10-01 ~ ", NOTE)
            inspector = InspectorMealHistory(session.database).rows_for(saved, person.person_id)[0]
            assert (inspector.intake_channel, inspector.note) == (expected_channel, NOTE)
        personal = create_person_history_dialog(session, saved.person(targets[0][0]), Meal.DINNER)
        view = personal.findChild(QTableView, "history_식종 변경")
        assert view.model().rows[0][2:5] == (expected_channel, "2026-10-01 ~ ", NOTE)
        personal.close()
        dialog = MonthlyMealHistoryDialog(DAY, lambda *_: monthly)
        assert [dialog.table.item(0, i).text() for i in (3, 4, 5)] == [expected_channel, "2026-10-01 ~ ", NOTE]
        assert dialog.column_filter_values("intake_channel") == (expected_channel,)
        assert dialog.column_filter_values("note") == (NOTE,)
        dialog.close()


@pytest.mark.parametrize("existing, requested, blocked", [
    (None, MealStatus.DELIVERY, True),
    (MealStatus.STOPPED, MealStatus.STOPPED, True),
    (MealStatus.STOPPED, MealStatus.DELIVERY, False),
    (MealStatus.PAUSED, MealStatus.STOPPED, False),
    (MealStatus.PAUSED, MealStatus.PAUSED, False),
])
def test_editor_rejects_only_repeated_delivery_and_stop(app, existing, requested, blocked):
    from src.domain.status import resolve_status_history
    roster, records, states = _initial()
    events = () if existing is None else (_event(roster, records, states, existing, DAY,
        date(2026, 10, 8) if existing is MealStatus.PAUSED else None),)
    history = resolve_status_history(roster.persons[0].person_id, Meal.DINNER, events)
    dialog = MealStatusChangeDialog(roster.persons[0], roster, {Meal.DINNER: history}, DAY, Meal.DINNER, requested)
    assert dialog._save_button.isEnabled() is not blocked
    if blocked:
        assert "이미" in dialog._error.text()
        dialog._save()
        assert dialog.result() != QDialog.Accepted
    dialog.close()


@pytest.mark.parametrize("actions, expected", [
    ([(MealStatus.PAUSED, 2, 12), (MealStatus.PAUSED, 8, 20)], "석식: 10/2~10/20 일시중지"),
    ([(MealStatus.PAUSED, 2, 20), (MealStatus.PAUSED, 8, 12)], "석식: 10/2~10/20 일시중지"),
    ([(MealStatus.PAUSED, 8, 20), (MealStatus.PAUSED, 2, 12)], "석식: 10/2~10/20 일시중지"),
    ([(MealStatus.PAUSED, 2, 20), (MealStatus.STOPPED, 8, None)], "석식: 10/2~ 중지"),
    ([(MealStatus.PAUSED, 2, 5), (MealStatus.PAUSED, 8, 12)], "석식: 10/2~10/5 일시중지\n석식: 10/8~10/12 일시중지"),
    ([(MealStatus.PAUSED, 2, 20), (MealStatus.DELIVERY, 8, 12)],
     "석식: 10/2~10/7 일시중지\n석식: 10/8~10/12 배송\n석식: 10/13~10/20 일시중지"),
])
def test_invoice_merges_continuous_pauses_and_stop_without_rewriting_history(actions, expected):
    from src.domain.revisions import HistoricalSnapshot
    roster, records, states = _initial()
    for status, first, last in actions:
        _event(roster, records, states, status, date(2026, 10, first), date(2026, 10, last) if last else None)
    original = tuple(records)
    rows_before = build_person_meal_history_rows(original, roster.persons[0].person_id)
    monthly_before = _result(records, states, 2026, 10)
    snapshot = HistoricalSnapshot(records[-1].metadata.revision_id, states)
    notes = invoice_history_notes(snapshot, records, DAY, date(2026, 10, 31), (Meal.DINNER,))
    assert notes[roster.persons[0].person_id] == expected
    assert tuple(records) == original
    assert build_person_meal_history_rows(tuple(records), roster.persons[0].person_id) == rows_before
    assert len(rows_before) == len(actions)
    assert _result(records, states, 2026, 10) == monthly_before


def test_invoice_pause_to_stop_keeps_start_in_previous_month():
    from src.domain.revisions import HistoricalSnapshot
    roster, records, states = _initial()
    _event(roster, records, states, MealStatus.PAUSED, date(2026, 9, 28), date(2026, 10, 5))
    _event(roster, records, states, MealStatus.STOPPED, DAY)
    snapshot = HistoricalSnapshot(records[-1].metadata.revision_id, states)
    notes = invoice_history_notes(snapshot, records, DAY, date(2026, 10, 31), (Meal.DINNER,))
    assert notes[roster.persons[0].person_id] == "석식: 9/28~ 중지"
    assert _result(records, states, 2026, 10).entries[0].period.starts_on == DAY


def test_invoice_does_not_merge_stop_starting_after_selected_period():
    from src.domain.revisions import HistoricalSnapshot
    roster, records, states = _initial()
    _event(roster, records, states, MealStatus.PAUSED, DAY, date(2026, 10, 15))
    _event(roster, records, states, MealStatus.STOPPED, date(2026, 10, 8))
    snapshot = HistoricalSnapshot(records[-1].metadata.revision_id, states)
    notes = invoice_history_notes(snapshot, records, DAY, date(2026, 10, 5), (Meal.DINNER,))
    assert notes[roster.persons[0].person_id] == "석식: 10/1~10/7 일시중지"


def test_mixed_batch_with_existing_delivery_does_not_partially_enroll(tmp_path):
    roster, records, _ = _initial(people=2)
    targets = ((roster.persons[0].person_id, Meal.DINNER), (roster.persons[1].person_id, Meal.LUNCH))
    errors = []
    reviewed = SimpleNamespace(
        exec=lambda: QDialog.Accepted,
        selected_meals=lambda: (Meal.DINNER, Meal.LUNCH),
        selected_targets=lambda: targets,
        selected_status=lambda: MealStatus.DELIVERY,
        selected_period=lambda: InclusiveDateRange(DAY, None),
    )
    with DatabaseSession.create(tmp_path / "duplicates.sqlite3", "password") as session:
        session.append_revision(records[0])
        session.try_backup_now = lambda: pytest.fail("duplicate change attempted backup")
        controller = MealStatusChangeController(session, dialog_type=lambda *a, **kw: reviewed,
            message_box=SimpleNamespace(critical=lambda *args: errors.append(args)))
        outcome = controller.run_batch(targets, MealStatus.DELIVERY, DAY)
        assert not outcome.completed and "이미 배송" in str(errors)
        assert session.database.current_roster().revision_id == roster.revision_id
        assert Meal.LUNCH not in session.database.current_roster().meals_for(targets[1][0], on=DAY)


def test_cancelled_stop_does_not_merge_into_invoice_pause():
    from src.domain.revisions import HistoricalSnapshot, EntityRef
    from src.domain.meal_history_cancellation import MealHistoryTarget, build_meal_history_cancellation
    from src.domain.projections import STATUS_EVENT_KIND
    from uuid import uuid4
    roster, records, states = _initial()
    _event(roster, records, states, MealStatus.PAUSED, DAY, date(2026, 10, 15))
    stop = _event(roster, records, states, MealStatus.STOPPED, date(2026, 10, 8))
    snapshot = HistoricalSnapshot(records[-1].metadata.revision_id, states)
    _, cancellation = build_meal_history_cancellation(snapshot, tuple(records),
        MealHistoryTarget(stop.revision_id, STATUS_EVENT_KIND, stop.event_id),
        actor=ACTOR, recorded_at=stop.recorded_at, revision_id=uuid4())
    states = dict(states)
    for change in cancellation.changes:
        ref = EntityRef(change.entity_kind, change.entity_id)
        if change.after is None:
            states.pop(ref, None)
        else:
            states[ref] = change.after
    records.append(cancellation)
    snapshot = HistoricalSnapshot(cancellation.metadata.revision_id, states)
    notes = invoice_history_notes(snapshot, records, DAY, date(2026, 10, 31), (Meal.DINNER,))
    assert notes[roster.persons[0].person_id] == "석식: 10/1~10/15 일시중지"
