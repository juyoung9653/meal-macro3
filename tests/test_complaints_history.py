"""Whole-file complaint browsing is searchable, read-only, and responsive."""

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

import pytest
from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtWidgets import QApplication, QDialog, QWidget

from src.desktop_v2.application import DesktopStartupController, StartupStatus
from src.desktop_v2.complaints_history import ComplaintsHistoryDialog
from src.desktop_v2.roster_window import RosterWindow
from src.domain.complaints import Complaint, complaint_state
from src.domain.intake import IntakeChannel
from src.domain.projections import PERSON_KIND
from src.domain.revisions import (
    AuditChange, ChangeOperation, EntityRef, HistoricalSnapshot, RevisionActor,
    RevisionMetadata, RevisionRecord, RevisionSource,
)
from src.domain.roster import Meal
from tests.test_desktop_v2_roster import _roster


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


class SyntheticComplaintDatabase:
    """Expose only read operations; unexpected storage calls fail the test."""

    def __init__(self, *, empty=False):
        base = _roster()
        self.roster = replace(base, persons=tuple(
            replace(person, profile=replace(person.profile, name=name))
            for person, name in zip(base.persons, ("합성 가람", "합성 나래"))
        ))
        self.calls = []
        self.threads = []
        self.complaints = () if empty else (
            Complaint(UUID(int=401), base.persons[0].person_id,
                      datetime(2026, 10, 1, 2, tzinfo=UTC), IntakeChannel.PHONE,
                      "용기 확인 요청", meal=Meal.LUNCH),
            Complaint(UUID(int=402), base.persons[1].person_id,
                      datetime(2026, 10, 3, 3, tzinfo=UTC), IntakeChannel.EMAIL,
                      "배송 시각 확인\n" + "긴 합성 내용 " * 500 + "끝까지 보이는 내용", meal=Meal.BREAKFAST),
            Complaint(UUID(int=403), UUID(int=999),
                      datetime(2026, 10, 2, 4, tzinfo=UTC), IntakeChannel.OTHER,
                      "과거 대상자의 접수 기록", meals=(Meal.BREAKFAST, Meal.DINNER)),
        )
        changes = tuple(
            AuditChange("complaint", item.complaint_id, ChangeOperation.INSERT, None, complaint_state(item))
            for item in self.complaints
        ) + (AuditChange(PERSON_KIND, UUID(int=999), ChangeOperation.DELETE,
                         {"name": "합성 이전 대상자"}, None),)
        self.record = RevisionRecord(RevisionMetadata(
            base.revision_id, None, datetime(2026, 10, 4, 1, tzinfo=UTC), None,
            RevisionActor("합성 담당자", "synthetic-sid"), "합성 접수", RevisionSource.UI,
        ), changes)
        self.state = HistoricalSnapshot(base.revision_id, {
            EntityRef("complaint", item.complaint_id): complaint_state(item)
            for item in self.complaints
        })

    def _read(self, name):
        self.calls.append(name)
        self.threads.append(QThread.currentThread())

    def current_roster(self):
        self._read("current_roster")
        return self.roster

    def snapshot(self, revision_id):
        assert revision_id == self.roster.revision_id
        self._read("snapshot")
        return self.state

    def revision(self, revision_id):
        assert revision_id == self.roster.revision_id
        self._read("revision")
        return self.record


def test_entire_file_ignores_roster_filters_keeps_deleted_names_and_newest_first(app):
    database = SyntheticComplaintDatabase()
    before = database.state
    window = RosterWindow(database.roster)
    window.table_model.set_column_search("name", "대상자 없음")
    assert window.table_model.rowCount() == 0
    dialog = ComplaintsHistoryDialog(database, window)
    try:
        assert dialog.model.rowCount() == 3
        assert [row[1] for row in dialog.model.rows] == ["합성 나래", "합성 이전 대상자", "합성 가람"]
        assert dialog.model.rows[0][0] == datetime(2026, 10, 3, 12)
        assert dialog.model.headings == (
            "접수 시각(KST)", "성명", "식종", "채널", "내용", "기록 시각(KST)", "변경자",
        )
        assert all("UUID" not in heading and "ID" not in heading for heading in dialog.model.headings)
        assert database.calls == ["current_roster", "snapshot", "revision"]
        assert all(thread != app.thread() for thread in database.threads)
        assert dialog.thread() == app.thread()
        assert database.state is before
        assert not dialog.model.flags(dialog.model.index(0, 0)) & Qt.ItemFlag.ItemIsEditable
        assert not dialog.model.setData(dialog.model.index(0, 0), "수정")
        assert dialog.details.isReadOnly()
    finally:
        dialog.close()
        window.close()


@pytest.mark.parametrize("query,expected_name", (
    ("가람", "합성 가람"), ("중식", "합성 가람"), ("메일", "합성 나래"),
    ("과거 대상자", "합성 이전 대상자"), ("끝까지 보이는 내용", "합성 나래"),
))
def test_searches_name_meal_channel_full_content_and_clear_restores_all(app, query, expected_name):
    dialog = ComplaintsHistoryDialog(SyntheticComplaintDatabase())
    try:
        dialog.search.setText(query)
        assert dialog.proxy.rowCount() == 1
        assert f"성명: {expected_name}" in dialog.details.toPlainText()
        assert "검색 결과 1건" in dialog.count_label.text()
        dialog.search.clear()
        assert dialog.proxy.rowCount() == 3
        assert dialog.count_label.text() == "전체 3건"
        assert "성명: 합성 나래" in dialog.details.toPlainText()
    finally:
        dialog.close()


def test_full_details_change_with_selection_and_no_matches_clears_details(app):
    database = SyntheticComplaintDatabase()
    dialog = ComplaintsHistoryDialog(database)
    try:
        assert database.complaints[1].content in dialog.details.toPlainText()
        assert dialog.table.textElideMode() == Qt.TextElideMode.ElideNone
        assert "\n" not in dialog.proxy.data(dialog.proxy.index(0, 4))
        assert dialog.proxy.data(dialog.proxy.index(0, 4), Qt.ItemDataRole.ToolTipRole) == database.complaints[1].content
        assert "기록 시각(KST): 2026-10-04 10:00" in dialog.details.toPlainText()
        assert "변경자: 합성 담당자" in dialog.details.toPlainText()
        assert "00000000-" not in dialog.details.toPlainText()
        dialog.table.selectRow(1)
        assert "성명: 합성 이전 대상자" in dialog.details.toPlainText()
        assert "식종: 조식, 석식" in dialog.details.toPlainText()
        dialog.search.setText("없는 검색어")
        assert dialog.proxy.rowCount() == 0
        assert dialog.details.toPlainText() == ""
        assert "검색 결과가 없습니다" in dialog.count_label.text()
        assert database.calls == ["current_roster", "snapshot", "revision"]
    finally:
        dialog.close()


def test_empty_dialog_and_minimum_viewport_are_readable(app):
    dialog = ComplaintsHistoryDialog(SyntheticComplaintDatabase(empty=True))
    try:
        dialog.resize(1080, 680)
        dialog.show()
        app.processEvents()
        assert dialog.proxy.rowCount() == 0
        assert dialog.count_label.text() == "등록된 민원이 없습니다."
        assert dialog.details.toPlainText() == ""
        assert dialog.table.horizontalScrollBar().maximum() == 0
        assert dialog.details.height() >= 100
    finally:
        dialog.close()


def test_startup_wires_complaints_signal_to_read_only_dialog(app):
    roster = SimpleNamespace(persons=(object(),), revision_id=UUID(int=1))
    database = SimpleNamespace(
        current_roster=lambda: roster,
        snapshot=lambda revision_id: HistoricalSnapshot(revision_id, {}),
    )
    session = SimpleNamespace(database=database, credentials=SimpleNamespace(database_id=UUID(int=5)))

    class Window(QWidget):
        complaint_history_requested = Signal()

        def __init__(self, displayed, *_args, **_kwargs):
            super().__init__()
            self.roster = displayed

    opened = []

    class Dialog(QDialog):
        def __init__(self, current, parent):
            super().__init__(parent)
            self.setModal(True)
            opened.append((current, parent))

    controller = DesktopStartupController(
        settings=SimpleNamespace(), roster_window_type=Window,
        complaint_history_dialog_type=Dialog,
    )
    controller._histories = lambda *_: {}
    controller._revision_display_code = lambda *_: "합성 변경번호"
    access = SimpleNamespace(session=session, backup_result=None, exec=lambda: QDialog.DialogCode.Accepted)
    outcome = controller._start_with_dialog(access, None)
    assert outcome.status is StartupStatus.READY
    outcome.window.show()
    outcome.window.complaint_history_requested.emit()
    dialog = outcome.window.findChild(Dialog)
    assert opened == [(database, outcome.window)]
    assert dialog is not None and dialog.isVisible()
    assert dialog.parentWidget() is outcome.window
    assert not dialog.isModal()
    assert QApplication.activeModalWidget() is None
    assert len(outcome.window._dialog_workflows) == 1
    outcome.window.close()
    assert not outcome.window._dialog_workflows
    assert not dialog.isVisible()


def test_application_catches_load_error_without_showing_complaint_content(app):
    errors = []

    def broken(*_args):
        raise ValueError("민감한 원본 민원")

    controller = DesktopStartupController(
        complaint_history_dialog_type=broken,
        message_box=SimpleNamespace(critical=lambda *args: errors.append(args)),
    )
    controller._run_complaint_history(SimpleNamespace(database=object()), None)
    assert errors == [(None, "전체 민원을 열지 못했습니다", "민원을 읽지 못했습니다. 창을 다시 열어주세요.")]


def test_real_encrypted_database_browse_search_and_close_keep_ledger_unchanged(app, tmp_path):
    from src.domain.complaints import build_complaint_revision
    from src.storage.database_session import DatabaseSession
    from tests.test_complaints import ACTOR, COMPLAINT, NEXT, PERSON, RECEIVED, _initial_record
    from tests.test_complaints import _roster as complaint_roster

    roster = complaint_roster()
    roster = replace(roster, persons=(
        replace(roster.persons[0], profile=replace(roster.persons[0].profile, name="합성 대상자")),
    ))
    with DatabaseSession.create(tmp_path / "synthetic.sqlite3", "password", "recovery") as session:
        session.append_revision(_initial_record(roster))
        session.append_revision(build_complaint_revision(
            roster, Complaint(COMPLAINT, PERSON, RECEIVED, IntakeChannel.EMAIL, "합성 접수 내용"),
            actor=ACTOR, recorded_at=RECEIVED, revision_id=NEXT,
        ))
        before = session.database.revision_summaries()
        snapshot = session.database.snapshot(NEXT)
        dialog = ComplaintsHistoryDialog(session.database)
        dialog.search.setText("합성 접수")
        assert dialog.proxy.rowCount() == 1
        assert "성명: 합성 대상자" in dialog.details.toPlainText()
        dialog.close()
        assert session.database.revision_summaries() == before
        assert session.database.snapshot(NEXT) == snapshot
