"""Manual column constraints, editor formatting, and persisted rules."""

from datetime import UTC, date, datetime
from uuid import UUID

import pytest

from src.domain.column_input_rules import (
    RULES_ID, RULES_KIND, InputRule, build_rules_revision, load_rules,
    rule_for, validate_input, validate_profile_changes,
)
from src.domain.projections import roster_audit_changes
from src.domain.revisions import EntityRef, HistoricalSnapshot, RevisionActor, RevisionMetadata, RevisionRecord, RevisionSource
from src.domain.roster import Person, PersonProfile, RosterSnapshot
from src.storage.database_session import DatabaseSession


ACTOR = RevisionActor("tester", "test-sid")
BASE = UUID("fc300000-0000-0000-0000-000000000001")
NEXT = UUID("fc300000-0000-0000-0000-000000000002")
PERSON = UUID("fc300000-0000-0000-0000-000000000003")


def test_phone_date_length_and_untouched_legacy_values():
    for value in ("02-123-4567", "02-1234-5678", "031-123-4567", "010-1234-5678"):
        validate_input("mobile", value, InputRule("phone"))
    for value in ("031-1234", "2-1234-5678", "01012345678", "010-1234-567x", "xx-xxx-xxxx"):
        with pytest.raises(ValueError, match="전화번호"):
            validate_input("mobile", value, InputRule("phone"))
    validate_input("birth_date", "2024-02-29", InputRule("date"))
    for value in ("2025-02-29", "20250228", "2024-13-01"):
        with pytest.raises(ValueError, match="날짜"):
            validate_input("birth_date", value, InputRule("date"))
    with pytest.raises(ValueError, match="최대 3글자"):
        validate_input("name", "네글자임", InputRule("text", 3))
    original = PersonProfile("원래", mobile_phone="010-1")
    validate_profile_changes(original, PersonProfile("수정", mobile_phone="010-1"), {})
    with pytest.raises(ValueError, match="전화번호"):
        validate_profile_changes(original, PersonProfile("원래", mobile_phone="010-2"), {})


def test_custom_rules_use_definition_id_not_display_label():
    custom_id = UUID("fc300000-0000-0000-0000-000000000004")
    key = f"custom:{custom_id}"
    snapshot = HistoricalSnapshot(BASE, {
        EntityRef(RULES_KIND, RULES_ID): {
            "version": 1,
            "rules": {key: {"format": "phone", "max_length": 13}},
        }
    })
    assert rule_for(load_rules(snapshot), key) == InputRule("phone", 13)
    assert rule_for(load_rules(snapshot), "custom:renamed-label") == InputRule("text")


def test_rule_revision_reopen_and_default_reset(tmp_path):
    roster = RosterSnapshot(BASE, persons=(Person(PERSON, PersonProfile("대상")),))
    initial = RevisionRecord(
        RevisionMetadata(BASE, None, datetime(2026, 10, 21, tzinfo=UTC), None, ACTOR, "initial", RevisionSource.IMPORT),
        roster_audit_changes(RosterSnapshot(UUID(int=999)), roster),
    )
    path = tmp_path / "column-rules.sqlite3"
    with DatabaseSession.create(path, "password", "recovery") as session:
        session.append_revision(initial)
        snapshot = session.database.snapshot(BASE)
        assert rule_for(load_rules(snapshot), "mobile") == InputRule("phone")
        assert build_rules_revision(BASE, snapshot, "mobile", InputRule("phone"), actor=ACTOR, recorded_at=datetime.now(UTC), revision_id=NEXT) is None
        record = build_rules_revision(BASE, snapshot, "mobile", InputRule("text", 8), actor=ACTOR, recorded_at=datetime.now(UTC), revision_id=NEXT)
        assert record is not None and len(record.changes) == 1
        assert record.changes[0].entity_kind == RULES_KIND
        assert record.changes[0].entity_id == RULES_ID
        session.append_revision(record)
        assert session.database.current_roster().revision_id == NEXT
    with DatabaseSession.open(path, "password") as session:
        saved = session.database.snapshot(NEXT)
        assert rule_for(load_rules(saved), "mobile") == InputRule("text", 8)
        assert saved.entity(RULES_KIND, RULES_ID)["rules"]["mobile"]["max_length"] == 8


def test_settings_cancel_no_change_then_save_once(tmp_path, app):
    from PySide6.QtWidgets import QDialog
    from src.desktop_v2.column_input_rules import ColumnInputRuleController

    roster = RosterSnapshot(BASE, persons=(Person(PERSON, PersonProfile("대상")),))
    initial = RevisionRecord(
        RevisionMetadata(BASE, None, datetime(2026, 10, 21, tzinfo=UTC), None, ACTOR, "initial", RevisionSource.IMPORT),
        roster_audit_changes(RosterSnapshot(UUID(int=999)), roster),
    )

    class StubDialog:
        accepted = False
        selected = InputRule("phone")

        def __init__(self, label, rule, *, birth_date, parent):
            assert label == "핸드폰" and not birth_date
            self.current = rule

        def exec(self):
            return QDialog.DialogCode.Accepted if self.accepted else QDialog.DialogCode.Rejected

        def selected_rule(self):
            return self.selected

    with DatabaseSession.create(tmp_path / "setting.sqlite3", "password", "recovery") as session:
        session.append_revision(initial)
        controller = ColumnInputRuleController(session, dialog_type=StubDialog, actor_provider=lambda: ACTOR)
        assert controller.run("mobile", "핸드폰") is False
        assert session.database.current_roster().revision_id == BASE
        StubDialog.accepted = True
        assert controller.run("mobile", "핸드폰") is False
        assert session.database.current_roster().revision_id == BASE
        StubDialog.selected = InputRule("text", 12)
        assert controller.run("mobile", "핸드폰") is True
        changed_id = session.database.current_roster().revision_id
        assert changed_id != BASE
        assert rule_for(load_rules(session.database.snapshot(changed_id)), "mobile") == InputRule("text", 12)


def test_changed_invalid_cell_rejects_without_revision(app):
    from types import SimpleNamespace
    from src.desktop_v2.roster_cell_edit import RosterCellEditController, RosterCellEditStatus

    roster = RosterSnapshot(BASE, persons=(Person(PERSON, PersonProfile("대상", mobile_phone="010-1")),))
    records = []
    errors = []

    class Messages:
        @staticmethod
        def critical(_parent, _title, message):
            errors.append(message)

    class Session:
        database = SimpleNamespace(current_roster=lambda: roster)

        @staticmethod
        def append_revision(record):
            records.append(record)

    controller = RosterCellEditController(Session(), message_box=Messages)
    outcome = controller.save(PERSON, "mobile", "010-1234")
    assert outcome.status is RosterCellEditStatus.FAILED
    assert records == [] and "전화번호" in errors[0]
    assert roster.person(PERSON).profile.mobile_phone == "010-1"


def test_legacy_phone_cannot_be_copied_to_household_as_new_value(app):
    from src.desktop_v2.person_edit import PersonProfileEditDialog

    owner = Person(PERSON, PersonProfile("대상", mobile_phone="010-1"))
    sibling = Person(UUID(int=17), PersonProfile("가족", mobile_phone="010-2"))
    dialog = PersonProfileEditDialog(owner, household_members=(owner, sibling))
    assert not dialog._form_error.isVisible()
    dialog.household_mobile.setChecked(True)
    assert "전화번호" in dialog._form_error.text()
    assert not dialog._save_button.isEnabled()
    dialog.close()


def test_column_header_menu_has_one_rule_entry_for_nonmeal(app, monkeypatch):
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QAction
    from src.desktop_v2.roster_window import RosterWindow

    window = RosterWindow(RosterSnapshot(BASE, persons=(Person(PERSON, PersonProfile("대상")),)))
    seen = []

    class Menu:
        def __init__(self):
            self.actions = []

        def addSeparator(self):
            pass

        def addAction(self, text):
            action = QAction(text)
            self.actions.append(action)
            return action

        def exec(self, *_args):
            pass

    def create_menu(*_args, **_kwargs):
        menu = Menu()
        seen.append(menu)
        return menu

    monkeypatch.setattr(window._column_layout, "create_menu", create_menu)
    requested = []
    window.column_input_rule_requested.connect(requested.append)
    window._show_column_menu(QPoint(0, 0), column_key="name")
    assert [action.text() for action in seen[-1].actions].count("입력 제한 설정…") == 1
    next(action for action in seen[-1].actions if action.text() == "입력 제한 설정…").trigger()
    window._show_column_menu(QPoint(0, 0), column_key="sequence")
    next(action for action in seen[-1].actions if action.text() == "입력 제한 설정…").trigger()
    assert requested == ["name", "sequence"]
    window._show_column_menu(QPoint(0, 0), column_key="status:lunch")
    assert "입력 제한 설정…" not in [action.text() for action in seen[-1].actions]
    window.close()


def test_digit_editor_keeps_cursor_and_handles_paste(app):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from src.desktop_v2.column_input_rules import RuleLineEdit

    phone = RuleLineEdit(InputRule("phone"))
    phone.show()
    phone.setFocus()
    QTest.keyClicks(phone, "01012345678")
    assert phone.text() == "010-1234-5678"
    phone.setText("031-123-4567")  # Existing formatted values remain as loaded.
    phone.setCursorPosition(8)
    QTest.keyClick(phone, Qt.Key.Key_Backspace)
    assert phone.text() == "031-124-567"
    phone.selectAll()
    phone.insert("010-9999-8888")
    assert phone.text() == "010-9999-8888"
    phone.selectAll()
    QTest.keyClicks(phone, "021234567")
    assert phone.text() == "02-123-4567"
    QTest.keyClicks(phone, "8")
    assert phone.text() == "02-1234-5678"
    phone.selectAll()
    phone.insert("02-987-6543")
    assert phone.text() == "02-987-6543"
    phone.close()
    day = RuleLineEdit(InputRule("date"))
    day.show()
    day.setFocus()
    QTest.keyClicks(day, "20240229")
    assert day.text() == "2024-02-29"
    day.close()


def test_typed_date_and_number_rule_dialog_disables_incompatible_formats(app):
    from src.desktop_v2.column_input_rules import ColumnInputRuleDialog

    date_dialog = ColumnInputRuleDialog("등록일", InputRule(), allowed_formats=frozenset({"text", "date"}))
    assert not date_dialog.format.model().item(1).isEnabled()
    assert date_dialog.format.model().item(2).isEnabled()
    date_dialog.close()
    number_dialog = ColumnInputRuleDialog("수량", InputRule(), allowed_formats=frozenset({"text"}))
    assert not number_dialog.format.model().item(1).isEnabled()
    assert not number_dialog.format.model().item(2).isEnabled()
    number_dialog.close()


def test_birth_date_remains_digit_formatted_with_text_length_rule(app):
    from PySide6.QtTest import QTest
    from src.desktop_v2.column_input_rules import make_rule_editor

    editor = make_rule_editor("birth_date", {"birth_date": InputRule("text", 10)})
    editor.show()
    editor.setFocus()
    QTest.keyClicks(editor, "20150302")
    assert editor.text() == "2015-03-02"
    editor.close()


def test_sequence_dialog_validates_changed_value_without_rejecting_unchanged_legacy(app):
    from PySide6.QtWidgets import QDialog
    from src.desktop_v2.sequence_edit import SequenceEditDialog
    from src.domain.roster import DeliveryAssignment, Meal

    roster = RosterSnapshot(
        BASE,
        persons=(Person(PERSON, PersonProfile("대상")),),
        assignments=(DeliveryAssignment(PERSON, Meal.LUNCH, sequence="001"),),
    )
    dialog = SequenceEditDialog(roster, PERSON, input_rules={"sequence": InputRule("text", 2)})
    dialog.fields[Meal.DINNER].setText("02")
    dialog._save()
    assert dialog.result() == QDialog.DialogCode.Accepted  # Existing 001 remains untouched.
    dialog.close()
    dialog = SequenceEditDialog(roster, PERSON, input_rules={"sequence": InputRule("text", 2)})
    dialog.fields[Meal.LUNCH].setText("002")
    dialog._save()
    assert dialog.result() != QDialog.DialogCode.Accepted
    assert "최대 2글자" in dialog.error.text()
    dialog.close()


@pytest.fixture
def app():
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])
