"""Offscreen closure/replacement editor and backup persistence checks."""

from __future__ import annotations

import os
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import UUID, uuid4

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import QApplication, QDialog

from src.desktop_v2.service_adjustment import (
    PublicHolidayBatchDialog,
    ServiceAdjustmentAction,
    ServiceAdjustmentController,
    ServiceAdjustmentDialog,
    ServiceAdjustmentHubDialog,
    ServiceAdjustmentRequest,
)
from src.domain.service_adjustments import (
    SERVICE_ADJUSTMENT_SCHEDULE_ID,
    SERVICE_ADJUSTMENT_SCHEDULE_KIND,
    ReplacementDeliveryOverride,
    ServiceAdjustmentChange,
    ServiceAdjustmentOrigin,
    ServiceAdjustmentSchedule,
    ServiceTreatment,
    service_adjustment_schedule_from_state,
)
from src.domain.revisions import (
    AuditChange,
    ChangeOperation,
    RevisionActor,
    RevisionMetadata,
    RevisionRecord,
    RevisionSource,
)
from src.domain.roster import (
    InclusiveDateRange,
    HouseholdDeliverySetting,
    Meal,
    MealEnrollment,
    Person,
    PersonProfile,
    RosterSnapshot,
)
from src.domain.public_holidays import PublicHolidayCandidate
from src.storage.database_session import DatabaseSession


BASE_REVISION_ID = UUID("82000000-0000-0000-0000-000000000001")
BASE_ENTITY_ID = UUID("82000000-0000-0000-0000-000000000002")
ADJUSTMENT_REVISION_ID = UUID("82000000-0000-0000-0000-000000000003")
ADJUSTMENT_CHANGE_ID = UUID("82000000-0000-0000-0000-000000000004")
HOLIDAY_REVISION_ID = UUID("82000000-0000-0000-0000-000000000005")
HOLIDAY_CHANGE_ID = UUID("82000000-0000-0000-0000-000000000006")
HOLIDAY_CHANGE_ID_2 = UUID("82000000-0000-0000-0000-000000000007")
ANCHOR_DAY = date(2026, 9, 10)
CHUSEOK = PublicHolidayCandidate(
    date(2026, 9, 25),
    "추석",
    "python-holidays:KR",
    "0.102",
)
CHUSEOK_EVE = PublicHolidayCandidate(
    date(2026, 9, 24),
    "추석 전날",
    "python-holidays:KR",
    "0.102",
)


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _base_revision() -> RevisionRecord:
    return RevisionRecord(
        RevisionMetadata(
            revision_id=BASE_REVISION_ID,
            parent_revision_id=None,
            recorded_at=datetime(2026, 8, 26, 1, 0, tzinfo=UTC),
            effective_on=date(2026, 8, 1),
            actor=RevisionActor("tester", "S-1-5-21-test"),
            reason="휴무 화면 기준 명단",
            source=RevisionSource.UI,
        ),
        (
            AuditChange(
                "service-adjustment-ui-probe",
                BASE_ENTITY_ID,
                ChangeOperation.INSERT,
                before=None,
                after={"value": "base"},
            ),
        ),
    )


def test_dialog_defaults_all_meals_and_allows_per_day_meal_mapping(
    app: QApplication,
):
    dialog = ServiceAdjustmentDialog(ServiceAdjustmentSchedule(), ANCHOR_DAY)

    assert dialog.selected_meals() == tuple(Meal)
    assert dialog.selected_treatment() is ServiceTreatment.NOT_PROVIDED
    assert dialog.selected_period() == InclusiveDateRange(ANCHOR_DAY, ANCHOR_DAY)
    assert dialog._replacement_frame.isHidden()
    assert dialog._save_button.isEnabled()

    dialog._ends_on.setDate(QDate(2026, 9, 12))
    dialog._replacement_radio.setChecked(True)
    dialog._custom_mapping.setChecked(True)
    app.processEvents()

    assert dialog.selected_treatment() is ServiceTreatment.REPLACEMENT
    assert dialog.selected_default_delivery_on() == date(2026, 9, 9)
    assert dialog._mapping_table.rowCount() == 3
    assert len(dialog._mapping_editors) == 9
    assert not dialog._custom_frame.isHidden()
    assert dialog._history.isHidden()

    custom = dialog._mapping_editors[(date(2026, 9, 11), Meal.LUNCH)]
    custom.setDate(QDate(2026, 9, 8))
    dialog._default_delivery_on.setDate(QDate(2026, 9, 7))
    app.processEvents()

    assert (
        dialog._mapping_editors[(date(2026, 9, 10), Meal.BREAKFAST)]
        .date()
        .toString("yyyy-MM-dd")
        == "2026-09-07"
    )
    assert custom.date().toString("yyyy-MM-dd") == "2026-09-08"
    assert dialog.selected_delivery_overrides() == (
        ReplacementDeliveryOverride(
            Meal.LUNCH,
            date(2026, 9, 11),
            date(2026, 9, 8),
        ),
    )
    assert "개별 변경 1건" in dialog._summary.text()
    dialog._save_button.click()
    assert dialog.result() == QDialog.DialogCode.Accepted


def test_hub_lists_offline_candidates_without_changing_schedule(
    app: QApplication,
):
    schedule = ServiceAdjustmentSchedule()
    dialog = ServiceAdjustmentHubDialog(
        schedule,
        ANCHOR_DAY,
        holiday_provider=lambda year: (
            (CHUSEOK_EVE, CHUSEOK) if year == 2026 else ()
        ),
    )
    app.processEvents()

    assert dialog._candidates.rowCount() == 2
    assert dialog._candidates.item(0, 2).text() == "추석 전날"
    assert "검토 필요" in dialog._candidates.item(0, 3).text()
    assert dialog._candidates.item(0, 0).checkState() is Qt.CheckState.Unchecked
    assert not dialog._review_button.isEnabled()
    assert schedule == ServiceAdjustmentSchedule()

    dialog._select_unreviewed_button.click()
    app.processEvents()

    assert dialog._candidates.item(0, 0).checkState() is Qt.CheckState.Checked
    assert dialog._candidates.item(1, 0).checkState() is Qt.CheckState.Checked
    assert dialog._review_button.text() == "선택한 공휴일 2일 검토"
    dialog._review_button.click()

    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.selected_request() == ServiceAdjustmentRequest(
        ServiceAdjustmentAction.PUBLIC_HOLIDAY,
        (CHUSEOK_EVE, CHUSEOK),
        (CHUSEOK_EVE, CHUSEOK),
    )


def test_batch_editor_requires_choice_and_maps_every_holiday_meal_delivery(
    app: QApplication,
):
    dialog = PublicHolidayBatchDialog(
        ServiceAdjustmentSchedule(),
        (CHUSEOK_EVE, CHUSEOK),
        (CHUSEOK_EVE, CHUSEOK),
    )
    app.processEvents()

    assert dialog.selected_meals() == tuple(Meal)
    assert not dialog._save_button.isEnabled()
    assert "처리 방법" in dialog._error.text()
    assert dialog._delivery_frame.isHidden()

    dialog._replacement_radio.setChecked(True)
    app.processEvents()

    assert dialog.selected_treatment() is ServiceTreatment.REPLACEMENT
    assert dialog._delivery_table.rowCount() == 2
    assert len(dialog._delivery_editors) == 6
    assert all(
        editor.date().toString("yyyy-MM-dd") == "2026-09-23"
        for editor in dialog._delivery_editors.values()
    )
    custom = dialog._delivery_editors[(CHUSEOK.day, Meal.LUNCH)]
    custom.setDate(QDate(2026, 9, 22))
    app.processEvents()

    deliveries = dialog.selected_replacement_deliveries()
    assert len(deliveries) == 6
    assert ReplacementDeliveryOverride(
        Meal.LUNCH,
        CHUSEOK.day,
        date(2026, 9, 22),
    ) in deliveries
    assert dialog._save_button.isEnabled()
    assert "실제 제공일 6칸" in dialog._summary.text()


def test_batch_editor_only_maps_meals_scheduled_on_each_holiday(
    app: QApplication,
):
    person_id = uuid4()
    roster = RosterSnapshot(
        uuid4(),
        persons=(
            Person(
                person_id,
                PersonProfile("합성 아동"),
                (HouseholdDeliverySetting(Meal.BREAKFAST, (3,)),),
            ),
        ),
        enrollments=(
            MealEnrollment(person_id, Meal.BREAKFAST),
            MealEnrollment(person_id, Meal.LUNCH),
        ),
    )
    dialog = PublicHolidayBatchDialog(
        ServiceAdjustmentSchedule(),
        (CHUSEOK_EVE, CHUSEOK),
        (CHUSEOK_EVE, CHUSEOK),
        roster=roster,
    )

    dialog._replacement_radio.setChecked(True)
    app.processEvents()

    expected_keys = (
        (CHUSEOK_EVE.day, Meal.BREAKFAST),
        (CHUSEOK_EVE.day, Meal.LUNCH),
        (CHUSEOK.day, Meal.LUNCH),
    )
    assert dialog.replacement_service_keys() == expected_keys
    assert tuple(dialog._delivery_editors) == expected_keys
    assert dialog._delivery_table.cellWidget(
        1,
        2 + tuple(Meal).index(Meal.BREAKFAST),
    ) is None
    assert dialog._delivery_table.item(
        1,
        2 + tuple(Meal).index(Meal.BREAKFAST),
    ).text() == "—"
    assert len(dialog.selected_replacement_deliveries()) == 3
    assert "실제 제공일 3칸" in dialog._summary.text()


def test_batch_editor_keeps_weekend_breakfast_bundled_on_an_earlier_delivery_day(
    app: QApplication,
):
    saturday_holiday = PublicHolidayCandidate(
        date(2026, 9, 26),
        "합성 주말 공휴일",
        "test:KR",
        "1",
    )
    person_id = uuid4()
    roster = RosterSnapshot(
        uuid4(),
        persons=(Person(person_id, PersonProfile("합성 아동")),),
        enrollments=(
            MealEnrollment(person_id, Meal.BREAKFAST),
            MealEnrollment(person_id, Meal.LUNCH),
        ),
    )
    dialog = PublicHolidayBatchDialog(
        ServiceAdjustmentSchedule(),
        (saturday_holiday,),
        (saturday_holiday,),
        roster=roster,
    )

    dialog._replacement_radio.setChecked(True)
    app.processEvents()

    assert dialog.replacement_service_keys() == (
        (saturday_holiday.day, Meal.BREAKFAST),
    )
    assert (saturday_holiday.day, Meal.LUNCH) not in dialog._delivery_editors


def test_batch_editor_warns_when_selected_meals_include_prior_reviews(
    app: QApplication,
):
    prior = ServiceAdjustmentChange(
        HOLIDAY_CHANGE_ID,
        InclusiveDateRange(CHUSEOK_EVE.day, CHUSEOK_EVE.day),
        (Meal.BREAKFAST,),
        ServiceTreatment.NORMAL,
        None,
        (),
        CHUSEOK_EVE.name,
        datetime(2026, 8, 26, 1, 30, tzinfo=UTC),
        ServiceAdjustmentOrigin.PUBLIC_HOLIDAY,
        CHUSEOK_EVE,
    )
    dialog = PublicHolidayBatchDialog(
        ServiceAdjustmentSchedule((prior,)),
        (CHUSEOK_EVE, CHUSEOK),
        (CHUSEOK_EVE, CHUSEOK),
    )
    app.processEvents()

    assert not dialog._overwrite_notice.isHidden()
    assert "이미 검토한 식종 1칸" in dialog._overwrite_notice.text()

    dialog._meal_checks[Meal.BREAKFAST].setChecked(False)
    app.processEvents()

    assert dialog._overwrite_notice.isHidden()


class _AcceptedDialog:
    def __init__(self, schedule, anchor_day, parent=None):
        del parent
        assert schedule == ServiceAdjustmentSchedule()
        assert anchor_day == ANCHOR_DAY

    @staticmethod
    def exec():
        return QDialog.DialogCode.Accepted

    @staticmethod
    def selected_period():
        return InclusiveDateRange(date(2026, 9, 10), date(2026, 9, 12))

    @staticmethod
    def selected_meals():
        return tuple(Meal)

    @staticmethod
    def selected_treatment():
        return ServiceTreatment.REPLACEMENT

    @staticmethod
    def selected_default_delivery_on():
        return date(2026, 9, 9)

    @staticmethod
    def selected_delivery_overrides():
        return (
            ReplacementDeliveryOverride(
                Meal.LUNCH,
                date(2026, 9, 11),
                date(2026, 9, 8),
            ),
        )

    @staticmethod
    def selected_note():
        return "업체 연속 휴무"


class _AcceptedManualHub:
    def __init__(self, schedule, anchor_day, holiday_provider, parent=None):
        del holiday_provider, parent
        assert schedule == ServiceAdjustmentSchedule()
        assert anchor_day == ANCHOR_DAY

    @staticmethod
    def exec():
        return QDialog.DialogCode.Accepted

    @staticmethod
    def selected_request():
        return ServiceAdjustmentRequest(ServiceAdjustmentAction.MANUAL)


class _AcceptedHolidayHub:
    def __init__(self, schedule, anchor_day, holiday_provider, parent=None):
        del holiday_provider, parent
        assert schedule == ServiceAdjustmentSchedule()
        assert anchor_day == ANCHOR_DAY

    @staticmethod
    def exec():
        return QDialog.DialogCode.Accepted

    @staticmethod
    def selected_request():
        return ServiceAdjustmentRequest(
            ServiceAdjustmentAction.PUBLIC_HOLIDAY,
            (CHUSEOK_EVE, CHUSEOK),
            (CHUSEOK_EVE, CHUSEOK),
        )


class _AcceptedHolidayBatchDialog:
    def __init__(
        self,
        schedule,
        public_holidays,
        calendar_holidays,
        parent=None,
        *,
        roster=None,
    ):
        del parent
        assert schedule == ServiceAdjustmentSchedule()
        assert public_holidays == (CHUSEOK_EVE, CHUSEOK)
        assert calendar_holidays == (CHUSEOK_EVE, CHUSEOK)
        assert isinstance(roster, RosterSnapshot)

    @staticmethod
    def exec():
        return QDialog.DialogCode.Accepted

    @staticmethod
    def selected_meals():
        return tuple(Meal)

    @staticmethod
    def selected_treatment():
        return ServiceTreatment.NORMAL

    @staticmethod
    def selected_replacement_deliveries():
        return ()


class _IncompleteHolidayReplacementDialog(_AcceptedHolidayBatchDialog):
    @staticmethod
    def selected_meals():
        return (Meal.LUNCH,)

    @staticmethod
    def selected_treatment():
        return ServiceTreatment.REPLACEMENT

    @staticmethod
    def replacement_service_keys():
        return (
            (CHUSEOK_EVE.day, Meal.LUNCH),
            (CHUSEOK.day, Meal.LUNCH),
        )

    @staticmethod
    def selected_replacement_deliveries():
        return (
            ReplacementDeliveryOverride(
                Meal.LUNCH,
                CHUSEOK_EVE.day,
                date(2026, 9, 23),
            ),
        )


class _Messages:
    @staticmethod
    def critical(*args):
        raise AssertionError(args)

    @staticmethod
    def warning(*args):
        raise AssertionError(args)


class _CapturedMessages:
    errors: list[tuple] = []

    @classmethod
    def critical(cls, *args):
        cls.errors.append(args)

    @staticmethod
    def warning(*args):
        raise AssertionError(args)


def test_controller_saves_vendor_schedule_and_verified_backup_restores_it(
    app: QApplication,
    tmp_path: Path,
):
    del app
    backup_directory = tmp_path / "backups"
    backup_directory.mkdir()
    source = tmp_path / "operations.sqlite3"
    with DatabaseSession.create(
        source,
        "everyday-password",
        "institution-recovery",
        backup_directory=backup_directory,
    ) as session:
        session.append_revision(_base_revision())
        outcome = ServiceAdjustmentController(
            session,
            hub_dialog_type=_AcceptedManualHub,
            dialog_type=_AcceptedDialog,
            actor_provider=lambda: RevisionActor("tester", "S-1-5-21-test"),
            today_provider=lambda: ANCHOR_DAY,
            recorded_at_provider=lambda: datetime(
                2026, 8, 26, 2, 0, tzinfo=UTC
            ),
            revision_id_factory=lambda: ADJUSTMENT_REVISION_ID,
            change_id_factory=lambda: ADJUSTMENT_CHANGE_ID,
            message_box=_Messages,
        ).run()

        assert outcome.completed
        assert outcome.roster is not None
        assert outcome.roster.revision_id == ADJUSTMENT_REVISION_ID
        assert outcome.schedule is not None
        provision = outcome.schedule.provision_for(
            Meal.LUNCH,
            date(2026, 9, 11),
        )
        assert provision.billable
        assert provision.delivery_on == date(2026, 9, 8)
        assert outcome.backup_result is not None
        assert outcome.backup_result.record is not None
        backup_path = outcome.backup_result.record.path

    restored_path = tmp_path / "restored.sqlite3"
    with DatabaseSession.restore_and_open_with_user_password(
        backup_path,
        restored_path,
        "everyday-password",
    ) as restored:
        snapshot = restored.database.snapshot(ADJUSTMENT_REVISION_ID)
        state = snapshot.entity(
            SERVICE_ADJUSTMENT_SCHEDULE_KIND,
            SERVICE_ADJUSTMENT_SCHEDULE_ID,
        )
        schedule = service_adjustment_schedule_from_state(state)
        provision = schedule.provision_for(
            Meal.BREAKFAST,
            date(2026, 9, 12),
        )
        assert provision.billable
        assert provision.delivery_on == date(2026, 9, 9)
        restored.database.check_integrity()


def test_controller_persists_explicit_normal_holiday_review(
    app: QApplication,
    tmp_path: Path,
):
    del app
    backup_directory = tmp_path / "holiday-backups"
    backup_directory.mkdir()
    source = tmp_path / "holiday-review.sqlite3"
    with DatabaseSession.create(
        source,
        "everyday-password",
        "institution-recovery",
        backup_directory=backup_directory,
    ) as session:
        session.append_revision(_base_revision())
        change_ids = iter((HOLIDAY_CHANGE_ID, HOLIDAY_CHANGE_ID_2))
        outcome = ServiceAdjustmentController(
            session,
            hub_dialog_type=_AcceptedHolidayHub,
            holiday_dialog_type=_AcceptedHolidayBatchDialog,
            actor_provider=lambda: RevisionActor("tester", "S-1-5-21-test"),
            today_provider=lambda: ANCHOR_DAY,
            recorded_at_provider=lambda: datetime(
                2026, 8, 26, 2, 30, tzinfo=UTC
            ),
            revision_id_factory=lambda: HOLIDAY_REVISION_ID,
            change_id_factory=change_ids.__next__,
            message_box=_Messages,
        ).run()

        assert outcome.completed
        assert outcome.schedule is not None
        assert outcome.schedule.public_holiday_treatments_for(
            CHUSEOK.day
        ) == {meal: ServiceTreatment.NORMAL for meal in Meal}
        assert outcome.schedule.public_holiday_treatments_for(
            CHUSEOK_EVE.day
        ) == {meal: ServiceTreatment.NORMAL for meal in Meal}
        stored = outcome.schedule.changes[-2:]
        assert all(
            change.origin is ServiceAdjustmentOrigin.PUBLIC_HOLIDAY
            for change in stored
        )
        assert tuple(change.public_holiday for change in stored) == (
            CHUSEOK_EVE,
            CHUSEOK,
        )
        assert outcome.backup_result is not None
        assert outcome.backup_result.record is not None
        revision = session.database.revision(HOLIDAY_REVISION_ID)
        assert revision.metadata.reason == "공휴일 운영 일괄 검토 저장"
        assert len(revision.changes) == 1
        decisions = revision.metadata.decisions["public_holiday_reviews"]
        assert len(decisions) == 2
        assert all(
            decision["public_holiday"]["provider_version"] == "0.102"
            for decision in decisions
        )


def test_controller_does_not_partially_save_incomplete_holiday_replacement(
    tmp_path: Path,
):
    source = tmp_path / "incomplete-holiday-review.sqlite3"
    backup_directory = tmp_path / "incomplete-holiday-backups"
    backup_directory.mkdir()
    _CapturedMessages.errors.clear()
    with DatabaseSession.create(
        source,
        "everyday-password",
        "institution-recovery",
        backup_directory=backup_directory,
    ) as session:
        session.append_revision(_base_revision())
        change_ids = iter((HOLIDAY_CHANGE_ID, HOLIDAY_CHANGE_ID_2))

        outcome = ServiceAdjustmentController(
            session,
            hub_dialog_type=_AcceptedHolidayHub,
            holiday_dialog_type=_IncompleteHolidayReplacementDialog,
            actor_provider=lambda: RevisionActor("tester", "S-1-5-21-test"),
            today_provider=lambda: ANCHOR_DAY,
            recorded_at_provider=lambda: datetime(
                2026, 8, 26, 2, 30, tzinfo=UTC
            ),
            revision_id_factory=lambda: HOLIDAY_REVISION_ID,
            change_id_factory=change_ids.__next__,
            message_box=_CapturedMessages,
        ).run()

        assert not outcome.completed
        assert session.database.current_roster().revision_id == BASE_REVISION_ID
        assert session.database.snapshot(BASE_REVISION_ID).entity(
            SERVICE_ADJUSTMENT_SCHEDULE_KIND,
            SERVICE_ADJUSTMENT_SCHEDULE_ID,
        ) is None
        assert "저장하지 못했습니다" in _CapturedMessages.errors[0][1]
