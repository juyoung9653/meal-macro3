"""Whole-file complaint browsing is searchable, read-only, and responsive."""

from src.storage.database_session import AutomaticBackupResult

from dataclasses import replace
from datetime import UTC, date, datetime
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
        assert dialog.model.rows[0][0] == date(2026, 10, 3)
        assert dialog.model.headings == (
            "접수일", "성명", "핸드폰", "식종", "채널", "내용", "변경자", "변경번호", "기록 시각(KST)",
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
        assert "\n" not in dialog.proxy.data(dialog.proxy.index(0, 5))
        assert dialog.proxy.data(dialog.proxy.index(0, 5), Qt.ItemDataRole.ToolTipRole) == database.complaints[1].content
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
    session = SimpleNamespace(database=database, retry_backup_on_open=lambda: AutomaticBackupResult(), credentials=SimpleNamespace(database_id=UUID(int=5)))

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



def test_header_filters_sort_and_export_preserve_full_values(app, tmp_path):
    import openpyxl
    from src.services.database_export import build_complaints_workbook
    dialog = ComplaintsHistoryDialog(SyntheticComplaintDatabase())
    try:
        assert not dialog.table.isColumnHidden(5)
        assert not dialog.table.isColumnHidden(6)
        dialog._cycle_sort(0)
        assert [row[0] for row in dialog.export_rows()] == [date(2026, 10, day) for day in (1, 2, 3)]
        dialog._cycle_sort(0)
        assert dialog.export_rows()[0][0] == date(2026, 10, 3)
        dialog._cycle_sort(0)
        assert not dialog.proxy.criteria
        dialog._set_column_values("channel", frozenset({"메일"}))
        assert dialog.proxy.rowCount() == 1
        dialog._set_column_search("content", "끝까지")
        rows = dialog.export_rows()
        assert len(rows) == 1 and "\n" in rows[0][5]
        book = build_complaints_workbook(dialog.model.headings[:-1], rows)
        target = tmp_path / "complaints.xlsx"
        book.save(target)
        book.close()
        saved = openpyxl.load_workbook(target)
        try:
            sheet = saved.active
            assert sheet["F2"].value == rows[0][5]
            assert sheet["G2"].value == "합성 담당자"
            assert sheet["H2"].value == rows[0][7]
            assert sheet["A2"].number_format == "yyyy-mm-dd"
            assert sheet.auto_filter.ref == "A1:H2"
        finally:
            saved.close()
        dialog._set_column_values("channel", frozenset())
        assert dialog.proxy.rowCount() == 0
        assert not dialog.export_button.isEnabled()
        dialog._clear_filters()
        assert dialog.proxy.rowCount() == 3
    finally:
        dialog.close()


def test_export_text_is_literal_and_long_text_is_not_truncated():
    from src.services.database_export import build_complaints_workbook
    headings = ("접수일", "성명", "식종", "채널", "내용", "변경자", "변경번호")
    row = (date(2026, 10, 28), "합성", "조식", "전화", "=1+1\n둘째 줄", "담당자", "20261028-10")
    book = build_complaints_workbook(headings, (row,))
    assert book.active["E2"].value == row[4] and book.active["E2"].data_type == "s"
    book.close()
    with pytest.raises(ValueError, match="32,767"):
        build_complaints_workbook(headings, ((*row[:4], "가" * 32768, *row[5:]),))



def test_export_button_encrypts_filtered_rows_and_cancel_does_not_write(app, tmp_path, monkeypatch):
    from io import BytesIO
    import msoffcrypto
    import openpyxl
    import src.desktop_v2.complaints_history as module
    dialog = ComplaintsHistoryDialog(SyntheticComplaintDatabase())
    target = tmp_path / "filtered.xlsx"
    notices, errors = [], []
    monkeypatch.setattr(module.QFileDialog, "getSaveFileName", lambda *a, **k: (str(target), ""))
    monkeypatch.setattr(module.QMessageBox, "information", lambda *a: notices.append(a))
    monkeypatch.setattr(module.QMessageBox, "critical", lambda *a: errors.append(a))
    monkeypatch.setattr(module, "require_export_password_setting", lambda snapshot: SimpleNamespace(password="synthetic-password"))
    try:
        dialog._set_column_values("channel", frozenset({"메일"}))
        dialog._export()
        assert target.is_file() and notices and not errors
        with target.open("rb") as source:
            office = msoffcrypto.OfficeFile(source)
            assert office.is_encrypted()
            office.load_key(password="synthetic-password")
            plain = BytesIO()
            office.decrypt(plain)
        book = openpyxl.load_workbook(plain)
        try:
            assert book.active.max_row == 2
            assert book.active["B2"].value == "합성 나래"
            assert "끝까지 보이는 내용" in book.active["F2"].value
        finally:
            book.close()
            plain.close()
        before = target.read_bytes()
        monkeypatch.setattr(module.QMessageBox, "question", lambda *a: module.QMessageBox.StandardButton.No)
        dialog._export()
        assert target.read_bytes() == before
        monkeypatch.setattr(module.QFileDialog, "getSaveFileName", lambda *a, **k: ("", ""))
        dialog._export()
        assert target.read_bytes() == before
        assert not errors
    finally:
        dialog.close()


def test_revision_sort_uses_numeric_suffix_and_header_picker_applies(app):
    from src.desktop_v2.column_search import ColumnValuePicker
    dialog = ComplaintsHistoryDialog(SyntheticComplaintDatabase())
    try:
        rows = list(dialog.model.rows)
        dialog.model.beginResetModel()
        dialog.model.rows = tuple((*row[:7], code, row[8]) for row, code in zip(rows, ("20261004-10", "20261004-2", "20261004-1")))
        dialog.model.endResetModel()
        dialog._cycle_sort(7)
        assert [row[7] for row in dialog.export_rows()] == ["20261004-1", "20261004-2", "20261004-10"]
        dialog.header.open_value_filter("name")
        picker = dialog.header._menu.findChild(ColumnValuePicker)
        picker.applied.emit("나래", None)
        assert dialog.proxy.rowCount() == 1
        assert dialog.export_rows()[0][1] == "합성 나래"
    finally:
        dialog.close()


def test_household_columns_follow_saved_multiple_criteria_in_search_filter_and_export(app):
    from src.domain.household_grouping import GROUPING_ID, GROUPING_KIND
    from src.domain.roster import CustomFieldDefinition, CustomFieldType
    database = SyntheticComplaintDatabase()
    custom_id = UUID(int=902)
    field = CustomFieldDefinition(custom_id, "가구 확인값", CustomFieldType.TEXT, 0)
    database.roster = replace(database.roster, custom_fields=(field,), persons=tuple(
        replace(person, profile=replace(person.profile, household_head_name="합성 세대주",
            custom_values={custom_id: "기준 A" if index == 0 else "기준 B"}))
        for index, person in enumerate(database.roster.persons)))
    database.state = HistoricalSnapshot(database.state.revision_id, {
        **database.state.entities,
        EntityRef(GROUPING_KIND, GROUPING_ID): {"match_mode": "all", "columns": ["household_head_name", f"custom:{custom_id}"]},
    })
    dialog = ComplaintsHistoryDialog(database)
    try:
        assert dialog.model.headings[:5] == ("접수일", "성명", "세대주 성명", "가구 확인값", "식종")
        assert dialog.model.rows[1][2:4] == ("", "")  # deleted person's values are unknown
        dialog.search.setText("기준 A")
        assert dialog.proxy.rowCount() == 1
        assert dialog.export_rows()[0][1:4] == ("합성 가람", "합성 세대주", "기준 A")
        dialog.search.clear()
        dialog._set_column_values(f"household:custom:{custom_id}", frozenset({"기준 B"}))
        assert dialog.export_rows()[0][1] == "합성 나래"
        assert dialog.column_filter_values("household:household_head_name") == ("", "합성 세대주")
    finally:
        dialog.close()
    database.state = HistoricalSnapshot(database.state.revision_id, {
        **database.state.entities,
        EntityRef(GROUPING_KIND, GROUPING_ID): {"match_mode": "all", "columns": ["name", "mobile"]},
    })
    reopened = ComplaintsHistoryDialog(database)
    assert reopened.model.headings[:4] == ("접수일", "성명", "핸드폰", "식종")
    reopened.close()
