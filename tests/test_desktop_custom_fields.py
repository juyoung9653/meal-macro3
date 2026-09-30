from datetime import UTC, date, datetime
from types import SimpleNamespace
from uuid import UUID

import pytest
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from src.desktop_v2.custom_fields import (
    CustomFieldAction,
    CustomFieldController,
    CustomFieldRequest,
    CustomFieldsDialog,
)
from src.desktop_v2.person_edit import PersonProfileEditDialog
from src.domain.roster import (
    CustomFieldDefinition,
    CustomFieldType,
    Person,
    PersonProfile,
    RosterSnapshot,
)
from src.domain.revisions import RevisionActor


REVISION_ID = UUID("c1000000-0000-0000-0000-000000000001")
PERSON_ID = UUID("c1000000-0000-0000-0000-000000000002")
NUMBER_ID = UUID("c1000000-0000-0000-0000-000000000003")


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _roster() -> RosterSnapshot:
    definition = CustomFieldDefinition(
        NUMBER_ID,
        "횟수",
        CustomFieldType.NUMBER,
        0,
    )
    return RosterSnapshot(
        REVISION_ID,
        persons=(
            Person(PERSON_ID, PersonProfile("홍길동", custom_values={NUMBER_ID: 0})),
        ),
        custom_fields=(definition,),
    )


class _Database:
    def __init__(self, roster: RosterSnapshot):
        self.roster = roster

    def current_roster(self):
        return self.roster


class _Session:
    def __init__(
        self, roster: RosterSnapshot, *, error=None, raises: Exception | None = None
    ):
        self.database = _Database(roster)
        self.error = error
        self.raises = raises
        self.records = []

    def append_revision(self, record):
        if self.raises is not None:
            raise self.raises
        self.records.append(record)
        return SimpleNamespace(error=self.error)


class _Dialog:
    def __init__(self, _definitions, _parent=None, **_kwargs):
        self._result, self._request = _Dialog.responses.pop(0)

    def exec(self):
        return self._result

    def request(self):
        return self._request


class _Messages:
    def __init__(self, answer=QMessageBox.StandardButton.No):
        self.answer = answer
        self.critical_calls = []
        self.warning_calls = []

    def critical(self, *args):
        self.critical_calls.append(args)

    def warning(self, *args):
        self.warning_calls.append(args)

    def question(self, *_args):
        return self.answer


def _controller(session, messages):
    return CustomFieldController(
        session,
        actor_provider=lambda: RevisionActor("tester", "S-1-5-21-test"),
        dialog_type=_Dialog,
        message_box=messages,
        recorded_at_provider=lambda: datetime(2026, 8, 26, 12, tzinfo=UTC),
        effective_date_provider=lambda: date(2026, 8, 26),
        revision_id_factory=_revision_ids(),
        definition_id_factory=_definition_ids(),
    )


def _revision_ids():
    values = iter(
        (
            UUID("c1000000-0000-0000-0000-000000000010"),
            UUID("c1000000-0000-0000-0000-000000000011"),
        )
    )
    return lambda: next(values)


def _definition_ids():
    values = iter(
        (
            UUID("c1000000-0000-0000-0000-000000000020"),
            UUID("c1000000-0000-0000-0000-000000000021"),
        )
    )
    return lambda: next(values)


def test_controller_returns_latest_roster_after_multiple_commits_then_close():
    _Dialog.responses = [
        (
            QDialog.DialogCode.Accepted,
            CustomFieldRequest(CustomFieldAction.CREATE, "구역", CustomFieldType.TEXT),
        ),
        (
            QDialog.DialogCode.Accepted,
            CustomFieldRequest(CustomFieldAction.CREATE, "요일", CustomFieldType.TEXT),
        ),
        (QDialog.DialogCode.Rejected, None),
    ]
    session = _Session(_roster())
    outcome = _controller(session, _Messages()).run()

    assert outcome.completed
    assert outcome.roster is not None
    assert [field.name for field in outcome.roster.custom_fields] == [
        "횟수",
        "구역",
        "요일",
    ]
    assert len(session.records) == 2


def test_controller_warns_for_backup_error_but_returns_committed_snapshot():
    _Dialog.responses = [
        (
            QDialog.DialogCode.Accepted,
            CustomFieldRequest(CustomFieldAction.CREATE, "구역", CustomFieldType.TEXT),
        ),
        (QDialog.DialogCode.Rejected, None),
    ]
    messages = _Messages()
    outcome = _controller(_Session(_roster(), error=OSError("offline")), messages).run()

    assert outcome.completed
    assert outcome.roster is not None
    assert outcome.roster.custom_fields[-1].name == "구역"
    assert len(messages.warning_calls) == 1


def test_controller_returns_no_change_when_append_fails():
    _Dialog.responses = [
        (
            QDialog.DialogCode.Accepted,
            CustomFieldRequest(CustomFieldAction.CREATE, "구역", CustomFieldType.TEXT),
        ),
        (QDialog.DialogCode.Rejected, None),
    ]
    messages = _Messages()
    outcome = _controller(
        _Session(_roster(), raises=RuntimeError("write failed")), messages
    ).run()

    assert not outcome.completed
    assert outcome.roster is None
    assert len(messages.critical_calls) == 1


def test_delete_confirmation_no_does_not_request_or_commit(app):
    dialog = CustomFieldsDialog(_roster().custom_fields, message_box=_Messages())
    dialog._list.setCurrentRow(0)
    dialog._delete()

    assert dialog.result() == QDialog.DialogCode.Rejected
    assert dialog._request is None


def test_profile_editor_preserves_zero_and_float_custom_values(app):
    roster = _roster()
    dialog = PersonProfileEditDialog(
        roster.person(PERSON_ID),
        custom_fields=roster.custom_fields,
    )
    editor = dialog._editors[f"custom:{NUMBER_ID}"]
    assert editor.text() == "0"
    editor.setText("1.5")

    assert dialog._candidate_profile().custom_values[NUMBER_ID] == 1.5


def test_profile_editor_hides_provider_and_preserves_legacy_value(app):
    person = Person(PERSON_ID, PersonProfile("홍길동", provider="기존기관"))
    dialog = PersonProfileEditDialog(person)

    assert "provider" not in dialog._editors
    assert dialog._candidate_profile().provider == "기존기관"


@pytest.mark.parametrize("field_type", tuple(CustomFieldType))
def test_real_add_form_converts_qt_data_to_domain_type(app, field_type):
    dialog = CustomFieldsDialog(())
    dialog._name.setText("추가 안내")
    dialog._type.setCurrentIndex(tuple(CustomFieldType).index(field_type))
    app.processEvents()
    assert dialog._choices.isEnabled() == (field_type is CustomFieldType.CHOICE)
    if field_type is CustomFieldType.CHOICE:
        dialog._choices.setText("오전, 오후")
    dialog._add()
    request = dialog.request()
    assert request.field_type is field_type
    assert request.choices == (("오전", "오후") if field_type is CustomFieldType.CHOICE else ())
    dialog.close()
