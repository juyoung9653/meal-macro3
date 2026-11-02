from __future__ import annotations

import os
from datetime import UTC, date, datetime
from pathlib import Path
from typing import ClassVar
from uuid import UUID

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QTime
from PySide6.QtWidgets import QApplication, QDialog

from src.desktop_v2.complaints import (
    ComplaintController,
    ComplaintDialog,
    ComplaintStatus,
)
from src.domain.complaints import complaints_from_snapshot
from src.domain.intake import IntakeChannel
from src.domain.projections import roster_audit_changes
from src.domain.revisions import (
    RevisionActor,
    RevisionMetadata,
    RevisionRecord,
    RevisionSource,
)
from src.domain.roster import (
    Household,
    HouseholdMembership,
    InclusiveDateRange,
    Meal,
    Person,
    PersonProfile,
    RosterSnapshot,
)
from src.storage.database_session import DatabaseSession

BASE = UUID("a2000000-0000-0000-0000-000000000001")
NEXT = UUID("a2000000-0000-0000-0000-000000000002")
PERSON = UUID("a2000000-0000-0000-0000-000000000003")
COMPLAINT = UUID("a2000000-0000-0000-0000-000000000004")
NOW = datetime(2026, 9, 20, 10, 30, tzinfo=UTC)
ACTOR = RevisionActor("tester", "S-1-5-21-test")


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _initial() -> RevisionRecord:
    roster = RosterSnapshot(BASE, persons=(Person(PERSON, PersonProfile("아동")),))
    return _initial_for(roster)


def _initial_for(roster: RosterSnapshot) -> RevisionRecord:
    return RevisionRecord(
        RevisionMetadata(BASE, None, NOW, None, ACTOR, "initial", RevisionSource.UI),
        roster_audit_changes(RosterSnapshot(UUID("a2000000-0000-0000-0000-000000000099")), roster),
    )


def test_dialog_requires_content_and_keeps_related_meal_optional(app):
    dialog = ComplaintDialog(Person(PERSON, PersonProfile("아동")), received_at=NOW)
    assert dialog._person.text() == "아동"
    assert dialog.meal() is None
    assert not dialog._save_button.isEnabled()
    dialog._content.setPlainText("문의 내용")
    app.processEvents()
    assert dialog._save_button.isEnabled()
    assert dialog.content() == "문의 내용"
    assert dialog.channel() is IntakeChannel.PHONE
    dialog._meal_checks[Meal.DINNER].setChecked(True)
    assert dialog.meal() is Meal.DINNER
    assert dialog.selected_meals() == (Meal.DINNER,)


def test_dialog_converts_utc_received_time_to_local_wall_time_without_losing_instant(
    app: QApplication,
):
    del app
    received_utc = datetime(2026, 9, 21, 1, 30, tzinfo=UTC)
    dialog = ComplaintDialog(Person(PERSON, PersonProfile("아동")), received_at=received_utc)

    assert dialog.received_at().astimezone(UTC) == received_utc
    dialog._received_at.setTime(QTime(11, 45))
    assert dialog.received_at().astimezone(UTC) == datetime(2026, 9, 21, 2, 45, tzinfo=UTC)


def test_dialog_selects_multiple_meals_and_current_household_members(app: QApplication):
    del app
    household = UUID("a2000000-0000-0000-0000-000000000020")
    sibling = UUID("a2000000-0000-0000-0000-000000000021")
    roster = RosterSnapshot(
        BASE,
        persons=(Person(PERSON, PersonProfile("아동")), Person(sibling, PersonProfile("형제"))),
        households=(Household(household),),
        memberships=(
            HouseholdMembership(PERSON, household, InclusiveDateRange(date(2026, 9, 1))),
            HouseholdMembership(sibling, household, InclusiveDateRange(date(2026, 9, 15))),
        ),
    )
    dialog = ComplaintDialog(
        roster.person(PERSON), roster=roster, household_day=date(2026, 9, 20), received_at=NOW
    )
    dialog._meal_checks[Meal.BREAKFAST].setChecked(True)
    dialog._meal_checks[Meal.DINNER].setChecked(True)
    assert dialog.selected_meals() == (Meal.BREAKFAST, Meal.DINNER)
    assert dialog.selected_person_ids() == (PERSON,)
    dialog._same_household.setChecked(True)
    assert dialog.selected_person_ids() == (PERSON, sibling)
    assert "2명" in dialog._targets.text() and "형제" in dialog._targets.text()
    expired = ComplaintDialog(
        roster.person(PERSON), roster=roster, household_day=date(2026, 9, 10), received_at=NOW
    )
    assert expired._same_household.isEnabled()


class _AcceptedDialog:
    def __init__(self, person, meal, *, received_at, parent=None):
        assert person.person_id == PERSON
        assert meal is None
        assert received_at == NOW
        del parent

    def exec(self):
        return QDialog.DialogCode.Accepted

    @staticmethod
    def received_at(): return NOW
    @staticmethod
    def channel(): return IntakeChannel.EMAIL
    @staticmethod
    def content(): return "메일로 접수"
    @staticmethod
    def meal(): return None


class _CancelledDialog:
    def __init__(self, *args, **kwargs): pass
    def exec(self): return QDialog.DialogCode.Rejected


class _Messages:
    calls: ClassVar[list[tuple[str, tuple]]] = []
    @classmethod
    def critical(cls, *args): cls.calls.append(("critical", args))
    @classmethod
    def warning(cls, *args): cls.calls.append(("warning", args))


class _FailingDatabase:
    def __init__(self, roster):
        self._roster = roster

    def current_roster(self):
        return self._roster


class _FailingSession:
    def __init__(self, roster):
        self.database = _FailingDatabase(roster)
        self.appended = []

    def append_revision(self, record):
        self.appended.append(record)
        raise RuntimeError("DB write failed")


class _HouseholdAcceptedDialog(ComplaintDialog):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._content.setPlainText("가구 민원")
        self._meal_checks[Meal.BREAKFAST].setChecked(True)
        self._meal_checks[Meal.DINNER].setChecked(True)
        self._same_household.setChecked(True)

    def exec(self):
        return QDialog.DialogCode.Accepted


def test_controller_saves_cancelled_does_not_write_and_failure_is_not_success(tmp_path: Path):
    path = tmp_path / "complaints.sqlite3"
    with DatabaseSession.create(path, "password", "recovery") as session:
        session.append_revision(_initial())
        controller_args = {
            "actor_provider": lambda: ACTOR,
            "now_provider": lambda: NOW,
            "recorded_at_provider": lambda: NOW,
            "complaint_id_factory": lambda: COMPLAINT,
            "revision_id_factory": lambda: NEXT,
            "message_box": _Messages,
        }
        saved = ComplaintController(session, dialog_type=_AcceptedDialog, **controller_args).run(PERSON)
        assert saved.completed and saved.roster is not None
        assert complaints_from_snapshot(session.database.snapshot(NEXT))[0].content == "메일로 접수"

    with DatabaseSession.open(path, "password") as session:
        cancelled = ComplaintController(session, dialog_type=_CancelledDialog, **controller_args).run(PERSON)
        assert cancelled.status is ComplaintStatus.CANCELLED
        assert session.database.current_roster().revision_id == NEXT
        failed = ComplaintController(session, dialog_type=_AcceptedDialog, **controller_args).run(UUID(int=4))
        assert failed.status is ComplaintStatus.FAILED
        assert not failed.completed


def test_controller_reports_append_failure_without_returning_a_successful_roster():
    roster = RosterSnapshot(BASE, persons=(Person(PERSON, PersonProfile("아동")),))
    session = _FailingSession(roster)
    outcome = ComplaintController(
        session,
        dialog_type=_AcceptedDialog,
        actor_provider=lambda: ACTOR,
        now_provider=lambda: NOW,
        recorded_at_provider=lambda: NOW,
        complaint_id_factory=lambda: COMPLAINT,
        revision_id_factory=lambda: NEXT,
        message_box=_Messages,
    ).run(PERSON)

    assert outcome.status is ComplaintStatus.FAILED
    assert outcome.roster is None
    assert not outcome.completed
    assert session.database.current_roster() is roster
    assert len(session.appended) == 1


def test_controller_saves_two_household_people_once_and_reopens(tmp_path: Path):
    sibling = UUID("a2000000-0000-0000-0000-000000000031")
    household = UUID("a2000000-0000-0000-0000-000000000032")
    roster = RosterSnapshot(
        BASE,
        persons=(Person(PERSON, PersonProfile("아동")), Person(sibling, PersonProfile("형제"))),
        households=(Household(household),),
        memberships=(HouseholdMembership(PERSON, household), HouseholdMembership(sibling, household)),
    )
    complaint_ids = iter((COMPLAINT, UUID("a2000000-0000-0000-0000-000000000033")))
    path = tmp_path / "household.sqlite3"
    with DatabaseSession.create(path, "password", "recovery") as session:
        session.append_revision(_initial_for(roster))
        outcome = ComplaintController(
            session, dialog_type=_HouseholdAcceptedDialog, actor_provider=lambda: ACTOR,
            now_provider=lambda: NOW, recorded_at_provider=lambda: NOW,
            complaint_id_factory=lambda: next(complaint_ids), revision_id_factory=lambda: NEXT,
            message_box=_Messages,
        ).run(PERSON, household_day=NOW.date())
        assert outcome.completed
        assert len(session.database.revision_summaries()) == 2
    with DatabaseSession.open(path, "password") as session:
        complaints = complaints_from_snapshot(session.database.snapshot(NEXT))
        assert {item.person_id for item in complaints} == {PERSON, sibling}
        assert all(item.meals == (Meal.BREAKFAST, Meal.DINNER) for item in complaints)
        assert all(item.content == "가구 민원" and item.channel is IntakeChannel.PHONE for item in complaints)


def test_batch_append_failure_attempts_one_two_change_revision():
    sibling = UUID("a2000000-0000-0000-0000-000000000041")
    household = UUID("a2000000-0000-0000-0000-000000000042")
    roster = RosterSnapshot(
        BASE, persons=(Person(PERSON, PersonProfile("아동")), Person(sibling, PersonProfile("형제"))),
        households=(Household(household),), memberships=(HouseholdMembership(PERSON, household), HouseholdMembership(sibling, household)),
    )
    ids = iter((COMPLAINT, UUID("a2000000-0000-0000-0000-000000000043")))
    session = _FailingSession(roster)
    outcome = ComplaintController(
        session, dialog_type=_HouseholdAcceptedDialog, actor_provider=lambda: ACTOR,
        now_provider=lambda: NOW, recorded_at_provider=lambda: NOW,
        complaint_id_factory=lambda: next(ids), revision_id_factory=lambda: NEXT, message_box=_Messages,
    ).run(PERSON, household_day=NOW.date())
    assert outcome.status is ComplaintStatus.FAILED and not outcome.completed
    assert len(session.appended) == 1 and len(session.appended[0].changes) == 2
    assert session.database.current_roster() is roster
