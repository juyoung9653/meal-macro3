"""Offscreen person-profile editor safety checks."""

from src.storage.database_session import AutomaticBackupResult

import os
from datetime import date
from uuid import UUID

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QMessageBox

from src.desktop_v2.person_edit import PersonProfileEditDialog
from src.domain.roster import (
    Person,
    PersonProfile,
)

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


def test_delivery_editor_validates_incomplete_values_and_reset_removes_override(app):
    from src.domain.roster import HouseholdDeliverySetting, Meal

    original = (HouseholdDeliverySetting(Meal.DINNER, (0, 1, 2, 3, 4), "도시락"),)
    dialog = PersonProfileEditDialog(_person(), household_delivery_settings=original)
    assert not dialog._save_button.isEnabled()
    assert "별도 설정" in dialog._delivery_toggle.text()
    dialog._delivery_toggle.click()
    dinner_days = dialog._delivery_days[Meal.DINNER]
    for day in dinner_days:
        day.setChecked(False)
    assert not dialog._save_button.isEnabled()
    assert "요일" in dialog._form_error.text()
    dialog._delivery_toggle.click()  # invalid input may still be collapsed and reopened
    dialog._delivery_toggle.click()
    dialog._reset_delivery_settings()
    assert dialog._save_button.isEnabled()
    dialog._save_button.click()
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.delivery_settings() == ()


def test_delivery_editor_accepts_arbitrary_form_and_cancel_keeps_original(app):
    from src.domain.roster import Meal

    dialog = PersonProfileEditDialog(_person(), household_delivery_settings=())
    assert not dialog._save_button.isEnabled()
    dialog._delivery_forms[Meal.LUNCH].setCurrentText("  보냉 도시락  ")
    assert dialog._save_button.isEnabled()
    assert dialog.delivery_settings()[0].form == "보냉 도시락"
    dialog.reject()
    assert dialog.result() == QDialog.DialogCode.Rejected


def test_delivery_editor_maps_service_days_to_physical_days_and_resets(app):
    from src.domain.roster import HouseholdDeliverySetting, Meal

    existing = (HouseholdDeliverySetting(Meal.BREAKFAST, delivery_weekdays=(0, 1, 2, 3, 4, 3, 4)),)
    dialog = PersonProfileEditDialog(_person(), household_delivery_settings=existing)
    breakfast = dialog._physical_delivery_days[Meal.BREAKFAST]
    assert tuple(day.currentIndex() for day in breakfast) == (0, 1, 2, 3, 4, 3, 4)
    assert [day.isChecked() for day in dialog._delivery_days[Meal.BREAKFAST]] == [False, False, False, False, True, True, True]
    assert not dialog._save_button.isEnabled()
    assert "별도 설정" in dialog._delivery_status[Meal.BREAKFAST].text()
    breakfast[6].setCurrentIndex(3)
    assert dialog._save_button.isEnabled()
    assert dialog.delivery_settings()[0].delivery_weekdays == (0, 1, 2, 3, 4, 3, 3)
    dialog._reset_delivery_settings()
    assert dialog.delivery_settings() == ()
    dialog.reject()
    assert dialog.result() == QDialog.DialogCode.Rejected


def test_delivery_weekday_default_is_inherited_until_edited(app):
    from src.domain.roster import Meal

    dialog = PersonProfileEditDialog(_person(), household_delivery_settings=())
    assert dialog._physical_delivery_days[Meal.BREAKFAST][5].isEnabled()
    assert dialog.delivery_settings() is None
    assert not dialog._save_button.isEnabled()
    dialog._physical_delivery_days[Meal.BREAKFAST][5].setCurrentIndex(3)
    assert "저장할 변경" in dialog._delivery_status[Meal.BREAKFAST].text()
    assert dialog.delivery_settings()[0].delivery_weekdays == (0, 1, 2, 3, 4, 3, 4)
    dialog._physical_delivery_days[Meal.BREAKFAST][5].setCurrentIndex(4)
    assert dialog.delivery_settings() is None
    assert not dialog._save_button.isEnabled()


def test_delivery_reset_displays_inherited_weekdays(app):
    from src.domain.roster import HouseholdDeliverySetting, Meal

    original = (HouseholdDeliverySetting(Meal.BREAKFAST, tuple(range(7)), delivery_weekdays=(0, 1, 2, 3, 4, 3, 3)),)
    dialog = PersonProfileEditDialog(_person(), household_delivery_settings=original)
    assert all(day.isChecked() for day in dialog._delivery_days[Meal.BREAKFAST])
    dialog._reset_delivery_meal(Meal.BREAKFAST)
    assert [day.isChecked() for day in dialog._delivery_days[Meal.BREAKFAST]] == [False, False, False, False, True, True, True]
    assert tuple(day.currentIndex() for day in dialog._physical_delivery_days[Meal.BREAKFAST]) == (0, 1, 2, 3, 4, 4, 4)
    assert dialog.delivery_settings() == ()
    assert [day.isChecked() for day in dialog._delivery_days[Meal.BREAKFAST]] == [False, False, False, False, True, True, True]
    dialog._reset_delivery_settings()
    assert dialog.delivery_settings() == ()
    assert dialog._save_button.isEnabled()


def test_delivery_only_scope_defaults_and_no_profile_controls(app):
    from uuid import uuid4

    from src.domain.roster import Meal

    person = _person()
    sibling = Person(uuid4(), PersonProfile("가족"))
    dialog = PersonProfileEditDialog(
        person, household_members=(person, sibling),
        household_delivery_settings=(), delivery_only=True,
    )
    assert dialog.windowTitle() == "홍길동 배송 설정"
    assert not dialog._editors
    assert not hasattr(dialog, "household_mobile")
    assert not dialog.household_delivery.isChecked()
    assert "1명" in dialog._delivery_scope.text()
    dialog.household_delivery.setChecked(True)
    assert "2명" in dialog._delivery_scope.text() and "가족" in dialog._delivery_scope.text()
    breakfast = Meal.BREAKFAST
    assert tuple(index for index, day in enumerate(dialog._delivery_days[breakfast]) if day.isChecked()) == (4, 5, 6)
    assert tuple(day.currentIndex() for day in dialog._physical_delivery_days[breakfast]) == (0, 1, 2, 3, 4, 4, 4)
    assert dialog._delivery_status[breakfast].text() == "기본값"
    assert not dialog._save_button.isEnabled()
    dialog._delivery_forms[breakfast].setCurrentText("식재료 박스")
    assert dialog._save_button.isEnabled()
    assert dialog.delivery_settings()[0].form == "식재료 박스"


def test_default_equivalent_legacy_setting_remains_on_noop_and_reset_clears(app):
    from src.domain.roster import HouseholdDeliverySetting, Meal

    original = (HouseholdDeliverySetting(
        Meal.BREAKFAST, (4, 5, 6), delivery_weekdays=(0, 1, 2, 3, 4, 4, 4)
    ),)
    dialog = PersonProfileEditDialog(
        _person(), household_delivery_settings=original, delivery_only=True,
    )
    assert dialog.delivery_settings() is None
    assert dialog._delivery_status[Meal.BREAKFAST].text() == "기본값"
    assert not dialog._save_button.isEnabled()
    dialog._delivery_days[Meal.BREAKFAST][0].setChecked(True)
    dialog._delivery_days[Meal.BREAKFAST][0].setChecked(False)
    assert dialog.delivery_settings() is None
    assert not dialog._save_button.isEnabled()
    dialog._reset_delivery_meal(Meal.BREAKFAST)
    assert dialog.delivery_settings() == ()
    assert dialog._save_button.isEnabled()


def test_person_without_household_can_edit_delivery_and_profile_copy_is_separate(app):
    from src.domain.roster import Meal
    from uuid import uuid4

    person = _person()
    standalone = PersonProfileEditDialog(person, delivery_only=True)
    assert standalone._delivery_forms[Meal.DINNER].isEnabled()
    assert not standalone.household_delivery.isEnabled()
    standalone._delivery_forms[Meal.DINNER].setCurrentText("도시락")
    assert standalone._save_button.isEnabled()

    sibling = Person(uuid4(), PersonProfile("가족"))
    profile = PersonProfileEditDialog(person, household_members=(person, sibling))
    assert not profile.household_mobile.isChecked()
    assert not profile.household_delivery.isChecked()
    profile.household_delivery.setChecked(True)
    assert not profile.household_mobile.isChecked()


def test_delivery_copy_can_save_unchanged_person_when_sibling_differs(app):
    from src.domain.roster import HouseholdDeliverySetting, Meal
    from uuid import uuid4

    original = (HouseholdDeliverySetting(Meal.DINNER, form="도시락"),)
    person = _person()
    sibling = Person(uuid4(), PersonProfile("가족"))
    dialog = PersonProfileEditDialog(
        person, delivery_only=True, household_members=(person, sibling),
        effective_delivery_settings=original,
        member_delivery_settings={person.person_id: original, sibling.person_id: ()},
    )
    assert dialog.delivery_settings() is None
    assert not dialog._save_button.isEnabled()
    dialog.household_delivery.setChecked(True)
    assert dialog._save_button.isEnabled()
    dialog._save_button.click()
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.apply_delivery_to_household()


def test_delivery_controller_copy_unchanged_inherited_person_to_sibling(app):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from uuid import uuid4

    from src.desktop_v2.person_edit import PersonProfileEditController
    from src.domain.roster import (
        Household,
        HouseholdMembership,
        RosterSnapshot,
    )
    from src.domain.revisions import RevisionActor
    from src.domain.roster import HouseholdDeliverySetting, Meal
    from src.storage.database_session import DatabaseSession

    setting = HouseholdDeliverySetting(Meal.DINNER, form="도시락")
    person = Person(uuid4(), PersonProfile("본인"))
    sibling = Person(uuid4(), PersonProfile("가족"), delivery_settings=())
    household = Household(uuid4(), delivery_settings=(setting,))
    roster = RosterSnapshot(
        uuid4(), persons=(person, sibling), households=(household,),
        memberships=tuple(HouseholdMembership(member.person_id, household.household_id)
                          for member in (person, sibling)),
    )
    records = []
    session = Mock(spec=DatabaseSession)
    session.database = SimpleNamespace(current_roster=lambda: roster)
    session.append_revision = lambda record: (records.append(record) or AutomaticBackupResult(error=None))

    class Dialog:
        def __init__(self, *args, **kwargs):
            pass
        def exec(self):
            return QDialog.DialogCode.Accepted
        def profile(self):
            return person.profile
        def delivery_settings(self):
            return None
        def apply_delivery_to_household(self):
            return True

    outcome = PersonProfileEditController(
        session, dialog_type=Dialog, actor_provider=lambda: RevisionActor("tester", "test"),
    ).run_delivery_settings(person.person_id)
    assert outcome.completed
    assert len(records) == 1
    assert outcome.roster.person(sibling.person_id).delivery_settings == (setting,)
    assert outcome.roster.person(person.person_id).delivery_settings == (setting,)


def test_controller_saves_household_phone_changes_in_one_revision(app):
    from dataclasses import replace
    from types import SimpleNamespace
    from uuid import uuid4

    from src.desktop_v2.person_edit import PersonProfileEditController
    from src.domain.roster import (
        Household,
        HouseholdMembership,
        RosterSnapshot,
    )
    from src.domain.revisions import RevisionActor

    person = Person(uuid4(), PersonProfile("본인", mobile_phone="010-1111-1111"))
    sibling = Person(uuid4(), PersonProfile("가족", mobile_phone="010-2222-2222"))
    household = Household(uuid4())
    roster = RosterSnapshot(uuid4(), persons=(person, sibling), households=(household,),
        memberships=tuple(HouseholdMembership(member.person_id, household.household_id)
                          for member in (person, sibling)))
    records = []
    def append(record):
        records.append(record)
        return AutomaticBackupResult(error=None)
    from unittest.mock import Mock

    from src.storage.database_session import DatabaseSession

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


def test_delivery_controller_changes_one_person_and_accepted_noop_skips_revision(app):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from uuid import uuid4

    from src.desktop_v2.person_edit import PersonProfileEditController, PersonProfileEditStatus
    from src.domain.roster import (
        Household,
        HouseholdMembership,
        RosterSnapshot,
    )
    from src.domain.revisions import RevisionActor
    from src.domain.roster import HouseholdDeliverySetting, Meal
    from src.storage.database_session import DatabaseSession

    person = Person(uuid4(), PersonProfile("본인"))
    sibling = Person(uuid4(), PersonProfile("가족"))
    other = Person(uuid4(), PersonProfile("다른 가구"))
    household = Household(uuid4())
    other_household = Household(uuid4())
    roster = RosterSnapshot(
        uuid4(), persons=(person, sibling, other), households=(household, other_household),
        memberships=(
            HouseholdMembership(person.person_id, household.household_id),
            HouseholdMembership(sibling.person_id, household.household_id),
            HouseholdMembership(other.person_id, other_household.household_id),
        ),
    )
    records = []
    session = Mock(spec=DatabaseSession)
    session.database = SimpleNamespace(current_roster=lambda: roster)
    session.append_revision = lambda record: (records.append(record) or AutomaticBackupResult(error=None))
    setting = HouseholdDeliverySetting(Meal.BREAKFAST, (4, 5, 6), "도시락")

    class Dialog:
        candidate = (setting,)

        def __init__(self, *args, **kwargs):
            pass

        def exec(self):
            return QDialog.DialogCode.Accepted

        def profile(self):
            return person.profile

        def delivery_settings(self):
            return self.candidate

    controller = PersonProfileEditController(
        session, dialog_type=Dialog, actor_provider=lambda: RevisionActor("tester", "test"),
    )
    Dialog.candidate = None
    assert controller.run_delivery_settings(person.person_id).status is PersonProfileEditStatus.CANCELLED
    assert not records
    Dialog.candidate = (setting,)
    outcome = controller.run_delivery_settings(person.person_id)
    assert outcome.completed
    assert len(records) == 1
    assert outcome.roster.delivery_settings_for(person.person_id, on=date.today()) == (setting,)
    assert outcome.roster.delivery_settings_for(sibling.person_id, on=date.today()) == ()
    assert outcome.roster.delivery_settings_for(other.person_id, on=date.today()) == ()
    assert outcome.roster.person(person.person_id).delivery_settings == (setting,)


def test_delivery_controller_copies_to_selected_day_household_when_membership_changed(app):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from uuid import uuid4

    from src.desktop_v2.person_edit import PersonProfileEditController
    from src.domain.roster import (
        Household,
        HouseholdMembership,
        RosterSnapshot,
    )
    from src.domain.revisions import RevisionActor
    from src.domain.roster import HouseholdDeliverySetting, InclusiveDateRange, Meal
    from src.storage.database_session import DatabaseSession

    selected_day = date(2026, 9, 1)
    current_day = date(2026, 10, 1)
    person = Person(uuid4(), PersonProfile("이동한 사람"))
    old_sibling = Person(uuid4(), PersonProfile("지난달 가구원"))
    new_sibling = Person(uuid4(), PersonProfile("현재 가구원"))
    old_household = Household(uuid4())
    new_household = Household(uuid4())
    roster = RosterSnapshot(
        uuid4(), persons=(person, old_sibling, new_sibling),
        households=(old_household, new_household),
        memberships=(
            HouseholdMembership(person.person_id, old_household.household_id,
                                InclusiveDateRange(ends_on=date(2026, 9, 30))),
            HouseholdMembership(person.person_id, new_household.household_id,
                                InclusiveDateRange(starts_on=current_day)),
            HouseholdMembership(old_sibling.person_id, old_household.household_id),
            HouseholdMembership(new_sibling.person_id, new_household.household_id),
        ),
    )
    records = []
    session = Mock(spec=DatabaseSession)
    session.database = SimpleNamespace(current_roster=lambda: roster)
    session.append_revision = lambda record: (records.append(record) or AutomaticBackupResult(error=None))
    setting = HouseholdDeliverySetting(Meal.DINNER, form="도시락")

    class Dialog:
        def __init__(self, *args, **kwargs):
            pass

        def exec(self):
            return QDialog.DialogCode.Accepted

        def profile(self):
            return person.profile

        def delivery_settings(self):
            return (setting,)

        def apply_delivery_to_household(self):
            return True

    outcome = PersonProfileEditController(
        session, dialog_type=Dialog, actor_provider=lambda: RevisionActor("tester", "test"),
        effective_date_provider=lambda: current_day,
    ).run_delivery_settings(person.person_id, service_day=selected_day)
    assert outcome.completed
    assert len(records) == 1
    assert records[0].metadata.effective_on == selected_day
    assert outcome.roster.delivery_settings_for(person.person_id, on=selected_day) == (setting,)
    assert outcome.roster.delivery_settings_for(old_sibling.person_id, on=selected_day) == (setting,)
    assert outcome.roster.delivery_settings_for(new_sibling.person_id, on=selected_day) == ()
    assert outcome.roster.person(new_sibling.person_id).delivery_settings is None
