"""Mandatory first-import and startup-gating controller tests."""

from __future__ import annotations

import hashlib
import os
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import openpyxl
import pytest
from PySide6.QtWidgets import QApplication, QDialog

from src.desktop_v2 import (
    DesktopStartupController,
    DocumentAction,
    GeneratedDocumentResult,
    DocumentMealSelection,
    DocumentOutputMode,
    DocumentPeriodSelection,
    GuidedImportOutcome,
    GuidedImportStatus,
    GuidedInitialImportController,
    GuidedRosterImportController,
    ImportWizardSelection,
    MealStatusChangeController,
    PersonProfileEditController,
    SchoolCalendarEditOutcome,
    SchoolCalendarEditStatus,
    StartupStatus,
    SettingsAction,
    next_available_document_output,
    suggest_document_filename,
)
from src.domain import (
    InclusiveDateRange,
    Meal,
    MealStatus,
    RevisionActor,
    RevisionSummary,
    RevisionSource,
)
from src.domain.revisions import format_revision_timestamp
from src.importing import (
    HouseholdMatchingRule,
    IdentityResolution,
    ImportMappingProfile,
    ProfileField,
    ReconciliationDecision,
    StoredHouseholdMatchingRule,
    StoredImportProfile,
    household_matching_rules_from_snapshot,
    import_profiles_from_snapshot,
    parse_workbook,
)
from src.storage import AutomaticBackupResult, DatabaseSession


PERSON_ID = UUID("84000000-0000-0000-0000-000000000001")
PROFILE_ID = UUID("84000000-0000-0000-0000-000000000002")
HOUSEHOLD_RULE_ID = UUID("84000000-0000-0000-0000-000000000003")
LATER_PERSON_ID = UUID("84000000-0000-0000-0000-000000000004")
PROFILE_EDIT_REVISION_ID = UUID("84000000-0000-0000-0000-000000000005")
MEAL_STATUS_REVISION_ID = UUID("84000000-0000-0000-0000-000000000006")
BREAKFAST_STATUS_EVENT_ID = UUID("84000000-0000-0000-0000-000000000007")
LUNCH_STATUS_EVENT_ID = UUID("84000000-0000-0000-0000-000000000008")


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


class _Messages:
    critical_calls: list[tuple] = []
    warning_calls: list[tuple] = []

    @classmethod
    def reset(cls):
        cls.critical_calls = []
        cls.warning_calls = []

    @classmethod
    def critical(cls, *args):
        cls.critical_calls.append(args)

    @classmethod
    def warning(cls, *args):
        cls.warning_calls.append(args)


def _selection(tmp_path: Path) -> ImportWizardSelection:
    path = tmp_path / "existing.xlsx"
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.title = "명단"
    worksheet.append(("성명", "생년월일", "식종", "핸드폰", "학교명", "주소"))
    worksheet.append(
        (
            "홍길동",
            "20150302",
            "조식",
            "010-1234-5678",
            "이전초",
            "성남대로 1",
        )
    )
    workbook.save(path)
    workbook.close()
    profile = ImportMappingProfile(
        PROFILE_ID,
        1,
        "기존 명단",
        "roster-xlsx",
        {
            ProfileField.NAME: "A",
            ProfileField.BIRTH_DATE: "B",
            ProfileField.MOBILE_PHONE: "D",
            ProfileField.SCHOOL_NAME: "E",
            ProfileField.ROAD_ADDRESS: "F",
        },
        combined_meals_column="C",
    )
    return ImportWizardSelection(
        parse_workbook(path, profile),
        date(2026, 8, 17),
        None,
        HouseholdMatchingRule(HOUSEHOLD_RULE_ID, 1),
        None,
    )


def _later_selection(
    tmp_path: Path,
    existing_profile: StoredImportProfile,
    existing_rule: StoredHouseholdMatchingRule,
) -> ImportWizardSelection:
    path = tmp_path / "later.xlsx"
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.title = "명단"
    worksheet.append(("성명", "생년월일", "식종", "핸드폰", "학교명", "주소"))
    worksheet.append(("새아동", "20160403", "중식", "01012345678", None, None))
    workbook.save(path)
    workbook.close()
    return ImportWizardSelection(
        parse_workbook(path, existing_profile.profile),
        date(2026, 8, 18),
        existing_profile,
        existing_rule.rule,
        existing_rule,
    )


def _blank_profile_update_selection(
    tmp_path: Path,
    existing_profile: StoredImportProfile,
    existing_rule: StoredHouseholdMatchingRule,
) -> ImportWizardSelection:
    path = tmp_path / "blank-profile-update.xlsx"
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.title = "명단"
    worksheet.append(("성명", "생년월일", "식종", "핸드폰", "학교명", "주소"))
    worksheet.append(("홍길동", "20150302", "중식", None, None, None))
    workbook.save(path)
    workbook.close()
    return ImportWizardSelection(
        parse_workbook(path, existing_profile.profile),
        date(2026, 8, 18),
        existing_profile,
        existing_rule.rule,
        existing_rule,
    )


def _wizard_type(selection: ImportWizardSelection, *, accepted: bool = True):
    class Wizard:
        def __init__(self, **_kwargs):
            pass

        def exec(self):
            return (
                QDialog.DialogCode.Accepted
                if accepted
                else QDialog.DialogCode.Rejected
            )

        def selection(self):
            return selection

    return Wizard


class _AcceptedReview:
    def __init__(self, roster, plan, parent=None, *, household_plan=None):
        assert roster is None
        assert household_plan is not None
        self._plan = plan
        self._household_plan = household_plan

    def exec(self):
        return QDialog.DialogCode.Accepted

    def decisions(self):
        observation_id = self._plan.staged_people[0].observation.observation_id
        return {
            observation_id: ReconciliationDecision(
                observation_id,
                IdentityResolution.CREATE_NEW,
                PERSON_ID,
            )
        }

    def household_decisions(self):
        return self._household_plan.default_decisions()


class _RejectedReview(_AcceptedReview):
    def exec(self):
        return QDialog.DialogCode.Rejected


class _AcceptedFollowupReview:
    def __init__(self, roster, plan, parent=None, *, household_plan=None):
        assert roster is not None
        assert household_plan is not None
        self._plan = plan
        self._household_plan = household_plan

    def exec(self):
        return QDialog.DialogCode.Accepted

    def decisions(self):
        observation_id = self._plan.staged_people[0].observation.observation_id
        return {
            observation_id: ReconciliationDecision(
                observation_id,
                IdentityResolution.CREATE_NEW,
                LATER_PERSON_ID,
            )
        }

    def household_decisions(self):
        return self._household_plan.default_decisions()


class _AcceptedMatchedFollowupReview:
    def __init__(self, roster, plan, parent=None, *, household_plan=None):
        assert roster is not None
        assert household_plan is not None
        self._household_plan = household_plan

    def exec(self):
        return QDialog.DialogCode.Accepted

    def decisions(self):
        return {}

    def household_decisions(self):
        return self._household_plan.default_decisions()


class _AcceptedProfileEditDialog:
    def __init__(self, person, parent=None, *, message_box=None):
        self._profile = replace(
            person.profile,
            school_name=None,
            detail_address="201호",
            mobile_phone=None,
        )

    def exec(self):
        return QDialog.DialogCode.Accepted

    def profile(self):
        return self._profile


class _AcceptedMealStatusDialog:
    def __init__(
        self,
        person,
        roster,
        histories,
        anchor_day,
        initial_meal,
        initial_status,
        parent=None,
    ):
        assert person.person_id == PERSON_ID
        assert roster.person(PERSON_ID) == person
        assert set(histories) == set(Meal)
        assert anchor_day == date(2026, 8, 18)
        assert initial_meal is Meal.BREAKFAST
        assert initial_status is MealStatus.DELIVERY

    def exec(self):
        return QDialog.DialogCode.Accepted

    def selected_meals(self):
        return (Meal.BREAKFAST, Meal.LUNCH)

    def selected_status(self):
        return MealStatus.DELIVERY

    def selected_period(self):
        return InclusiveDateRange(date(2026, 8, 18), None)


def _session(tmp_path: Path) -> DatabaseSession:
    backup_directory = tmp_path / "backups"
    backup_directory.mkdir()
    session = DatabaseSession.create(
        tmp_path / "operation.sqlite3",
        "user-password",
        "institution-recovery",
        backup_directory=backup_directory,
    )
    session.backup_now()
    return session


def test_cancelled_file_step_leaves_db_without_fake_revision(
    app: QApplication,
    tmp_path: Path,
):
    session = _session(tmp_path)
    selection = _selection(tmp_path)
    controller = GuidedInitialImportController(
        session,
        wizard_type=_wizard_type(selection, accepted=False),
        review_type=_AcceptedReview,
        actor_provider=lambda: RevisionActor("tester", "S-1-5-21-test"),
        message_box=_Messages,
    )

    outcome = controller.run()

    assert outcome.status is GuidedImportStatus.CANCELLED
    assert session.database.latest_revision() is None
    assert session.database.current_roster() is None
    session.close()


def test_cancelled_final_review_also_leaves_empty_db_uncommitted(
    app: QApplication,
    tmp_path: Path,
):
    session = _session(tmp_path)
    selection = _selection(tmp_path)
    controller = GuidedInitialImportController(
        session,
        wizard_type=_wizard_type(selection),
        review_type=_RejectedReview,
        actor_provider=lambda: RevisionActor("tester", "S-1-5-21-test"),
        message_box=_Messages,
    )

    outcome = controller.run()

    assert outcome.status is GuidedImportStatus.CANCELLED
    assert session.database.latest_revision() is None
    session.close()


def test_accepted_review_commits_first_revision_and_automatic_backup(
    app: QApplication,
    tmp_path: Path,
):
    _Messages.reset()
    session = _session(tmp_path)
    selection = _selection(tmp_path)
    controller = GuidedInitialImportController(
        session,
        wizard_type=_wizard_type(selection),
        review_type=_AcceptedReview,
        actor_provider=lambda: RevisionActor("tester", "S-1-5-21-test"),
        message_box=_Messages,
    )

    outcome = controller.run()

    assert outcome.completed
    assert outcome.roster is not None
    assert outcome.roster.person(PERSON_ID).profile.name == "홍길동"
    assert len(outcome.roster.households) == 1
    assert outcome.roster.memberships[0].person_id == PERSON_ID
    assert outcome.backup_result is not None and outcome.backup_result.succeeded
    assert len(tuple((tmp_path / "backups").glob("*.mealbackup"))) == 2
    assert _Messages.critical_calls == []
    assert _Messages.warning_calls == []
    latest = session.database.latest_revision()
    rules = household_matching_rules_from_snapshot(
        session.database.snapshot(latest.metadata.revision_id)
    )
    assert rules[0].rule_id == HOUSEHOLD_RULE_ID
    session.close()


def test_later_import_joins_existing_household_and_backup_restores_it(
    app: QApplication,
    tmp_path: Path,
):
    session = _session(tmp_path)
    initial = GuidedInitialImportController(
        session,
        wizard_type=_wizard_type(_selection(tmp_path)),
        review_type=_AcceptedReview,
        actor_provider=lambda: RevisionActor("tester", "S-1-5-21-test"),
        message_box=_Messages,
    ).run()
    assert initial.completed and initial.roster is not None
    initial_revision_id = initial.roster.revision_id
    existing_household_id = initial.roster.memberships[0].household_id
    snapshot = session.database.snapshot(initial_revision_id)
    existing_profile = import_profiles_from_snapshot(snapshot)[0]
    existing_rule = household_matching_rules_from_snapshot(snapshot)[0]

    later = GuidedRosterImportController(
        session,
        wizard_type=_wizard_type(
            _later_selection(tmp_path, existing_profile, existing_rule)
        ),
        review_type=_AcceptedFollowupReview,
        actor_provider=lambda: RevisionActor("tester", "S-1-5-21-test"),
        message_box=_Messages,
    ).run()

    assert later.completed and later.roster is not None
    assert len(later.roster.persons) == 2
    assert len(later.roster.households) == 1
    assert {person.person_id for person in later.roster.household_members(
        existing_household_id,
        on=date(2026, 8, 18),
    )} == {PERSON_ID, LATER_PERSON_ID}
    latest = session.database.latest_revision()
    assert latest.metadata.parent_revision_id == initial_revision_id
    household_decision = latest.metadata.decisions["households"]
    assert next(iter(household_decision.values()))["selection_kind"] == (
        "existing_household"
    )
    assert later.backup_result is not None and later.backup_result.succeeded
    backup_path = later.backup_result.record.path
    assert len(tuple((tmp_path / "backups").glob("*.mealbackup"))) == 3
    session.close()

    with DatabaseSession.restore_and_open_with_user_password(
        backup_path,
        tmp_path / "restored-later.sqlite3",
        "user-password",
    ) as restored:
        roster = restored.database.current_roster()
        assert roster is not None
        assert {person.person_id for person in roster.household_members(
            existing_household_id,
            on=date(2026, 8, 18),
        )} == {PERSON_ID, LATER_PERSON_ID}


def test_later_mapped_blanks_preserve_profile_through_backup_restore(
    app: QApplication,
    tmp_path: Path,
):
    session = _session(tmp_path)
    initial = GuidedInitialImportController(
        session,
        wizard_type=_wizard_type(_selection(tmp_path)),
        review_type=_AcceptedReview,
        actor_provider=lambda: RevisionActor("tester", "S-1-5-21-test"),
        message_box=_Messages,
    ).run()
    assert initial.completed and initial.roster is not None
    snapshot = session.database.snapshot(initial.roster.revision_id)
    existing_profile = import_profiles_from_snapshot(snapshot)[0]
    existing_rule = household_matching_rules_from_snapshot(snapshot)[0]

    later = GuidedRosterImportController(
        session,
        wizard_type=_wizard_type(
            _blank_profile_update_selection(
                tmp_path,
                existing_profile,
                existing_rule,
            )
        ),
        review_type=_AcceptedMatchedFollowupReview,
        actor_provider=lambda: RevisionActor("tester", "S-1-5-21-test"),
        message_box=_Messages,
    ).run()

    assert later.completed and later.roster is not None
    profile = later.roster.person(PERSON_ID).profile
    assert profile.mobile_phone == "010-1234-5678"
    assert profile.school_name == "이전초"
    assert profile.road_address == "성남대로 1"
    assert later.roster.meals_for(PERSON_ID, on=date(2026, 8, 18)) == (
        Meal.LUNCH,
    )
    latest = session.database.latest_revision()
    assert latest.metadata.rule_versions["profile_update_policy_version"] == 1
    assert latest.metadata.decisions["preserved_blank_profile_fields"] == {
        next(iter(latest.metadata.decisions["applied"])): (
            "school_name",
            "road_address",
            "mobile_phone",
        )
    }
    assert later.backup_result is not None and later.backup_result.succeeded
    backup_path = later.backup_result.record.path
    session.close()

    with DatabaseSession.restore_and_open_with_user_password(
        backup_path,
        tmp_path / "restored-blank-profile.sqlite3",
        "user-password",
    ) as restored:
        restored_profile = restored.database.current_roster().person(PERSON_ID).profile
        assert restored_profile.mobile_phone == "010-1234-5678"
        assert restored_profile.school_name == "이전초"
        assert restored_profile.road_address == "성남대로 1"
        restored_revision = restored.database.latest_revision()
        assert restored_revision.metadata.rule_versions[
            "profile_update_policy_version"
        ] == 1
        assert restored_revision.metadata.decisions[
            "preserved_blank_profile_fields"
        ]


def test_direct_profile_edit_can_clear_values_and_backup_restores_revision(
    app: QApplication,
    tmp_path: Path,
):
    _Messages.reset()
    session = _session(tmp_path)
    initial = GuidedInitialImportController(
        session,
        wizard_type=_wizard_type(_selection(tmp_path)),
        review_type=_AcceptedReview,
        actor_provider=lambda: RevisionActor("tester", "S-1-5-21-test"),
        message_box=_Messages,
    ).run()
    assert initial.completed and initial.roster is not None

    edited = PersonProfileEditController(
        session,
        dialog_type=_AcceptedProfileEditDialog,
        actor_provider=lambda: RevisionActor("tester", "S-1-5-21-test"),
        recorded_at_provider=lambda: datetime(2026, 8, 26, 13, 0, tzinfo=UTC),
        effective_date_provider=lambda: date(2026, 8, 26),
        revision_id_factory=lambda: PROFILE_EDIT_REVISION_ID,
        message_box=_Messages,
    ).run(PERSON_ID)

    assert edited.completed and edited.roster is not None
    profile = edited.roster.person(PERSON_ID).profile
    assert profile.school_name is None
    assert profile.mobile_phone is None
    assert profile.road_address == "성남대로 1"
    assert profile.detail_address == "201호"
    latest = session.database.latest_revision()
    assert latest.metadata.source is RevisionSource.UI
    assert latest.metadata.decisions["person_profile_edit"]["cleared_fields"] == (
        "school_name",
        "mobile_phone",
    )
    assert latest.metadata.decisions["person_profile_edit"]["changed_fields"] == (
        "school_name",
        "detail_address",
        "mobile_phone",
    )
    assert edited.backup_result is not None and edited.backup_result.succeeded
    backup_path = edited.backup_result.record.path
    assert len(tuple((tmp_path / "backups").glob("*.mealbackup"))) == 3
    assert _Messages.critical_calls == []
    assert _Messages.warning_calls == []
    session.close()

    with DatabaseSession.restore_and_open_with_user_password(
        backup_path,
        tmp_path / "restored-profile-edit.sqlite3",
        "user-password",
    ) as restored:
        restored_profile = restored.database.current_roster().person(PERSON_ID).profile
        assert restored_profile.school_name is None
        assert restored_profile.mobile_phone is None
        assert restored_profile.detail_address == "201호"
        restored_revision = restored.database.latest_revision()
        assert restored_revision.metadata.revision_id == PROFILE_EDIT_REVISION_ID
        assert restored_revision.metadata.decisions["person_profile_edit"][
            "cleared_fields"
        ] == ("school_name", "mobile_phone")


def test_multi_meal_delivery_auto_enrolls_and_backup_restores_events(
    app: QApplication,
    tmp_path: Path,
):
    _Messages.reset()
    session = _session(tmp_path)
    initial = GuidedInitialImportController(
        session,
        wizard_type=_wizard_type(_selection(tmp_path)),
        review_type=_AcceptedReview,
        actor_provider=lambda: RevisionActor("tester", "S-1-5-21-test"),
        message_box=_Messages,
    ).run()
    assert initial.completed and initial.roster is not None
    event_ids = iter((BREAKFAST_STATUS_EVENT_ID, LUNCH_STATUS_EVENT_ID))

    changed = MealStatusChangeController(
        session,
        dialog_type=_AcceptedMealStatusDialog,
        actor_provider=lambda: RevisionActor("tester", "S-1-5-21-test"),
        recorded_at_provider=lambda: datetime(2026, 8, 26, 14, 0, tzinfo=UTC),
        revision_id_factory=lambda: MEAL_STATUS_REVISION_ID,
        event_id_factory=lambda: next(event_ids),
        message_box=_Messages,
    ).run(
        PERSON_ID,
        Meal.BREAKFAST,
        MealStatus.DELIVERY,
        date(2026, 8, 18),
    )

    assert changed.completed and changed.roster is not None
    assert changed.roster.meals_for(PERSON_ID, on=date(2026, 8, 18)) == (
        Meal.BREAKFAST,
        Meal.LUNCH,
    )
    latest = session.database.latest_revision()
    decision = latest.metadata.decisions["meal_status_change"]
    assert decision["meals"] == (Meal.BREAKFAST.value, Meal.LUNCH.value)
    assert decision["status"] == MealStatus.DELIVERY.value
    assert decision["auto_enrolled_meals"] == (Meal.LUNCH.value,)
    assert session.database.status_history(
        PERSON_ID,
        Meal.BREAKFAST,
    ).events[0].event_id == BREAKFAST_STATUS_EVENT_ID
    assert session.database.status_history(
        PERSON_ID,
        Meal.LUNCH,
    ).events[0].event_id == LUNCH_STATUS_EVENT_ID
    assert changed.backup_result is not None and changed.backup_result.succeeded
    backup_path = changed.backup_result.record.path
    assert len(tuple((tmp_path / "backups").glob("*.mealbackup"))) == 3
    assert _Messages.critical_calls == []
    assert _Messages.warning_calls == []
    session.close()

    with DatabaseSession.restore_and_open_with_user_password(
        backup_path,
        tmp_path / "restored-meal-status.sqlite3",
        "user-password",
    ) as restored:
        roster = restored.database.current_roster()
        assert roster.meals_for(PERSON_ID, on=date(2026, 8, 18)) == (
            Meal.BREAKFAST,
            Meal.LUNCH,
        )
        assert restored.database.status_history(
            PERSON_ID,
            Meal.LUNCH,
        ).events[0].event_id == LUNCH_STATUS_EVENT_ID
        restored_revision = restored.database.latest_revision()
        assert restored_revision.metadata.revision_id == MEAL_STATUS_REVISION_ID
        assert restored_revision.metadata.decisions["meal_status_change"][
            "auto_enrolled_meals"
        ] == (Meal.LUNCH.value,)


def test_startup_cancel_does_not_construct_or_show_blank_roster(
    app: QApplication,
    tmp_path: Path,
):
    session = _session(tmp_path)

    class Access:
        backup_result = AutomaticBackupResult()

        def __init__(self):
            self.session = session

        def exec(self):
            return QDialog.DialogCode.Accepted

    class CancelGuided:
        def __init__(self, opened_session):
            assert opened_session is session

        def run(self, parent=None):
            return GuidedImportOutcome(GuidedImportStatus.CANCELLED)

    class BlankWindowMustNotExist:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("빈 명단 창을 만들면 안 됩니다.")

    settings = SimpleNamespace(database_path=None, backup_directory=tmp_path / "backups")
    controller = DesktopStartupController(
        settings=settings,
        create_dialog_type=lambda **_kwargs: Access(),
        guided_controller_type=CancelGuided,
        roster_window_type=BlankWindowMustNotExist,
        message_box=_Messages,
    )

    outcome = controller.start()

    assert outcome.status is StartupStatus.ONBOARDING_PENDING
    assert outcome.session is None
    with pytest.raises(RuntimeError, match="닫혔습니다"):
        _ = session.database


def test_startup_builds_roster_window_only_after_guided_import_completes(
    app: QApplication,
    tmp_path: Path,
):
    _Messages.reset()
    session = _session(tmp_path)
    selection = _selection(tmp_path)

    class Access:
        backup_result = AutomaticBackupResult()

        def __init__(self):
            self.session = session

        def exec(self):
            return QDialog.DialogCode.Accepted

    class Guided:
        def __init__(self, opened_session):
            self._controller = GuidedInitialImportController(
                opened_session,
                wizard_type=_wizard_type(selection),
                review_type=_AcceptedReview,
                actor_provider=lambda: RevisionActor("tester", "S-1-5-21-test"),
                message_box=_Messages,
            )

        def run(self, parent=None):
            return self._controller.run(parent)

    class Window:
        def __init__(self, roster, histories, *, revision_display_code):
            self.roster = roster
            self.histories = histories
            self.revision_display_code = revision_display_code
            self.closed = False
            self.replaced = False
            self.replace_count = 0
            self.table_model = SimpleNamespace(
                service_day=date(2026, 8, 17),
                meal=Meal.LUNCH,
            )
            self.import_review_requested = SimpleNamespace(
                connect=lambda callback: setattr(self, "_import_callback", callback)
            )
            self.person_edit_requested = SimpleNamespace(
                connect=lambda callback: setattr(
                    self,
                    "_person_edit_callback",
                    callback,
                )
            )
            self.person_add_requested = SimpleNamespace(
                connect=lambda callback: setattr(self, "_person_add_callback", callback)
            )
            self.person_delete_requested = SimpleNamespace(
                connect=lambda callback: setattr(self, "_person_delete_callback", callback)
            )
            self.status_action_requested = SimpleNamespace(
                connect=lambda callback: setattr(
                    self,
                    "_status_callback",
                    callback,
                )
            )
            self.unit_price_requested = SimpleNamespace(
                connect=lambda callback: setattr(
                    self,
                    "_unit_price_callback",
                    callback,
                )
            )
            self.service_adjustment_requested = SimpleNamespace(
                connect=lambda callback: setattr(
                    self,
                    "_service_adjustment_callback",
                    callback,
                )
            )
            self.settings_requested = SimpleNamespace(
                connect=lambda callback: setattr(
                    self,
                    "_settings_callback",
                    callback,
                )
            )
            self.documents_requested = SimpleNamespace(
                connect=lambda callback: setattr(
                    self,
                    "_documents_callback",
                    callback,
                )
            )
            self.document_output_selected = SimpleNamespace(
                emit=lambda selection: setattr(
                    self,
                    "document_output_selection",
                    selection,
                )
            )
            self.document_generated = SimpleNamespace(
                connect=lambda callback: setattr(
                    self,
                    "_document_generated_callback",
                    callback,
                )
            )

        def set_meal_change_history_provider(self, provider):
            self.meal_history_provider = provider

        def replace_roster(self, roster, histories, *, revision_display_code):
            self.roster = roster
            self.histories = histories
            self.revision_display_code = revision_display_code
            self.replaced = True
            self.replace_count += 1

        def close(self):
            self.closed = True

    class Followup:
        def __init__(self, opened_session):
            assert opened_session is session

        def run(self, parent=None):
            assert parent is outcome.window
            return GuidedImportOutcome(
                GuidedImportStatus.COMPLETED,
                session.database.current_roster(),
            )

    class ProfileEdit:
        def __init__(self, opened_session):
            assert opened_session is session

        def run(self, person_id, parent=None):
            assert person_id == PERSON_ID
            assert parent is outcome.window
            return SimpleNamespace(
                completed=True,
                roster=session.database.current_roster(),
            )

    row_actions = []

    class RowEdit:
        def __init__(self, opened_session):
            assert opened_session is session

        def add(self, meal, parent, *, service_day):
            assert parent is outcome.window
            assert service_day == parent.table_model.service_day
            row_actions.append(("add", meal))
            return SimpleNamespace(completed=True, roster=session.database.current_roster())

        def delete(self, person_id, parent):
            assert parent is outcome.window
            row_actions.append(("delete", person_id))
            return SimpleNamespace(completed=True, roster=session.database.current_roster())

    class MealStatusEdit:
        def __init__(self, opened_session):
            assert opened_session is session

        def run(
            self,
            person_id,
            meal,
            status,
            anchor_day,
            parent=None,
        ):
            assert (person_id, meal, status, anchor_day) == (
                PERSON_ID,
                Meal.LUNCH,
                MealStatus.PAUSED,
                date(2026, 8, 17),
            )
            assert parent is outcome.window
            return SimpleNamespace(
                completed=True,
                roster=session.database.current_roster(),
            )

    class UnitPriceEdit:
        def __init__(self, opened_session):
            assert opened_session is session

        def run(self, parent=None):
            assert parent is outcome.window
            return SimpleNamespace(
                completed=True,
                roster=session.database.current_roster(),
            )

    class ExportPasswordEdit:
        def __init__(self, opened_session):
            assert opened_session is session

        def ensure_configured(self, parent=None):
            assert parent is outcome.window
            return SimpleNamespace(
                completed=True,
                roster=session.database.current_roster(),
            )

        def run(self, parent=None):
            assert parent is outcome.window
            return SimpleNamespace(
                completed=True,
                roster=session.database.current_roster(),
            )

    class DocumentTemplateEdit:
        def __init__(self, opened_session):
            assert opened_session is session

        def ensure_configured(self, parent=None):
            assert parent is outcome.window
            return SimpleNamespace(
                completed=True,
                roster=session.database.current_roster(),
            )

        def run(self, parent=None):
            assert parent is outcome.window
            return SimpleNamespace(
                completed=True,
                roster=session.database.current_roster(),
            )

    class ServiceAdjustmentEdit:
        def __init__(self, opened_session):
            assert opened_session is session

        def run(self, parent=None):
            assert parent is outcome.window
            return SimpleNamespace(
                completed=True,
                roster=session.database.current_roster(),
            )

    class DocumentHub:
        def __init__(self, parent=None):
            assert parent is outcome.window

        def exec(self):
            return QDialog.DialogCode.Accepted

        @property
        def selected_action(self):
            return DocumentAction.INVOICE

    class DocumentSource:
        def __init__(self, summaries, parent=None):
            assert parent is outcome.window
            self.selected_summary = summaries[0]

        def exec(self):
            return QDialog.DialogCode.Accepted

    class DocumentPeriod:
        def __init__(self, request, parent=None):
            assert parent is outcome.window
            assert request.action is DocumentAction.INVOICE
            assert request.anchor_day == date(2026, 8, 17)
            assert request.anchor_meal is Meal.LUNCH
            self._request = request

        def exec(self):
            return QDialog.DialogCode.Accepted

        def selected_period(self):
            return DocumentPeriodSelection(
                self._request.action,
                self._request.source,
                self._request.anchor_day,
                self._request.anchor_meal,
                date(2026, 8, 1),
                date(2026, 8, 31),
            )

    class DocumentMeal:
        def __init__(self, period, parent=None):
            assert parent is outcome.window
            assert period.action is DocumentAction.INVOICE
            assert period.anchor_meal is Meal.LUNCH
            self._period = period

        def exec(self):
            return QDialog.DialogCode.Accepted

        def selection(self):
            return DocumentMealSelection(self._period, tuple(Meal))

    document_directory = tmp_path / "documents"
    document_directory.mkdir()

    class DocumentOutput:
        def __init__(self, document, saved_directory, parent=None):
            assert parent is outcome.window
            assert document.period.action is DocumentAction.INVOICE
            assert document.meals == tuple(Meal)
            assert saved_directory == document_directory
            self._document = document

        def exec(self):
            return QDialog.DialogCode.Accepted

        def selection(self):
            return next_available_document_output(
                self._document,
                document_directory,
                DocumentOutputMode.SAVED_DEFAULT,
            )

    completion_results = []

    class DocumentCompletion:
        def __init__(self, result, parent=None):
            assert parent is outcome.window
            result.verify_file()
            self._result = result

        def exec(self):
            completion_results.append(self._result)
            return QDialog.DialogCode.Rejected

    class SettingsHub:
        selected_action = SettingsAction.EXPORT_PASSWORD

        def __init__(self, parent=None):
            assert parent is outcome.window

        def exec(self):
            return QDialog.DialogCode.Accepted

    class Settings:
        database_path = None
        backup_directory = tmp_path / "backups"
        document_output_directory = document_directory

        def save_document_output_directory(self, directory):
            self.document_output_directory = Path(directory)

    settings = Settings()
    controller = DesktopStartupController(
        settings=settings,
        create_dialog_type=lambda **_kwargs: Access(),
        guided_controller_type=Guided,
        followup_controller_type=Followup,
        profile_edit_controller_type=ProfileEdit,
        roster_row_controller_type=RowEdit,
        meal_status_controller_type=MealStatusEdit,
        service_adjustment_controller_type=ServiceAdjustmentEdit,
        unit_price_controller_type=UnitPriceEdit,
        export_password_controller_type=ExportPasswordEdit,
        document_template_controller_type=DocumentTemplateEdit,
        document_hub_dialog_type=DocumentHub,
        document_source_dialog_type=DocumentSource,
        document_period_dialog_type=DocumentPeriod,
        document_meal_dialog_type=DocumentMeal,
        document_output_dialog_type=DocumentOutput,
        document_completion_dialog_type=DocumentCompletion,
        settings_hub_dialog_type=SettingsHub,
        roster_window_type=Window,
        message_box=_Messages,
    )

    outcome = controller.start()

    assert outcome.ready
    assert outcome.window.roster.person(PERSON_ID).profile.name == "홍길동"
    assert len(outcome.window.histories) == 3
    assert outcome.window.meal_history_provider(outcome.window.roster.person(PERSON_ID))
    assert outcome.window.revision_display_code == format_revision_timestamp(
        session.database.revision(outcome.window.roster.revision_id).metadata.recorded_at
    )
    outcome.window._import_callback()
    assert outcome.window.replaced
    outcome.window._person_edit_callback(PERSON_ID)
    assert outcome.window.replace_count == 2
    outcome.window._person_add_callback(Meal.LUNCH)
    outcome.window._person_delete_callback(PERSON_ID)
    assert row_actions == [("add", Meal.LUNCH), ("delete", PERSON_ID)]
    assert outcome.window.replace_count == 4
    outcome.window._status_callback(
        PERSON_ID,
        Meal.LUNCH,
        MealStatus.PAUSED,
    )
    assert outcome.window.replace_count == 5
    outcome.window._unit_price_callback()
    assert outcome.window.replace_count == 6
    outcome.window._service_adjustment_callback()
    assert outcome.window.replace_count == 7
    outcome.window._settings_callback()
    assert outcome.window.replace_count == 7
    outcome.window._documents_callback()
    selection = outcome.window.document_output_selection
    assert selection.document.period.action is DocumentAction.INVOICE
    assert (
        selection.document.period.source.revision_id
        == outcome.window.roster.revision_id
    )
    assert selection.document.period.starts_on == date(2026, 8, 1)
    assert selection.document.period.ends_on == date(2026, 8, 31)
    assert selection.document.meals == tuple(Meal)
    assert selection.directory == document_directory.resolve()
    assert selection.mode is DocumentOutputMode.SAVED_DEFAULT
    assert selection.filename == suggest_document_filename(selection.document)
    assert selection.reissue_number == 1
    assert selection.output_path == document_directory / selection.filename
    assert completion_results == []

    outcome.window._document_generated_callback(object())
    assert completion_results == []
    assert len(_Messages.critical_calls) == 1
    assert _Messages.critical_calls[0][1] == (
        "문서 완료 상태를 확인하지 못했습니다"
    )

    content = b"synthetic generated workbook"
    selection.output_path.write_bytes(content)
    generated = GeneratedDocumentResult(
        selection,
        datetime(2026, 8, 31, 15, 40, tzinfo=UTC),
        hashlib.sha256(content).hexdigest(),
        len(content),
    )
    outcome.window._document_generated_callback(generated)
    assert completion_results == [generated]
    assert outcome.window.replace_count == 7
    outcome.close()


def test_delivery_report_hub_skips_monthly_template_prerequisite(tmp_path: Path):
    document_directory = tmp_path / "documents"
    document_directory.mkdir()
    summary = RevisionSummary(
        1,
        UUID("84000000-0000-0000-0000-000000000099"),
        "20260831-001",
        datetime(2026, 8, 31, 1, 0, tzinfo=UTC),
        date(2026, 8, 31),
        RevisionActor("tester", "S-1-5-21-test"),
        "배송 리포트 원본",
        RevisionSource.UI,
    )
    session = SimpleNamespace(
        database=SimpleNamespace(revision_summaries=lambda: (summary,))
    )
    emitted = []
    window = SimpleNamespace(
        table_model=SimpleNamespace(
            service_day=date(2026, 8, 31),
            meal=Meal.LUNCH,
        ),
        document_output_selected=SimpleNamespace(emit=emitted.append),
    )
    prerequisite_calls = []

    class Hub:
        selected_action = DocumentAction.DELIVERY_REPORT

        def __init__(self, parent=None):
            assert parent is window

        def exec(self):
            return QDialog.DialogCode.Accepted

    class Source:
        selected_summary = summary

        def __init__(self, summaries, parent=None):
            assert summaries == (summary,)
            assert parent is window

        def exec(self):
            return QDialog.DialogCode.Accepted

    class Period:
        def __init__(self, request, parent=None):
            assert request.action is DocumentAction.DELIVERY_REPORT
            assert parent is window
            self._request = request

        def exec(self):
            return QDialog.DialogCode.Accepted

        def selected_period(self):
            return DocumentPeriodSelection(
                self._request.action,
                self._request.source,
                self._request.anchor_day,
                self._request.anchor_meal,
                date(2026, 8, 31),
                date(2026, 8, 31),
            )

    class MealDialog:
        def __init__(self, period, parent=None):
            assert parent is window
            self._period = period

        def exec(self):
            return QDialog.DialogCode.Accepted

        def selection(self):
            return DocumentMealSelection(self._period, tuple(Meal))

    class Output:
        def __init__(self, document, saved_directory, parent=None):
            assert saved_directory == document_directory
            assert parent is window
            self._document = document

        def exec(self):
            return QDialog.DialogCode.Accepted

        def selection(self):
            return next_available_document_output(
                self._document,
                document_directory,
                DocumentOutputMode.SAVED_DEFAULT,
            )

    class Password:
        def __init__(self, opened_session):
            assert opened_session is session

        def ensure_configured(self, parent=None):
            prerequisite_calls.append(("password", parent))
            return SimpleNamespace(completed=True, roster=None)

    class UnexpectedTemplate:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("배송 리포트에서 월간 문서 양식을 요구하면 안 됩니다.")

    settings = SimpleNamespace(
        document_output_directory=document_directory,
        save_document_output_directory=lambda _directory: None,
    )
    controller = DesktopStartupController(
        settings=settings,
        document_hub_dialog_type=Hub,
        document_source_dialog_type=Source,
        document_period_dialog_type=Period,
        document_meal_dialog_type=MealDialog,
        document_output_dialog_type=Output,
        export_password_controller_type=Password,
        document_template_controller_type=UnexpectedTemplate,
        message_box=_Messages,
    )

    controller._run_document_hub(session, window)

    assert prerequisite_calls == [("password", window)]
    assert len(emitted) == 1
    assert emitted[0].document.period.action is DocumentAction.DELIVERY_REPORT
    assert emitted[0].document.meals == tuple(Meal)


def test_school_calendar_editor_can_route_to_excel_import():
    calls = []
    roster = SimpleNamespace(
        revision_id=UUID("84000000-0000-0000-0000-000000000099"),
        persons=(),
    )

    class Database:
        @staticmethod
        def revision(revision_id):
            assert revision_id == roster.revision_id
            return SimpleNamespace(metadata=SimpleNamespace(
                recorded_at=datetime(2026, 8, 31, 1, 2, 3, tzinfo=UTC)
            ))

        @staticmethod
        def status_history(*_args):
            raise AssertionError("빈 명단에서는 상태 이력을 읽지 않아야 합니다")

    session = SimpleNamespace(database=Database())

    class StoredEditor:
        def __init__(self, opened_session):
            assert opened_session is session

        def run(self, parent=None):
            calls.append(("stored", parent))
            return SchoolCalendarEditOutcome(
                SchoolCalendarEditStatus.IMPORT_REQUESTED,
                roster,
            )

    class ExcelImporter:
        def __init__(self, opened_session):
            assert opened_session is session

        def run(self, parent=None):
            calls.append(("excel", parent))
            return SimpleNamespace(completed=True, roster=roster)

    class Window:
        def replace_roster(self, value, histories, *, revision_display_code):
            calls.append(("replace", value, histories, revision_display_code))

    window = Window()
    controller = DesktopStartupController(
        school_calendar_edit_controller_type=StoredEditor,
        school_calendar_controller_type=ExcelImporter,
        message_box=_Messages,
    )

    controller._run_school_calendar(session, window)

    assert calls == [
        ("stored", window),
        ("excel", window),
        ("replace", roster, {}, "20260831-100203"),
    ]
