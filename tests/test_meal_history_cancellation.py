"""One-item cancellation through immutable audit, encrypted storage and desktop UI."""
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from PySide6.QtWidgets import QApplication, QDialog, QTableView

from src.desktop_v2.application import DesktopStartupController
from src.desktop_v2.inspector_history import InspectorMealHistory
from src.desktop_v2.ledger_reader import load_revision_chain
from src.desktop_v2.meal_history import build_meal_history_entries_by_person, build_person_meal_history_rows
from src.desktop_v2.meal_history_cancel import MealHistoryCancelController
from src.desktop_v2.person_history import create_person_history_dialog
from src.domain.meal_history_cancellation import (
    CANCELLATION_KIND, MealHistoryTarget, build_meal_history_cancellation,
)
from src.domain.meal_status_changes import build_meal_status_revision
from src.domain.delivery_defaults import DELIVERY_DEFAULTS_ID, DELIVERY_DEFAULTS_KIND, delivery_defaults_state
from src.domain.projections import ENROLLMENT_KIND, STATUS_EVENT_KIND, roster_audit_changes, status_event_audit_change
from src.domain.revisions import AuditChange, ChangeOperation, RevisionActor, RevisionMetadata, RevisionRecord, RevisionSource
from src.domain.roster import Household, HouseholdMembership, HouseholdDeliverySetting, InclusiveDateRange, Meal, MealEnrollment, Person, PersonProfile, RosterSnapshot
from src.domain.status import MealStatus, StatusEvent
from src.services.revision_restore import restore_revision
from src.storage.database_session import DatabaseSession
from src.storage.encrypted_database import RevisionConflictError
from src.storage.typed_projection import ProjectionContractError

ACTOR = RevisionActor("합성 담당자", "synthetic-sid")
DAY = date(2026, 10, 1)
STAMP = datetime(2026, 10, 1, tzinfo=UTC)


def metadata(revision_id, parent, decisions=None):
    return RevisionMetadata(revision_id, parent, STAMP, DAY, ACTOR, "합성 기록", RevisionSource.UI,
                            decisions=decisions or {})


@pytest.fixture
def session(tmp_path):
    backups = tmp_path / "backups"
    backups.mkdir()
    with DatabaseSession.create(tmp_path / "synthetic.sqlite3", "test-password", backup_directory=backups) as current:
        yield current


def initialize(session, *, enrolled=True, row_add=False, initial_status=None):
    person = Person(uuid4(), PersonProfile("합성 대상", mobile_phone="01012345678"))
    roster = RosterSnapshot(uuid4(), persons=(person,), delivery_defaults=tuple(
        HouseholdDeliverySetting(meal, weekdays=tuple(range(7)), delivery_weekdays=tuple(range(7))) for meal in Meal), enrollments=(
        MealEnrollment(person.person_id, Meal.DINNER, InclusiveDateRange(DAY)),
    ) if enrolled else ())
    decisions = {"roster_row_change": {"operation": "add", "person_id": str(person.person_id)}} if row_add else {}
    events = () if initial_status is None else (status_event_audit_change(StatusEvent(
        uuid4(), roster.revision_id, person.person_id, Meal.DINNER, initial_status,
        InclusiveDateRange(DAY, DAY if initial_status is MealStatus.PAUSED else None), STAMP,
    )),)
    record = RevisionRecord(metadata(roster.revision_id, None, decisions), (
        *events,
        *roster_audit_changes(None, roster),
        AuditChange(DELIVERY_DEFAULTS_KIND, DELIVERY_DEFAULTS_ID, ChangeOperation.INSERT, None,
                    delivery_defaults_state(roster.delivery_defaults)),
    ))
    session.append_revision(record)
    return person.person_id


def status(session, person_id, value, start=DAY, end=None, meals=(Meal.DINNER,)):
    roster = session.database.current_roster()
    prepared = build_meal_status_revision(
        roster, person_id, meals, value, InclusiveDateRange(start, end), actor=ACTOR,
        recorded_at=STAMP + timedelta(seconds=len(session.database.revision_summaries())),
        revision_id=uuid4(), event_ids={meal: uuid4() for meal in meals},
    )
    session.append_revision(prepared.record)
    event = prepared.events[-1]
    return MealHistoryTarget(prepared.record.metadata.revision_id, STATUS_EVENT_KIND, event.event_id)


def prepare(session, target):
    current = session.database.current_roster()
    return build_meal_history_cancellation(
        session.database.snapshot(current.revision_id), load_revision_chain(session.database, current.revision_id),
        target, actor=ACTOR, recorded_at=STAMP + timedelta(minutes=1), revision_id=uuid4(),
    )


def cancel(session, target):
    roster, record = prepare(session, target)
    result = session.append_revision(record)
    assert result.succeeded
    return roster, record


def rows(session, person):
    current = session.database.current_roster()
    return build_person_meal_history_rows(load_revision_chain(session.database, current.revision_id), person)


def test_cancel_stop_preserves_later_pause_audit_and_reopen(session):
    person = initialize(session)
    stopped = status(session, person, MealStatus.STOPPED, date(2026, 10, 2))
    paused = status(session, person, MealStatus.PAUSED, date(2026, 10, 5), date(2026, 10, 6))
    original = session.database.revision(stopped.revision_id)
    roster, record = cancel(session, stopped)
    history = session.database.status_history(person, Meal.DINNER)
    assert history.status_on(date(2026, 10, 2)) is MealStatus.DELIVERY
    assert history.status_on(date(2026, 10, 5)) is MealStatus.PAUSED
    assert [event.event_id for event in history.events] == [paused.entity_id]
    assert session.database.revision(stopped.revision_id) == original
    assert session.database.snapshot(stopped.revision_id).entity(STATUS_EVENT_KIND, stopped.entity_id) is not None
    assert len(rows(session, person)) == 1 and rows(session, person)[0][1] == "일시중지"
    assert "취소" in session.database.latest_revision().metadata.reason
    assert len(session.database.revision_summaries()) == 4
    session.database.check_integrity()
    path = session.path
    session.close()
    with DatabaseSession.open(path, "test-password") as reopened:
        assert reopened.database.current_roster().revision_id == roster.revision_id
        assert reopened.database.status_history(person, Meal.DINNER).status_on(date(2026, 10, 2)) is MealStatus.DELIVERY
        assert len(rows(reopened, person)) == 1
        reopened.database.check_integrity()


def test_cancel_pause_reveals_earlier_stop(session):
    person = initialize(session)
    status(session, person, MealStatus.STOPPED)
    paused = status(session, person, MealStatus.PAUSED, date(2026, 10, 5), date(2026, 10, 6))
    cancel(session, paused)
    assert session.database.status_history(person, Meal.DINNER).status_on(date(2026, 10, 5)) is MealStatus.STOPPED
    assert [row[1] for row in rows(session, person)] == ["중지"]


@pytest.mark.parametrize("fail_after_first", [False, True])
def test_atomic_cancellation_batch_preserves_audit_or_rolls_back_everything(session, fail_after_first):
    person = initialize(session)
    first = status(session, person, MealStatus.STOPPED, date(2026, 10, 2))
    second = status(session, person, MealStatus.STOPPED, date(2026, 10, 3))
    retained = status(session, person, MealStatus.STOPPED, DAY)
    before = session.database.snapshot(session.database.latest_revision_id())
    old_records = tuple(session.database.revision_summaries())

    def records():
        for index, target in enumerate((first, second)):
            if fail_after_first and index == 1:
                raise KeyboardInterrupt("synthetic interruption")
            yield prepare(session, target)[1]

    if fail_after_first:
        with pytest.raises(KeyboardInterrupt):
            session.database.append_revisions(records())
        assert session.database.snapshot(session.database.latest_revision_id()) == before
        assert session.database.revision_summaries() == old_records
    else:
        session.database.append_revisions(records())
        assert [event.event_id for event in session.database.status_events()] == [retained.entity_id]
        assert len(rows(session, person)) == 1
        assert len(session.database.revision_summaries()) == len(old_records) + 2
        assert session.database.snapshot(before.revision_id) == before
    assert session.database._require_connection().get_autocommit()
    session.database.check_integrity()


def test_cancel_one_meal_start_reverts_only_its_enrollment(session):
    person = initialize(session, enrolled=False)
    target = status(session, person, MealStatus.DELIVERY, meals=(Meal.BREAKFAST, Meal.DINNER))
    cancel(session, target)
    assert session.database.current_roster().meals_for(person, on=DAY) == (Meal.BREAKFAST,)
    assert [event.meal for event in session.database.status_events()] == [Meal.BREAKFAST]
    assert [row[:2] for row in rows(session, person)] == [("조식", "시작")]


def test_cancel_resume_start_preserves_preexisting_enrollment(session):
    person = initialize(session)
    status(session, person, MealStatus.STOPPED)
    target = status(session, person, MealStatus.DELIVERY, date(2026, 10, 2))
    before = session.database.current_roster().enrollments
    cancel(session, target)
    assert session.database.current_roster().enrollments == before
    assert session.database.status_history(person, Meal.DINNER).status_on(date(2026, 10, 2)) is MealStatus.STOPPED


def test_cancel_manual_add_start_keeps_person_and_other_fields(session):
    person = initialize(session, row_add=True)
    current = session.database.current_roster()
    entries = build_meal_history_entries_by_person(load_revision_chain(session.database, current.revision_id))[person]
    assert entries[0].target.entity_kind == ENROLLMENT_KIND
    cancel(session, entries[0].target)
    assert session.database.current_roster().persons == current.persons
    assert session.database.current_roster().enrollments == ()
    assert rows(session, person) == ()


def test_start_dependency_block_and_cancel_in_reverse_order(session):
    person = initialize(session, enrolled=False)
    started = status(session, person, MealStatus.DELIVERY)
    stopped = status(session, person, MealStatus.STOPPED, date(2026, 10, 2))
    before = session.database.snapshot(stopped.revision_id)
    with pytest.raises(ValueError, match="다른 상태 기록"):
        prepare(session, started)
    assert session.database.snapshot(stopped.revision_id) == before
    assert len(session.database.revision_summaries()) == 3
    cancel(session, stopped)
    cancel(session, started)
    assert session.database.current_roster().enrollments == ()
    assert session.database.status_events() == ()
    assert rows(session, person) == ()


def test_stale_and_forged_cancellation_are_atomic(session):
    person = initialize(session)
    stopped = status(session, person, MealStatus.STOPPED)
    _, record = prepare(session, stopped)
    with pytest.raises(ValueError, match="취소 내용"):
        session.append_revision(replace(record, changes=record.changes[:1]))
    unvalidated = RevisionRecord(replace(record.metadata, decisions={}), record.changes[:1])
    with pytest.raises(ProjectionContractError, match="append-only"):
        session.append_revision(unvalidated)
    changed = uuid4()
    session.append_revision(RevisionRecord(metadata(changed, stopped.revision_id), (
        AuditChange("synthetic.setting", uuid4(), ChangeOperation.INSERT, None, {"value": 1}),
    )))
    with pytest.raises(RevisionConflictError):
        session.append_revision(record)
    assert session.database.latest_revision().metadata.revision_id == changed
    assert len(session.database.status_events()) == 1
    assert not any(ref.entity_kind == CANCELLATION_KIND for ref in session.database.snapshot(changed).entities)
    session.database.check_integrity()


def test_restore_before_cancellation_restores_event_and_history_visibility(session):
    person = initialize(session)
    target = status(session, person, MealStatus.STOPPED)
    _, cancelled = cancel(session, target)
    assert rows(session, person) == ()
    restored = restore_revision(session, target.revision_id, cancelled.metadata.revision_id, ACTOR)
    assert restored.revision_id is not None
    assert session.database.status_history(person, Meal.DINNER).status_on(DAY) is MealStatus.STOPPED
    assert [row[1] for row in rows(session, person)] == ["중지"]
    cancel(session, target)
    assert rows(session, person) == ()


class Messages:
    def __init__(self):
        self.errors = []
        self.warnings = []
    def critical(self, *args):
        self.errors.append(args)
    def warning(self, *args):
        self.warnings.append(args)


class AcceptedDialog:
    def __init__(self, *args):
        pass
    def exec(self):
        return QDialog.DialogCode.Accepted


def test_person_menu_controller_save_refresh_and_stale_failure(session, monkeypatch):
    app = QApplication.instance() or QApplication([])
    person = initialize(session)
    target = status(session, person, MealStatus.PAUSED, DAY, DAY)
    roster = session.database.current_roster()
    dialog = create_person_history_dialog(session, roster.person(person), Meal.DINNER)
    selected = []
    dialog.meal_history_cancel_requested.connect(selected.append)
    dialog._meal_history_menu(0).actions()[0].trigger()
    assert selected == [target]
    messages = Messages()
    controller = MealHistoryCancelController(session, dialog_type=AcceptedDialog, message_box=messages,
                                             actor_provider=lambda: ACTOR)
    result = controller.run(selected[0], dialog.expected_revision_id, dialog)
    assert result is not None and messages.errors == []
    dialog.reload_meal_history(session)
    assert dialog.findChild(QTableView, "history_식종 변경").model().rowCount() == 0
    assert dialog.expected_revision_id == result.revision_id
    assert InspectorMealHistory(session.database).rows_for(result, person) == ()
    assert controller.run(target, roster.revision_id, dialog) is None
    assert "다른 작업" in messages.errors[0][-1]
    assert len(session.database.revision_summaries()) == 3
    dialog.close()
    app.processEvents()


def test_application_cancellation_refreshes_main_and_source_dialog(monkeypatch):
    from src.desktop_v2 import application as module
    saved = SimpleNamespace(revision_id=uuid4())
    calls = []
    class Controller:
        def __init__(self, session):
            pass
        def run(self, target, revision_id, parent=None):
            calls.append((target, revision_id, parent))
            return saved
    monkeypatch.setattr(module, "MealHistoryCancelController", Controller)
    controller = DesktopStartupController()
    monkeypatch.setattr(controller, "_replace_roster_if_changed", lambda *args: calls.append(args))
    dialog = SimpleNamespace(reload_meal_history=lambda session: calls.append(("reload", session)))
    session, window, target, revision_id = object(), object(), object(), uuid4()
    controller._run_meal_history_cancel(session, window, target, revision_id, source_dialog=dialog)
    assert calls == [(target, revision_id, dialog), (session, window, saved), ("reload", session)]


@pytest.mark.parametrize("initial_status", (MealStatus.PAUSED, MealStatus.STOPPED))
def test_cancel_initial_non_delivery_reveals_separately_cancellable_start(session, initial_status):
    person = initialize(session, row_add=True, initial_status=initial_status)
    roster = session.database.current_roster()
    entries = build_meal_history_entries_by_person(load_revision_chain(session.database, roster.revision_id))[person]
    assert len(entries) == 1 and entries[0].target.entity_kind == STATUS_EVENT_KIND
    cancel(session, entries[0].target)
    current = session.database.current_roster()
    assert current.meals_for(person, on=DAY) == (Meal.DINNER,)
    assert session.database.status_history(person, Meal.DINNER).status_on(DAY) is MealStatus.DELIVERY
    remaining = build_meal_history_entries_by_person(load_revision_chain(session.database, current.revision_id))[person]
    assert len(remaining) == 1 and remaining[0].row[1] == "시작"
    cancel(session, remaining[0].target)
    assert session.database.current_roster().enrollments == ()
    assert rows(session, person) == ()


def shared_household(session, person):
    from src.domain.household_shared import HOUSEHOLD_SHARED_ID, HOUSEHOLD_SHARED_KIND
    before = session.database.current_roster()
    other = Person(uuid4(), PersonProfile("합성 가구원", mobile_phone="01012345678"))
    household = Household(uuid4())
    after = replace(before, revision_id=uuid4(), persons=(*before.persons, other),
                    households=(household,), memberships=(
                        HouseholdMembership(person, household.household_id),
                        HouseholdMembership(other.person_id, household.household_id),
                    ), enrollments=(*before.enrollments, *(
                        (MealEnrollment(other.person_id, Meal.DINNER, InclusiveDateRange(DAY)),)
                        if before.enrollments else ())))
    session.append_revision(RevisionRecord(metadata(after.revision_id, before.revision_id), (
        *roster_audit_changes(before, after),
        AuditChange(HOUSEHOLD_SHARED_KIND, HOUSEHOLD_SHARED_ID, ChangeOperation.INSERT, None,
                    {"columns": ["status:dinner"]}),
    )))
    return other.person_id


def test_shared_enrollment_cascade_is_blocked_atomically(session):
    person = initialize(session, enrolled=False)
    other = shared_household(session, person)
    started = status(session, person, MealStatus.DELIVERY)
    before = session.database.current_roster()
    assert before.meals_for(other, on=DAY) == (Meal.DINNER,)
    with pytest.raises(ValueError, match="공용"):
        prepare(session, started)
    assert session.database.current_roster() == before
    assert len(session.database.status_events()) == 2


def test_shared_status_cancel_requires_exception_and_never_deletes_peer(session):
    from src.domain.household_shared import HOUSEHOLD_SHARED_ID, HOUSEHOLD_SHARED_KIND
    person = initialize(session)
    other = shared_household(session, person)
    stopped = status(session, person, MealStatus.STOPPED)
    peer_event = session.database.status_events(person_id=other)[0]
    before = session.database.current_roster()
    with pytest.raises(ValueError, match="공용 상태"):
        prepare(session, stopped)
    assert session.database.current_roster() == before
    old = session.database.snapshot(before.revision_id).entity(HOUSEHOLD_SHARED_KIND, HOUSEHOLD_SHARED_ID)
    session.append_revision(RevisionRecord(metadata(uuid4(), before.revision_id), (
        AuditChange(HOUSEHOLD_SHARED_KIND, HOUSEHOLD_SHARED_ID, ChangeOperation.UPDATE, old,
                    {"columns": ["status:dinner"], "exceptions": [{"person_id": str(person), "column": "status:dinner"}]}),
    )))
    cancel(session, stopped)
    assert session.database.status_history(person, Meal.DINNER).status_on(DAY) is MealStatus.DELIVERY
    assert session.database.status_history(other, Meal.DINNER).status_on(DAY) is MealStatus.STOPPED
    assert session.database.status_events(person_id=other) == (peer_event,)
    assert rows(session, person) == ()
    assert len(rows(session, other)) == 1



def test_roster_history_context_menu_emits_exact_row_identity(session, monkeypatch):
    from src.desktop_v2 import roster_window as module
    app = QApplication.instance() or QApplication([])
    person = initialize(session)
    target = status(session, person, MealStatus.STOPPED)
    roster = session.database.current_roster()
    history = InspectorMealHistory(session.database)
    window = module.RosterWindow(roster)
    window.set_meal_change_history_provider(lambda item: history.rows_for(roster, item.person_id))
    window._populate_meal_history(roster.person(person))
    app.processEvents()
    emitted = []
    window.meal_history_cancel_requested.connect(lambda *args: emitted.append(args))
    def choose(menu, *args):
        assert menu.actions()[0].text() == "선택한 변경 취소"
        menu.actions()[0].trigger()
    class Menu(module.QMenu):
        def exec(self, *args):
            return choose(self, *args)
    monkeypatch.setattr(module, "QMenu", Menu)
    item = window._meal_history_list.item(0)
    window._open_meal_history_context_menu(window._meal_history_list.visualItemRect(item).center())
    assert emitted == [(target, roster.revision_id)]
    window.close()
    app.processEvents()


@pytest.mark.parametrize("mode", ("decline", "concurrent_save"))
def test_cancel_confirmation_decline_or_concurrent_save_has_no_partial_cancellation(session, mode):
    app = QApplication.instance() or QApplication([])
    person = initialize(session)
    target = status(session, person, MealStatus.STOPPED)
    expected = session.database.current_roster().revision_id
    class Dialog(AcceptedDialog):
        def exec(self):
            if mode == "decline":
                return QDialog.DialogCode.Rejected
            session.append_revision(RevisionRecord(metadata(uuid4(), expected), (
                AuditChange("synthetic.concurrent", uuid4(), ChangeOperation.INSERT, None, {"value": 1}),
            )))
            return QDialog.DialogCode.Accepted
    messages = Messages()
    controller = MealHistoryCancelController(session, dialog_type=Dialog, message_box=messages,
                                             actor_provider=lambda: ACTOR)
    assert controller.run(target, expected) is None
    assert len(session.database.status_events()) == 1
    assert [row[1] for row in rows(session, person)] == ["중지"]
    assert len(session.database.revision_summaries()) == (2 if mode == "decline" else 3)
    assert bool(messages.errors) is (mode == "concurrent_save")
    app.processEvents()
