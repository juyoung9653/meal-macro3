"""Offscreen unit price editor and automatic-backup controller checks."""

from __future__ import annotations

import os
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import UUID

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QDate
from PySide6.QtWidgets import QApplication, QDialog

from src.desktop_v2 import (
    UnitPriceChangeController,
    UnitPriceChangeDialog,
)
from src.domain import (
    UNIT_PRICE_SCHEDULE_ID,
    UNIT_PRICE_SCHEDULE_KIND,
    AuditChange,
    ChangeOperation,
    Meal,
    RevisionActor,
    RevisionMetadata,
    RevisionRecord,
    RevisionSource,
    UnitPriceChange,
    UnitPriceSchedule,
    unit_price_schedule_from_state,
)
from src.storage import DatabaseSession


BASE_REVISION_ID = UUID("78000000-0000-0000-0000-000000000001")
BASE_ENTITY_ID = UUID("78000000-0000-0000-0000-000000000002")
PRICE_REVISION_ID = UUID("78000000-0000-0000-0000-000000000003")
PRICE_CHANGE_ID = UUID("78000000-0000-0000-0000-000000000004")
ANCHOR_DAY = date(2026, 9, 1)


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
            reason="단가 화면 기준 명단",
            source=RevisionSource.UI,
        ),
        (
            AuditChange(
                "unit-price-ui-probe",
                BASE_ENTITY_ID,
                ChangeOperation.INSERT,
                before=None,
                after={"value": "base"},
            ),
        ),
    )


def test_dialog_requires_explicit_meal_selection_and_summarizes_shared_change(
    app: QApplication,
):
    dialog = UnitPriceChangeDialog(UnitPriceSchedule(), ANCHOR_DAY)

    assert dialog.selected_meals() == ()
    assert dialog.selected_unit_price() == 10_000
    assert not dialog._save_button.isEnabled()
    assert dialog._error.isHidden()
    assert "식종을 선택" in dialog._summary_text.text()
    assert "10,000원" in dialog._current_labels[Meal.BREAKFAST].text()

    dialog._meal_checks[Meal.BREAKFAST].setChecked(True)
    dialog._meal_checks[Meal.DINNER].setChecked(True)
    dialog._price.setValue(11_500)
    app.processEvents()

    assert dialog.selected_meals() == (Meal.BREAKFAST, Meal.DINNER)
    assert dialog.selected_effective_on() == ANCHOR_DAY
    assert dialog._save_button.isEnabled()
    assert "조식   10,000원 → 11,500원" in dialog._summary_text.text()
    assert "석식   10,000원 → 11,500원" in dialog._summary_text.text()
    dialog._save_button.click()
    assert dialog.result() == QDialog.DialogCode.Accepted


def test_dialog_uses_selected_date_for_current_price_and_blocks_no_op(
    app: QApplication,
):
    schedule = UnitPriceSchedule(
        changes=(
            UnitPriceChange(
                PRICE_CHANGE_ID,
                (Meal.LUNCH,),
                11_000,
                ANCHOR_DAY,
                datetime(2026, 8, 26, 2, 0, tzinfo=UTC),
            ),
        )
    )
    dialog = UnitPriceChangeDialog(schedule, ANCHOR_DAY)
    dialog._meal_checks[Meal.LUNCH].setChecked(True)
    dialog._price.setValue(11_000)
    app.processEvents()

    assert not dialog._save_button.isEnabled()
    assert "이미 같은 단가" in dialog._error.text()
    assert dialog._history.item(0, 1).text() == "중식"

    dialog._effective_on.setDate(QDate(2026, 8, 31))
    app.processEvents()
    assert "10,000원" in dialog._current_labels[Meal.LUNCH].text()
    assert dialog._save_button.isEnabled()
    dialog.close()


class _AcceptedDialog:
    def __init__(self, schedule, anchor_day, parent=None):
        del parent
        assert schedule == UnitPriceSchedule()
        assert anchor_day == ANCHOR_DAY

    def exec(self):
        return QDialog.DialogCode.Accepted

    @staticmethod
    def selected_meals():
        return (Meal.BREAKFAST, Meal.LUNCH)

    @staticmethod
    def selected_unit_price():
        return 11_500

    @staticmethod
    def selected_effective_on():
        return ANCHOR_DAY


class _Messages:
    @staticmethod
    def critical(*args):
        raise AssertionError(args)

    @staticmethod
    def warning(*args):
        raise AssertionError(args)


def test_controller_saves_price_revision_and_verified_backup_can_restore_it(
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
        outcome = UnitPriceChangeController(
            session,
            dialog_type=_AcceptedDialog,
            actor_provider=lambda: RevisionActor("tester", "S-1-5-21-test"),
            today_provider=lambda: ANCHOR_DAY,
            recorded_at_provider=lambda: datetime(
                2026, 8, 26, 2, 0, tzinfo=UTC
            ),
            revision_id_factory=lambda: PRICE_REVISION_ID,
            change_id_factory=lambda: PRICE_CHANGE_ID,
            message_box=_Messages,
        ).run()

        assert outcome.completed
        assert outcome.roster is not None
        assert outcome.roster.revision_id == PRICE_REVISION_ID
        assert outcome.schedule is not None
        assert outcome.schedule.price_for(Meal.BREAKFAST, ANCHOR_DAY) == 11_500
        assert outcome.backup_result is not None
        assert outcome.backup_result.record is not None
        backup_path = outcome.backup_result.record.path

    restored_path = tmp_path / "restored.sqlite3"
    with DatabaseSession.restore_and_open_with_user_password(
        backup_path,
        restored_path,
        "everyday-password",
    ) as restored:
        snapshot = restored.database.snapshot(PRICE_REVISION_ID)
        state = snapshot.entity(UNIT_PRICE_SCHEDULE_KIND, UNIT_PRICE_SCHEDULE_ID)
        schedule = unit_price_schedule_from_state(state)
        assert schedule.price_for(Meal.BREAKFAST, ANCHOR_DAY) == 11_500
        assert schedule.price_for(Meal.LUNCH, ANCHOR_DAY) == 11_500
        assert schedule.price_for(Meal.DINNER, ANCHOR_DAY) == 10_000
        restored.database.check_integrity()
