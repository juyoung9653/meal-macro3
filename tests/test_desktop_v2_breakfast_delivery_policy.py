"""Offscreen breakfast-policy editor and automatic-backup checks."""

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
    BreakfastDeliveryPolicyController,
    BreakfastDeliveryPolicyDialog,
)
from src.domain import (
    BREAKFAST_DELIVERY_POLICY_ID,
    BREAKFAST_DELIVERY_POLICY_KIND,
    AuditChange,
    BreakfastDeliveryPolicyChange,
    BreakfastDeliveryPolicySchedule,
    ChangeOperation,
    ClosedFridayMode,
    RevisionActor,
    RevisionMetadata,
    RevisionRecord,
    RevisionSource,
    breakfast_delivery_policy_from_state,
)
from src.storage import DatabaseSession


BASE_REVISION_ID = UUID("bf100000-0000-0000-0000-000000000001")
BASE_ENTITY_ID = UUID("bf100000-0000-0000-0000-000000000002")
POLICY_REVISION_ID = UUID("bf100000-0000-0000-0000-000000000003")
POLICY_CHANGE_ID = UUID("bf100000-0000-0000-0000-000000000004")
ANCHOR_DAY = date(2026, 9, 1)


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _base_revision() -> RevisionRecord:
    return RevisionRecord(
        RevisionMetadata(
            revision_id=BASE_REVISION_ID,
            parent_revision_id=None,
            recorded_at=datetime(2026, 8, 31, 1, 0, tzinfo=UTC),
            effective_on=date(2026, 8, 1),
            actor=RevisionActor("tester", "S-1-5-21-test"),
            reason="조식 규칙 화면 기준 명단",
            source=RevisionSource.UI,
        ),
        (
            AuditChange(
                "breakfast-policy-ui-probe",
                BASE_ENTITY_ID,
                ChangeOperation.INSERT,
                before=None,
                after={"value": "base"},
            ),
        ),
    )


def test_dialog_explains_both_modes_and_uses_the_effective_date(app):
    existing = BreakfastDeliveryPolicyChange(
        POLICY_CHANGE_ID,
        ANCHOR_DAY,
        ClosedFridayMode.SKIP_FRIDAY_WEEKEND,
        datetime(2026, 8, 31, 2, 0, tzinfo=UTC),
    )
    dialog = BreakfastDeliveryPolicyDialog(
        BreakfastDeliveryPolicySchedule((existing,)),
        ANCHOR_DAY,
    )

    assert dialog.selected_mode() is ClosedFridayMode.SKIP_FRIDAY_WEEKEND
    assert dialog.selected_effective_on() == ANCHOR_DAY
    assert "금·토·일분 모두 미배송" in dialog._current_summary.text()
    assert "금·토·일분 미배송" in dialog._mode_buttons[
        ClosedFridayMode.SKIP_FRIDAY_WEEKEND
    ].accessibleDescription()

    dialog._effective_on.setDate(QDate(2026, 8, 31))
    dialog._mode_buttons[
        ClosedFridayMode.THURSDAY_WEEKEND_ONLY
    ].setChecked(True)
    app.processEvents()

    assert dialog.selected_effective_on() == date(2026, 8, 31)
    assert dialog.selected_mode() is ClosedFridayMode.THURSDAY_WEEKEND_ONLY
    assert "기본 규칙" in dialog._current_summary.text()
    assert dialog._mode_cards[
        ClosedFridayMode.THURSDAY_WEEKEND_ONLY
    ].property("selected")
    dialog.close()


class _AcceptedDialog:
    def __init__(self, schedule, anchor_day, parent=None):
        del parent
        assert schedule == BreakfastDeliveryPolicySchedule()
        assert anchor_day == ANCHOR_DAY

    @staticmethod
    def exec():
        return QDialog.DialogCode.Accepted

    @staticmethod
    def selected_mode():
        return ClosedFridayMode.SKIP_FRIDAY_WEEKEND

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


def test_controller_saves_policy_revision_and_verified_backup_can_restore_it(
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
        outcome = BreakfastDeliveryPolicyController(
            session,
            dialog_type=_AcceptedDialog,
            actor_provider=lambda: RevisionActor("tester", "S-1-5-21-test"),
            recorded_at_provider=lambda: datetime(
                2026, 8, 31, 2, 0, tzinfo=UTC
            ),
            revision_id_factory=lambda: POLICY_REVISION_ID,
            change_id_factory=lambda: POLICY_CHANGE_ID,
            message_box=_Messages,
        ).run(ANCHOR_DAY)

        assert outcome.completed
        assert outcome.changed
        assert outcome.roster is not None
        assert outcome.roster.revision_id == POLICY_REVISION_ID
        assert outcome.schedule is not None
        assert outcome.schedule.mode_on(ANCHOR_DAY) is (
            ClosedFridayMode.SKIP_FRIDAY_WEEKEND
        )
        assert outcome.backup_result is not None
        assert outcome.backup_result.record is not None
        backup_path = outcome.backup_result.record.path

    restored_path = tmp_path / "restored.sqlite3"
    with DatabaseSession.restore_and_open_with_user_password(
        backup_path,
        restored_path,
        "everyday-password",
    ) as restored:
        snapshot = restored.database.snapshot(POLICY_REVISION_ID)
        state = snapshot.entity(
            BREAKFAST_DELIVERY_POLICY_KIND,
            BREAKFAST_DELIVERY_POLICY_ID,
        )
        schedule = breakfast_delivery_policy_from_state(state)
        assert schedule.mode_on(ANCHOR_DAY) is (
            ClosedFridayMode.SKIP_FRIDAY_WEEKEND
        )
        assert restored.database.revision(POLICY_REVISION_ID).metadata.decisions[
            "breakfast_delivery_policy_change"
        ]["effective_on"] == ANCHOR_DAY.isoformat()
        restored.database.check_integrity()
