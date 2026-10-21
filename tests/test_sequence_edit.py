"""Meal-specific sequence edit and encrypted roster persistence."""

import os
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QDialog

from src.desktop_v2.roster_window import RosterTableModel, RosterWindow
from src.desktop_v2.sequence_edit import SequenceEditController, SequenceEditDialog
from src.domain.roster import DeliveryAssignment, Meal
from src.domain.revisions import RevisionActor
from src.domain.sequence_edits import build_sequence_revision
from src.domain.sequence_settings import (
    SequenceMode,
    build_sequence_mode_revision,
)
from src.storage.database_session import DatabaseSession
from tests.test_delivery_routing import base_roster, initialize


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _mixed_roster():
    roster = base_roster()
    person_id = roster.persons[0].person_id
    return replace(roster, assignments=(
        *roster.assignments,
        DeliveryAssignment(person_id, Meal.BREAKFAST, "조식 기사", "010"),
    ))


def test_sequence_revision_roundtrips_per_meal_and_preserves_driver(tmp_path):
    roster = _mixed_roster()
    person_id = roster.persons[0].person_id
    prepared = build_sequence_revision(
        roster, person_id,
        {Meal.DINNER: "001", Meal.BREAKFAST: " 021 ", Meal.LUNCH: "034"},
        actor=RevisionActor("tester", "test-sid"),
        recorded_at=datetime.now(UTC), revision_id=uuid4(),
    )
    assert prepared.record.metadata.effective_on is None
    assert len(prepared.record.changes) == 2
    assert prepared.snapshot.resolve_assignment(person_id, Meal.DINNER).sequence == "001"
    assert prepared.snapshot.resolve_assignment(person_id, Meal.BREAKFAST).driver == "조식 기사"
    assert prepared.snapshot.resolve_assignment(person_id, Meal.LUNCH).driver == "기존 기사"
    path = tmp_path / "sequence.sqlite3"
    with DatabaseSession.create(path, "password", "recovery") as session:
        initialize(session, roster)
        session.append_revision(prepared.record)
        saved = session.database.current_roster()
        assert saved.resolve_assignment(person_id, Meal.BREAKFAST).sequence == "021"
        assert saved.resolve_assignment(person_id, Meal.LUNCH).sequence == "034"
        assert saved.resolve_assignment(person_id, Meal.DINNER).sequence == "001"
        assert saved.resolve_assignment(person_id, Meal.LUNCH).driver == "기존 기사"
        assert saved.resolve_assignment(person_id, Meal.BREAKFAST).driver == "조식 기사"
    with DatabaseSession.open(path, "password") as session:
        reopened = session.database.current_roster()
        assert reopened.resolve_assignment(person_id, Meal.LUNCH).sequence == "034"
        assert reopened.resolve_assignment(person_id, Meal.BREAKFAST).driver == "조식 기사"


def test_clear_common_sequence_preserves_other_meals_and_driver_after_reopen(tmp_path):
    roster = base_roster()
    person_id = roster.persons[0].person_id
    prepared = build_sequence_revision(
        roster, person_id, {Meal.DINNER: ""},
        actor=RevisionActor("tester", "test-sid"),
        recorded_at=datetime.now(UTC), revision_id=uuid4(),
    )
    saved = prepared.snapshot
    assert saved.resolve_assignment(person_id, Meal.DINNER).sequence is None
    assert saved.resolve_assignment(person_id, Meal.BREAKFAST).sequence == "001"
    assert saved.resolve_assignment(person_id, Meal.LUNCH).sequence == "001"
    assert all(saved.resolve_assignment(person_id, meal).driver == "기존 기사" for meal in Meal)
    path = tmp_path / "clear-sequence.sqlite3"
    with DatabaseSession.create(path, "password", "recovery") as session:
        initialize(session, roster)
        session.append_revision(prepared.record)
        assert session.database.current_roster().resolve_assignment(person_id, Meal.DINNER).sequence is None
    with DatabaseSession.open(path, "password") as session:
        saved = session.database.current_roster()
        assert saved.resolve_assignment(person_id, Meal.DINNER).sequence is None
        assert saved.resolve_assignment(person_id, Meal.BREAKFAST).sequence == "001"
        assert saved.resolve_assignment(person_id, Meal.LUNCH).sequence == "001"
        assert all(saved.resolve_assignment(person_id, meal).driver == "기존 기사" for meal in Meal)


def test_clear_sequence_only_common_assignment_removes_empty_record():
    roster = base_roster()
    person_id = roster.persons[0].person_id
    roster = replace(roster, assignments=(DeliveryAssignment(person_id, sequence="001"),))
    prepared = build_sequence_revision(
        roster, person_id, {Meal.DINNER: ""},
        actor=RevisionActor("tester", "test-sid"),
        recorded_at=datetime.now(UTC), revision_id=uuid4(),
    )
    assert all(assignment.meal is not None for assignment in prepared.snapshot.assignments)
    assert prepared.snapshot.resolve_assignment(person_id, Meal.DINNER).sequence is None
    assert prepared.snapshot.resolve_assignment(person_id, Meal.BREAKFAST).sequence == "001"
    assert prepared.snapshot.resolve_assignment(person_id, Meal.LUNCH).sequence == "001"


def test_sequence_dialog_accepts_changed_empty_and_controller_skips_cancel_noop(app, tmp_path):
    roster = _mixed_roster()
    person_id = roster.persons[0].person_id
    dialog = SequenceEditDialog(roster, person_id)
    assert dialog.fields[Meal.LUNCH].text() == "001"
    assert dialog.fields[Meal.BREAKFAST].text() == "010"
    dialog.fields[Meal.LUNCH].clear()
    dialog._save()
    assert dialog.result() == QDialog.DialogCode.Accepted
    dialog.close()

    class StubDialog:
        accepted = False
        lunch_value = None
        def __init__(self, roster, person_id, parent):
            self.roster = roster
            self.person_id = person_id
        def exec(self):
            return QDialog.DialogCode.Accepted if self.accepted else QDialog.DialogCode.Rejected
        def values(self):
            values = {meal: self.roster.resolve_assignment(self.person_id, meal).sequence or "" for meal in Meal}
            if self.lunch_value is not None:
                values[Meal.LUNCH] = self.lunch_value
            return values

    with DatabaseSession.create(tmp_path / "noop.sqlite3", "password", "recovery") as session:
        initialize(session, roster)
        controller = SequenceEditController(
            session, dialog_type=StubDialog,
            actor_provider=lambda: RevisionActor("tester", "test-sid"),
        )
        assert controller.run(person_id) is None
        StubDialog.accepted = True
        assert controller.run(person_id) is None
        assert session.database.current_roster().revision_id == roster.revision_id
        StubDialog.lunch_value = "055"
        saved = controller.run(person_id)
        assert saved.resolve_assignment(person_id, Meal.LUNCH).sequence == "055"
        assert saved.resolve_assignment(person_id, Meal.BREAKFAST).sequence == "010"
        assert session.database.current_roster().revision_id == saved.revision_id


def test_unified_dialog_keeps_mixed_values_distinguishable_until_explicit_input(app):
    roster = _mixed_roster()
    person_id = roster.persons[0].person_id
    dialog = SequenceEditDialog(roster, person_id, mode=SequenceMode.UNIFIED)
    assert dialog.fields[Meal.DINNER] is dialog.fields[Meal.LUNCH]
    assert dialog.fields[Meal.DINNER].text() == ""
    assert dialog.values() == {}
    assert "석식: 001" in dialog.findChild(type(dialog.error), "sequenceMixedValues").text()
    assert "조식: 010" in dialog.findChild(type(dialog.error), "sequenceMixedValues").text()
    dialog._save()
    assert dialog.result() == QDialog.DialogCode.Accepted

    dialog = SequenceEditDialog(roster, person_id, mode=SequenceMode.UNIFIED)
    dialog.fields[Meal.DINNER].setText("007")
    assert dialog.values() == {meal: "007" for meal in (Meal.DINNER, Meal.BREAKFAST, Meal.LUNCH)}
    dialog.close()

    dialog = SequenceEditDialog(roster, person_id, mode=SequenceMode.UNIFIED)
    dialog.clear_button.click()
    assert dialog.values() == {meal: "" for meal in Meal}
    dialog.close()


def test_unified_controller_saves_all_meals_once_and_preserves_drivers(tmp_path):
    roster = _mixed_roster()
    person_id = roster.persons[0].person_id
    actor = RevisionActor("tester", "test-sid")
    path = tmp_path / "unified.sqlite3"
    with DatabaseSession.create(path, "password", "recovery") as session:
        initialize(session, roster)
        current = session.database.current_roster()
        mode_revision = build_sequence_mode_revision(
            current.revision_id,
            None,
            SequenceMode.UNIFIED,
            actor=actor,
            recorded_at=datetime.now(UTC),
            revision_id=uuid4(),
        )
        session.append_revision(mode_revision.record)
        before = session.database.current_roster().revision_id

        class Dialog:
            def __init__(self, _roster, _person_id, _parent, *, mode):
                assert mode is SequenceMode.UNIFIED

            def exec(self):
                return QDialog.DialogCode.Accepted

            def values(self):
                return {meal: "0007" for meal in (Meal.DINNER, Meal.BREAKFAST, Meal.LUNCH)}

        saved = SequenceEditController(
            session,
            dialog_type=Dialog,
            actor_provider=lambda: actor,
        ).run(person_id)
        assert saved is not None and saved.revision_id != before
        assert {
            saved.resolve_assignment(person_id, meal).sequence for meal in Meal
        } == {"0007"}
        assert saved.resolve_assignment(person_id, Meal.BREAKFAST).driver == "조식 기사"
        assert saved.resolve_assignment(person_id, Meal.LUNCH).driver == "기존 기사"


def test_sequence_display_and_context_menu_signal(app):
    roster = _mixed_roster()
    person_id = roster.persons[0].person_id
    common_model = RosterTableModel(base_roster())
    common_sequence_column = next(
        i for i, column in enumerate(common_model.columns) if column.key == "sequence"
    )
    assert common_model.data(common_model.index(0, common_sequence_column)) == "석식: 001\n조식: 001\n중식: 001"
    model = RosterTableModel(roster)
    sequence_column = next(i for i, column in enumerate(model.columns) if column.key == "sequence")
    index = model.index(0, sequence_column)
    assert model.data(index) == "석식: 001\n조식: 010\n중식: 001"
    assert "더블클릭 → 순서 수정" in model.data(index, Qt.ItemDataRole.ToolTipRole)
    window = RosterWindow(roster)
    received = []
    person_edits = []
    window.sequence_edit_requested.connect(received.append)
    window.person_edit_requested.connect(person_edits.append)
    sequence_menu = window._create_context_menu(roster.persons[0], column_key="sequence")
    assert [action.text() for action in sequence_menu.actions()] == [
        "순서 수정", "이력보기", "민원", "", "명단 행 삭제",
    ]
    sequence_menu.actions()[0].trigger()
    assert received == [person_id]
    assert person_edits == []
    window._table.doubleClicked.emit(window.table_model.index(0, sequence_column))
    assert received == [person_id, person_id]
    name_menu = window._create_context_menu(roster.persons[0], column_key="name")
    assert [action.text() for action in name_menu.actions()] == [
        "정보 수정", "이력보기", "민원", "", "명단 행 삭제",
    ]
    name_menu.actions()[0].trigger()
    assert person_edits == [person_id]
    assert window._table.verticalHeader().defaultSectionSize() >= 76
    window.close()
