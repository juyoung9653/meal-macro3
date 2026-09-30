"""Offscreen person-profile editor safety checks."""

import os
from datetime import date
from uuid import UUID

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from src.desktop_v2 import PersonProfileEditDialog
from src.domain import Person, PersonProfile

PERSON_ID = UUID("86000000-0000-0000-0000-000000000001")
CUSTOM_FIELD_ID = UUID("86000000-0000-0000-0000-000000000002")


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _person() -> Person:
    return Person(
        PERSON_ID,
        PersonProfile(
            "홍길동",
            birth_date=date(2015, 3, 2),
            school_name="이전초",
            class_name="2반",
            mobile_phone="010-1234-5678",
            custom_values={CUSTOM_FIELD_ID: "A구역"},
        ),
    )


class _Questions:
    answer = QMessageBox.StandardButton.No
    calls: list[tuple] = []

    @classmethod
    def reset(cls):
        cls.answer = QMessageBox.StandardButton.No
        cls.calls = []

    @classmethod
    def question(cls, *args):
        cls.calls.append(args)
        return cls.answer


def test_clearing_existing_value_shows_notice_and_requires_confirmation(
    app: QApplication,
):
    _Questions.reset()
    dialog = PersonProfileEditDialog(_person(), message_box=_Questions)

    assert "class_name" not in dialog._editors
    assert not dialog._save_button.isEnabled()
    dialog._editors["school_name"].setText("새학교")
    dialog._editors["mobile_phone"].clear()
    app.processEvents()

    assert not dialog._clear_notice.isHidden()
    assert "핸드폰" in dialog._clear_notice.text()
    assert dialog._save_button.isEnabled()

    dialog._save_button.click()

    assert dialog.result() == QDialog.DialogCode.Rejected
    assert len(_Questions.calls) == 1
    assert "핸드폰" in _Questions.calls[0][2]

    _Questions.answer = QMessageBox.StandardButton.Yes
    dialog._save_button.click()

    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.profile().school_name == "새학교"
    assert dialog.profile().class_name == "2반"
    assert dialog.profile().mobile_phone is None
    assert dialog.profile().custom_values == {CUSTOM_FIELD_ID: "A구역"}


def test_required_name_and_birth_date_are_validated_before_save(
    app: QApplication,
):
    _Questions.reset()
    dialog = PersonProfileEditDialog(_person(), message_box=_Questions)

    dialog._editors["name"].clear()
    app.processEvents()
    assert not dialog._save_button.isEnabled()
    assert "성명" in dialog._form_error.text()

    dialog._editors["name"].setText("홍길순")
    dialog._editors["birth_date"].setText("2015/03/02")
    app.processEvents()
    assert not dialog._save_button.isEnabled()
    assert "2015-03-02" in dialog._form_error.text()

    dialog._editors["birth_date"].setText("20150302")
    app.processEvents()
    assert dialog._save_button.isEnabled()
    dialog._save_button.click()

    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.profile().name == "홍길순"
    assert _Questions.calls == []


def test_household_phone_copy_requires_confirmation_even_without_subject_edits(app):
    from uuid import uuid4

    _Questions.reset()
    person = _person()
    sibling = Person(uuid4(), PersonProfile("가족", mobile_phone="010-2222-2222"))
    dialog = PersonProfileEditDialog(person, message_box=_Questions, household_members=(person, sibling))
    assert not dialog.household_mobile.isChecked()
    assert not dialog._save_button.isEnabled()
    dialog.household_mobile.setChecked(True)
    assert dialog._save_button.isEnabled()
    dialog._save_button.click()
    assert dialog.result() == QDialog.DialogCode.Rejected
    assert "가족" in _Questions.calls[-1][2]
    _Questions.answer = QMessageBox.StandardButton.Yes
    dialog._save_button.click()
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.apply_mobile_to_household()
    assert dialog.profile() == person.profile


def test_household_phone_disabled_for_single_person(app):
    dialog = PersonProfileEditDialog(_person())
    assert not dialog.household_mobile.isEnabled()
    assert not dialog.apply_mobile_to_household()


def test_controller_saves_household_phone_changes_in_one_revision(app):
    from dataclasses import replace
    from types import SimpleNamespace
    from uuid import uuid4

    from src.desktop_v2.person_edit import PersonProfileEditController
    from src.domain import Household, HouseholdMembership, RevisionActor, RosterSnapshot

    person = Person(uuid4(), PersonProfile("본인", mobile_phone="010-1111-1111"))
    sibling = Person(uuid4(), PersonProfile("가족", mobile_phone="010-2222-2222"))
    household = Household(uuid4())
    roster = RosterSnapshot(uuid4(), persons=(person, sibling), households=(household,),
        memberships=tuple(HouseholdMembership(member.person_id, household.household_id)
                          for member in (person, sibling)))
    records = []
    def append(record):
        records.append(record)
        return SimpleNamespace(error=None)
    from unittest.mock import Mock

    from src.storage import DatabaseSession

    session = Mock(spec=DatabaseSession)
    session.database = SimpleNamespace(current_roster=lambda: roster)
    session.append_revision = append
    class Dialog:
        def __init__(self, *args, **kwargs):
            pass
        def exec(self):
            return QDialog.DialogCode.Accepted
        def profile(self):
            return replace(person.profile, mobile_phone="010-9999-9999")
        def apply_mobile_to_household(self):
            return True
    outcome = PersonProfileEditController(session, dialog_type=Dialog,
        actor_provider=lambda: RevisionActor("tester", "test")).run(person.person_id)
    assert outcome.completed
    assert len(records) == 1
    assert len(records[0].changes) == 2
    assert {member.profile.mobile_phone for member in outcome.roster.persons} == {"010-9999-9999"}
