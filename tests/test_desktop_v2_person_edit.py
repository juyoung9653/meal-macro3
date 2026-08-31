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
