"""Corrections stay per-action, revision-backed and independent of delivery facts."""
from datetime import timedelta
from uuid import uuid4

import pytest
from PySide6.QtWidgets import QApplication, QDialog, QTableView

from src.desktop_v2.meal_history import build_meal_history_entries_by_person
from src.desktop_v2.meal_history_details import MealHistoryDetailsController, MealHistoryDetailsDialog
from src.desktop_v2.person_history import create_person_history_dialog
from src.domain.meal_history_details import build_meal_history_details_revision
from src.domain.status import MealStatus
from src.domain.roster import Meal
from src.services.monthly_meal_history import build_monthly_meal_history
from src.services.revision_restore import restore_revision
from tests.test_meal_history_cancellation import (
    ACTOR, DAY, STAMP, Messages, initialize, load_revision_chain, rows, session, status, cancel,
)


def prepare(session, target, channel="전화", note="수정 비고"):
    current = session.database.current_roster()
    return build_meal_history_details_revision(
        session.database.snapshot(current.revision_id), load_revision_chain(session.database, current.revision_id),
        target, channel, note, actor=ACTOR, recorded_at=STAMP + timedelta(hours=1), revision_id=uuid4())


def test_details_persist_without_changing_status_and_restore(session):
    pid = initialize(session)
    target = status(session, pid, MealStatus.STOPPED)
    original = session.database.current_roster().revision_id
    original_record = session.database.revision(target.revision_id)
    record = prepare(session, target)
    session.append_revision(record)
    assert session.database.status_history(pid, Meal.DINNER).status_on(DAY) is MealStatus.STOPPED
    assert session.database.revision(target.revision_id) == original_record
    assert rows(session, pid)[-1][2:5:2] == ("전화", "수정 비고")
    current = session.database.current_roster()
    monthly = build_monthly_meal_history(session.database.snapshot(current.revision_id),
        load_revision_chain(session.database, current.revision_id), DAY.year, DAY.month)
    assert [(r.intake_channel, r.note, r.target) for r in monthly.entries] == [("전화", "수정 비고", target)]
    assert prepare(session, target) is None
    session.append_revision(prepare(session, target, "", ""))
    assert rows(session, pid)[-1][2:5:2] == ("-", "")
    restore_revision(session, record.metadata.revision_id, session.database.current_roster().revision_id, ACTOR)
    assert rows(session, pid)[-1][2:5:2] == ("전화", "수정 비고")
    restore_revision(session, original, session.database.current_roster().revision_id, ACTOR)
    assert rows(session, pid)[-1][2:5:2] == ("-", "")
    session.append_revision(prepare(session, target, "메일", "복구 후 수정"))
    session.database.check_integrity()
    assert rows(session, pid)[-1][2:5:2] == ("메일", "복구 후 수정")


def test_edit_one_action_does_not_touch_other_actions_and_cancel_still_works(session):
    pid = initialize(session, row_add=True)
    target = build_meal_history_entries_by_person(load_revision_chain(session.database,
        session.database.current_roster().revision_id))[pid][0].target
    stopped = status(session, pid, MealStatus.STOPPED)
    session.append_revision(prepare(session, stopped))
    assert rows(session, pid)[0][2:5:2] == ("-", "")
    assert rows(session, pid)[1][2:5:2] == ("전화", "수정 비고")
    cancel(session, stopped)
    with pytest.raises(ValueError):
        prepare(session, stopped)
    session.append_revision(prepare(session, target, "메일", "가입 확인"))
    assert rows(session, pid)[0][2:5:2] == ("메일", "가입 확인")


def test_controller_and_person_view_refresh(session):
    app = QApplication.instance() or QApplication([])
    pid = initialize(session)
    target = status(session, pid, MealStatus.STOPPED)
    view = create_person_history_dialog(session, session.database.current_roster().person(pid), Meal.DINNER)
    requested = []
    view.meal_history_details_requested.connect(requested.append)
    table = view.findChild(QTableView, "history_식종 변경")
    view._edit_meal_details(table.model().index(0, 2))
    assert requested == [target]
    class EditDialog(MealHistoryDetailsDialog):
        def exec(self):
            self.channel.setCurrentText("기타")
            self.note.setPlainText("확인 완료")
            return QDialog.DialogCode.Accepted
    controller = MealHistoryDetailsController(session, dialog_type=EditDialog, message_box=Messages())
    assert controller.run(target, view.expected_revision_id, view) is not None
    view.reload_meal_history(session)
    assert table.model().rows[0][2:5:2] == ("기타", "확인 완료")
    view.close()


def test_cancel_editor_does_not_write_and_stale_revision_is_rejected(session):
    app = QApplication.instance() or QApplication([])
    pid = initialize(session)
    target = status(session, pid, MealStatus.STOPPED)
    expected = session.database.current_roster().revision_id
    class CancelDialog(MealHistoryDetailsDialog):
        def exec(self):
            return QDialog.DialogCode.Rejected
    controller = MealHistoryDetailsController(session, dialog_type=CancelDialog, message_box=Messages())
    assert controller.run(target, expected) is None
    assert session.database.current_roster().revision_id == expected
    session.append_revision(prepare(session, target))
    assert controller.run(target, expected) is None
