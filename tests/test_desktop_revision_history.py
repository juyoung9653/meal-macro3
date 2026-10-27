"""Read-only global revision history browser contracts."""

from __future__ import annotations

import os
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QTimer, Qt
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
    QLineEdit,
    QWidget,
    QHeaderView,
    QPlainTextEdit,
    QPushButton,
    QTableView,
    QWidget,
)

from src.desktop_v2.application import DesktopStartupController, StartupStatus
from src.desktop_v2.person_history import PersonHistoryDialog
from src.desktop_v2.revision_history import GlobalRevisionHistoryDialog
from src.domain.revisions import HistoricalSnapshot, RevisionActor, RevisionSource, RevisionSummary
from src.domain.roster import Meal
from tests.test_revision_restore import history

REVISION_1 = UUID("96000000-0000-0000-0000-000000000001")
REVISION_2 = UUID("96000000-0000-0000-0000-000000000002")
REVISION_3 = UUID("96000000-0000-0000-0000-000000000003")


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _summary(sequence, revision_id, code, reason, actor):
    return RevisionSummary(
        sequence=sequence,
        revision_id=revision_id,
        display_code=code,
        recorded_at=datetime(2026, 10, 14, sequence, tzinfo=UTC),
        effective_on=date(2026, 10, 14),
        actor=RevisionActor(actor, f"sid-{sequence}"),
        reason=reason,
        source=RevisionSource.UI,
    )


class _Database:
    def __init__(self):
        self.summaries = (
            _summary(1, REVISION_1, "20261014-100000", "첫 저장", "박담당"),
            _summary(3, REVISION_3, "20261014-110000", "학교명 수정", "김담당"),
            _summary(2, REVISION_2, "20261014-100000", "같은 번호의 저장", "이담당"),
        )
        self.parents = {
            REVISION_1: None,
            REVISION_2: REVISION_1,
            REVISION_3: REVISION_2,
        }
        self.revision_calls = []
        self.snapshot_calls = []
        self.summary_calls = 0

    def revision_summaries(self):
        self.summary_calls += 1
        return self.summaries

    def revision(self, revision_id):
        self.revision_calls.append(revision_id)
        return SimpleNamespace(
            revision_id=revision_id,
            metadata=SimpleNamespace(parent_revision_id=self.parents[revision_id]),
        )

    def snapshot(self, revision_id):
        self.snapshot_calls.append(revision_id)
        return SimpleNamespace(revision_id=revision_id)


def _safe_rows(record, snapshot, *, previous_snapshot=None):
    parent = "없음" if previous_snapshot is None else str(previous_snapshot.revision_id)
    return ((str(snapshot.revision_id), "", "학교", f"부모: {parent}"),)


def _select_revision(dialog, revision_id):
    for row in range(dialog._summary_proxy.rowCount()):
        index = dialog._summary_proxy.index(row, 0)
        if dialog._summary_proxy.data(index, Qt.ItemDataRole.UserRole) == revision_id:
            dialog._summary_table.selectRow(row)
            QApplication.processEvents()
            return
    raise AssertionError(f"revision is not visible: {revision_id}")


def test_dialog_orders_newest_searches_metadata_and_lazily_reads_selection(app):
    database = _Database()
    dialog = GlobalRevisionHistoryDialog(
        database,
        change_rows_builder=_safe_rows,
    )
    dialog.show()
    dialog.resize(1080, 680)
    app.processEvents()

    assert dialog.minimumWidth() == 1080
    assert dialog.minimumHeight() == 680
    assert dialog._summary_model.summaries[0].revision_id == REVISION_3
    assert database.revision_calls == [REVISION_3]
    assert database.snapshot_calls == [REVISION_3, REVISION_2]
    assert dialog.selected_revision_id == REVISION_3
    assert dialog._detail_model.rows[0][0] == str(REVISION_3)
    assert dialog._summary_table.horizontalScrollBar().maximum() == 0
    assert dialog._summary_model.columnCount() == 1
    assert dialog._summary_model.data(dialog._summary_model.index(0, 0)).startswith("학교명 수정\n")
    assert dialog._detail_model.columnCount() == 3
    assert dialog._detail_model.headerData(0, Qt.Orientation.Horizontal) == "대상"
    assert dialog._detail_model.headerData(1, Qt.Orientation.Horizontal) == "항목"
    assert dialog._detail_model.headerData(2, Qt.Orientation.Horizontal) == "변경 내용"
    assert dialog._detail_model.data(dialog._detail_model.index(0, 0)) == str(REVISION_3)
    assert dialog._detail_model.data(dialog._detail_model.index(0, 1)) == "학교"
    assert dialog._detail_model.data(dialog._detail_model.index(0, 2)) == f"부모: {REVISION_2}"
    assert dialog._detail_table.horizontalScrollBar().maximum() == 0
    dialog.configure_actions(
        SimpleNamespace(path=Path("synthetic.sqlite3")), SimpleNamespace(), lambda: None,
    )
    assert not any(button.text() == "다른 운영 파일 열기" for button in dialog.findChildren(QPushButton))
    assert dialog._restore_button.isVisible()
    assert dialog._restore_button.isEnabled()

    dialog._search.setText("김담당")
    QTest.qWait(550)
    assert dialog._summary_proxy.rowCount() == 1
    assert dialog.selected_revision_id == REVISION_3

    dialog._search.setText("같은 번호의 저장")
    QTest.qWait(550)
    assert dialog._summary_proxy.rowCount() == 1
    assert dialog.selected_revision_id == REVISION_2

    dialog._search.setText("20261014-100000")
    QTest.qWait(550)
    assert dialog._summary_proxy.rowCount() == 2
    assert dialog.selected_revision_id == REVISION_2
    dialog._summary_table.selectRow(1)
    app.processEvents()
    assert dialog.selected_revision_id == REVISION_1
    assert dialog._detail_model.rows[0][0] == str(REVISION_1)
    dialog.close()


def test_dialog_empty_search_error_and_readonly_states_are_bounded(app):
    empty_database = _Database()
    empty_database.summaries = ()
    empty = GlobalRevisionHistoryDialog(
        empty_database,
        change_rows_builder=_safe_rows,
    )
    assert empty._summary_model.rowCount() == 0
    assert empty._summary_status.text() == "저장된 변경 이력이 없습니다."
    assert empty.selected_revision_id is None
    empty.close()

    database = _Database()
    dialog = GlobalRevisionHistoryDialog(database, change_rows_builder=_safe_rows)
    dialog._search.setText("검색되지 않는 값")
    QTest.qWait(550)
    assert dialog._summary_proxy.rowCount() == 0
    assert dialog._summary_status.text() == "검색 결과가 없습니다."
    assert dialog._detail_model.rowCount() == 0
    assert dialog._summary_table.editTriggers().value == 0
    assert dialog._detail_table.editTriggers().value == 0
    dialog.close()

    class FailingDatabase:
        def revision_summaries(self):
            raise RuntimeError("password=never-show-this")

    failed = GlobalRevisionHistoryDialog(FailingDatabase())
    assert "읽지 못했습니다" in failed._summary_status.text()
    assert "never-show-this" not in failed._summary_status.text()
    failed.close()


def test_selection_clears_stale_rows_and_never_echoes_detail_exception(app):
    database = _Database()

    def builder(record, snapshot, *, previous_snapshot=None):
        if snapshot.revision_id == REVISION_2:
            raise RuntimeError("password=secret-payload")
        return _safe_rows(
            record,
            snapshot,
            previous_snapshot=previous_snapshot,
        )

    dialog = GlobalRevisionHistoryDialog(database, change_rows_builder=builder)
    assert dialog._detail_model.rowCount() == 1
    dialog._search.setText("같은 번호의 저장")
    QTest.qWait(550)
    assert dialog.selected_revision_id == REVISION_2
    assert dialog._detail_model.rowCount() == 0
    assert "읽지 못했습니다" in dialog._detail_status.text()
    assert "secret-payload" not in dialog._detail_status.text()
    dialog.close()


def test_search_waits_for_last_input_without_reading_database_while_typing(app):
    database = _Database()
    dialog = GlobalRevisionHistoryDialog(database, change_rows_builder=_safe_rows)
    dialog.show()
    reads = list(database.revision_calls)
    snapshots = list(database.snapshot_calls)
    applied = QSignalSpy(dialog._search_timer.timeout)
    dialog._search.setText("학교")
    QTest.qWait(250)
    dialog._search.setText("같은 번호의 저장")
    QTest.qWait(300)
    assert applied.count() == 0
    assert dialog._summary_proxy.rowCount() == 3
    assert database.revision_calls == reads
    assert database.snapshot_calls == snapshots
    QTest.qWait(250)
    assert applied.count() == 1
    assert dialog.selected_revision_id == REVISION_2
    assert database.summary_calls == 1
    assert database.revision_calls[-1] == REVISION_2
    dialog._search.clear()
    assert dialog._summary_proxy.rowCount() == 1
    QTest.qWait(550)
    assert dialog._summary_proxy.rowCount() == 3
    assert database.summary_calls == 1
    dialog.close()


def test_filter_preserves_visible_selection_without_reset_or_database_read(app):
    database = _Database()
    dialog = GlobalRevisionHistoryDialog(database, change_rows_builder=_safe_rows)
    dialog.show()
    dialog.activateWindow()
    dialog._search.setFocus()
    app.processEvents()
    assert dialog._search.hasFocus()
    resets = QSignalSpy(dialog._detail_model.modelReset)
    revision_calls = list(database.revision_calls)
    snapshot_calls = list(database.snapshot_calls)

    dialog._search.setText("학교명 수정")
    dialog._search.setSelection(1, 3)
    QTest.qWait(550)

    assert dialog.selected_revision_id == REVISION_3
    assert resets.count() == 0
    assert database.revision_calls == revision_calls
    assert database.snapshot_calls == snapshot_calls
    assert dialog._search.hasFocus()
    assert dialog._search.selectionStart() == 1
    assert dialog._search.selectedText() == "교명 "
    dialog.close()


def test_detail_rows_cache_loads_new_selection_once_and_is_cleared_on_close(app):
    database = _Database()
    dialog = GlobalRevisionHistoryDialog(database, change_rows_builder=_safe_rows)

    _select_revision(dialog, REVISION_2)
    assert database.revision_calls == [REVISION_3, REVISION_2]
    assert database.snapshot_calls == [REVISION_3, REVISION_2, REVISION_2, REVISION_1]
    _select_revision(dialog, REVISION_3)
    _select_revision(dialog, REVISION_2)
    assert database.revision_calls == [REVISION_3, REVISION_2]
    assert database.snapshot_calls == [REVISION_3, REVISION_2, REVISION_2, REVISION_1]
    assert set(dialog._detail_cache) == {REVISION_2, REVISION_3}

    dialog.close()
    assert not dialog._detail_cache
    assert dialog._detail_cache_text_size == 0


def test_detail_rows_cache_evicts_lru_and_bypasses_oversize_rows(app, monkeypatch):
    from src.desktop_v2 import revision_history

    monkeypatch.setattr(revision_history, "_DETAIL_CACHE_MAX_ENTRIES", 2)
    database = _Database()
    dialog = GlobalRevisionHistoryDialog(database, change_rows_builder=_safe_rows)
    _select_revision(dialog, REVISION_2)
    _select_revision(dialog, REVISION_1)
    assert tuple(dialog._detail_cache) == (REVISION_2, REVISION_1)
    _select_revision(dialog, REVISION_3)
    assert database.revision_calls.count(REVISION_3) == 2
    dialog.close()

    monkeypatch.setattr(revision_history, "_DETAIL_CACHE_MAX_TEXT_SIZE", 1)
    database = _Database()
    dialog = GlobalRevisionHistoryDialog(database, change_rows_builder=_safe_rows)
    assert not dialog._detail_cache
    _select_revision(dialog, REVISION_2)
    _select_revision(dialog, REVISION_3)
    assert database.revision_calls.count(REVISION_3) == 2
    assert not dialog._detail_cache
    dialog.close()


def test_failed_detail_is_not_cached_and_retries_after_reselection(app):
    database = _Database()
    attempts = 0

    def rows_with_one_failure(record, snapshot, *, previous_snapshot=None):
        nonlocal attempts
        if record.revision_id == REVISION_2:
            attempts += 1
            if attempts == 1:
                raise RuntimeError("one-time read failure")
        return _safe_rows(record, snapshot, previous_snapshot=previous_snapshot)

    dialog = GlobalRevisionHistoryDialog(
        database, change_rows_builder=rows_with_one_failure
    )
    _select_revision(dialog, REVISION_2)
    assert REVISION_2 not in dialog._detail_cache
    assert "읽지 못했습니다" in dialog._detail_status.text()
    _select_revision(dialog, REVISION_3)
    _select_revision(dialog, REVISION_2)
    assert attempts == 2
    assert database.revision_calls.count(REVISION_2) == 2
    assert REVISION_2 in dialog._detail_cache
    assert dialog._detail_model.rows[0][0] == str(REVISION_2)
    dialog.close()


@pytest.mark.parametrize(("fails", "reverse_selection"), ((False, False), (True, True)))
def test_slow_detail_load_restores_search_focus_cursor_and_selection(
    app, fails, reverse_selection
):
    import time

    database = _Database()

    def slow_rows(record, snapshot, *, previous_snapshot=None):
        if record.revision_id == REVISION_2:
            time.sleep(1.1)
            if fails:
                raise RuntimeError("slow read failure")
        return _safe_rows(record, snapshot, previous_snapshot=previous_snapshot)

    dialog = GlobalRevisionHistoryDialog(database, change_rows_builder=slow_rows)
    dialog.show()
    dialog.activateWindow()
    dialog._search.setFocus()
    app.processEvents()
    assert QApplication.activeWindow() is dialog
    assert dialog._search.hasFocus()

    dialog._search.setText("같은 번호의 저장")
    if reverse_selection:
        dialog._search.setSelection(6, -4)
    else:
        dialog._search.setSelection(2, 4)
    expected_cursor = dialog._search.cursorPosition()
    QTest.qWait(1700)

    assert dialog.selected_revision_id == REVISION_2
    assert dialog._search.hasFocus()
    assert dialog._search.cursorPosition() == expected_cursor
    assert dialog._search.selectionStart() == 2
    assert dialog._search.selectedText() == " 번호의"
    if fails:
        assert "읽지 못했습니다" in dialog._detail_status.text()
    else:
        assert dialog._detail_model.rows[0][0] == str(REVISION_2)
    dialog.close()


def test_detail_load_does_not_take_focus_back_from_another_window(app):
    import time

    database = _Database()

    def slow_rows(record, snapshot, *, previous_snapshot=None):
        if record.revision_id == REVISION_2:
            time.sleep(1.4)
        return _safe_rows(record, snapshot, previous_snapshot=previous_snapshot)

    dialog = GlobalRevisionHistoryDialog(database, change_rows_builder=slow_rows)
    dialog.show()
    dialog.activateWindow()
    dialog._search.setFocus()
    other = QWidget()
    other_search = QLineEdit(other)
    QTimer.singleShot(1600, other.show)
    QTimer.singleShot(1610, other.activateWindow)
    QTimer.singleShot(1620, other_search.setFocus)

    dialog._search.setText("같은 번호의 저장")
    QTest.qWait(2400)

    assert dialog.selected_revision_id == REVISION_2
    assert QApplication.activeWindow() is other
    assert other_search.hasFocus()
    assert not dialog._search.hasFocus()
    dialog.close()
    other.close()


def test_close_during_detail_read_does_not_repopulate_cache(app):
    import time

    database = _Database()

    def slow_rows(record, snapshot, *, previous_snapshot=None):
        if record.revision_id == REVISION_2:
            time.sleep(1.1)
        return _safe_rows(record, snapshot, previous_snapshot=previous_snapshot)

    dialog = GlobalRevisionHistoryDialog(database, change_rows_builder=slow_rows)
    dialog.show()
    dialog._search.setText("같은 번호의 저장")
    QTimer.singleShot(600, dialog.close)
    QTest.qWait(1700)

    assert not dialog.isVisible()
    assert not dialog._detail_cache
    assert dialog._detail_cache_text_size == 0


def test_close_cancels_pending_history_search(app):
    database = _Database()
    dialog = GlobalRevisionHistoryDialog(database, change_rows_builder=_safe_rows)
    dialog.show()
    reads = list(database.revision_calls)
    dialog._search.setText("같은 번호의 저장")
    dialog.close()
    QTest.qWait(550)
    assert not dialog._search_timer.isActive()
    assert database.revision_calls == reads


def test_pending_search_waits_for_inflight_detail_read(app):
    import time

    database = _Database()

    def slow_rows(record, snapshot, *, previous_snapshot=None):
        if record.revision_id == REVISION_2:
            time.sleep(0.65)
        return _safe_rows(record, snapshot, previous_snapshot=previous_snapshot)

    dialog = GlobalRevisionHistoryDialog(database, change_rows_builder=slow_rows)
    dialog.show()
    dialog._search.setText("첫 저장")
    dialog._summary_table.selectRow(1)
    assert database.revision_calls == [REVISION_3, REVISION_2]
    assert dialog.selected_revision_id == REVISION_2
    QTest.qWait(550)
    assert dialog.selected_revision_id == REVISION_1
    assert dialog._detail_model.rows[0][0] == str(REVISION_1)
    dialog.close()


def test_long_detail_scrolls_within_a_row_at_minimum_size(app):
    database = _Database()

    def long_rows(_record, _snapshot, *, previous_snapshot=None):
        return (("김하늘", "석식", "개인정보", "\n".join(f"변경 항목 {index}" for index in range(80))),)

    dialog = GlobalRevisionHistoryDialog(database, change_rows_builder=long_rows)
    dialog.resize(1080, 680)
    dialog.show()
    app.processEvents()

    assert dialog._detail_table.verticalScrollMode() is QAbstractItemView.ScrollMode.ScrollPerPixel
    assert dialog._summary_table.verticalScrollMode() is QAbstractItemView.ScrollMode.ScrollPerPixel
    scrollbar = dialog._detail_table.verticalScrollBar()
    assert scrollbar.maximum() > 0
    scrollbar.setValue(scrollbar.maximum())
    app.processEvents()
    assert scrollbar.value() == scrollbar.maximum()
    assert "변경 항목 79" in dialog._detail_model.data(dialog._detail_model.index(0, 2))
    dialog.close()


def test_startup_wires_revision_history_signal_to_injected_dialog(app):
    roster = SimpleNamespace(persons=(object(),), revision_id=REVISION_3)

    class Database:
        def current_roster(self):
            return roster

        def snapshot(self, revision_id):
            assert revision_id == roster.revision_id
            return HistoricalSnapshot(revision_id, {})

    session = SimpleNamespace(
        database=Database(),
        credentials=SimpleNamespace(database_id=UUID(int=5)),
    )

    class Signal:
        def __init__(self):
            self.callback = None

        def connect(self, callback):
            self.callback = callback

        def emit(self):
            self.callback()

    class Window(QWidget):
        def __init__(self, displayed, *_args, **_kwargs):
            super().__init__()
            self.roster = displayed
            self.revision_history_requested = Signal()

    shown = []

    class Dialog(QDialog):
        def __init__(self, database, parent):
            super().__init__(parent)
            shown.append((database, parent, False))

        def show(self):
            super().show()
            shown[-1] = (*shown[-1][:2], True)

    class Access:
        backup_result = None

        def __init__(self):
            self.session = session

        def exec(self):
            return QDialog.DialogCode.Accepted

    controller = DesktopStartupController(
        settings=SimpleNamespace(),
        roster_window_type=Window,
        revision_history_dialog_type=Dialog,
    )
    controller._histories = lambda _session, _roster, _snapshot: {}
    controller._revision_display_code = lambda _session, _roster: "20261014-110000"

    outcome = controller._start_with_dialog(Access(), None)
    assert outcome.status is StartupStatus.READY
    outcome.window.revision_history_requested.emit()
    assert shown == [(session.database, outcome.window, True)]
    dialog = outcome.window.findChild(Dialog)
    assert dialog.isVisible() and not dialog.isModal()
    outcome.window.close()
    assert not dialog.isVisible()


def test_person_meal_history_has_current_columns_and_matching_details(app):
    from src.desktop_v2.meal_history import MEAL_HISTORY_HEADINGS
    headings = MEAL_HISTORY_HEADINGS
    rows = (("중식", "일시중지", "전화", "2026-10-15 ~ 2026-10-20",
             "병원 방문", "합성 담당자", "20261014-001"),)
    tables = {
        "정보 변경": (("성명",), ()),
        "식종 변경": (headings, rows),
        "민원": (("성명",), ()),
    }
    person = SimpleNamespace(person_id=UUID(int=41), profile=SimpleNamespace(name="합성 대상자"))
    dialog = PersonHistoryDialog(person, tables, {}, Meal.LUNCH)
    dialog.resize(1080, 680)
    dialog.show()
    app.processEvents()

    view = dialog.findChild(QTableView, "history_식종 변경")
    assert headings == ("식종", "구분", "접수 경로", "기간", "비고", "변경자", "변경번호")
    assert view.model().columnCount() == len(headings)
    assert view.wordWrap()
    assert view.textElideMode() is Qt.TextElideMode.ElideNone
    assert view.verticalHeader().sectionResizeMode(0) is QHeaderView.ResizeMode.ResizeToContents
    header = view.horizontalHeader()
    assert tuple(headings[header.logicalIndex(index)] for index in range(header.count())) == headings
    assert all(not view.isColumnHidden(index) for index in range(len(headings)))
    assert view.horizontalScrollBar().maximum() == 0
    assert tuple(view.model().data(view.model().index(0, column))
                 for column in range(len(headings))) == rows[0]
    details = dialog.findChild(QPlainTextEdit, "historyDetails_식종 변경")
    assert details.toPlainText() == chr(10).join((
        "식종: 중식", "구분: 일시중지", "접수 경로: 전화", "기간: 2026-10-15 ~ 2026-10-20",
        "비고: 병원 방문", "변경자: 합성 담당자", "변경번호: 20261014-001",
    ))
    dialog.close()


def test_default_builder_shows_roster_headers_and_household_names(app):
    from src.domain.projections import MEMBERSHIP_KIND, PERSON_KIND, STATUS_EVENT_KIND
    from src.domain.revisions import AuditChange, ChangeOperation, EntityRef, RevisionMetadata, RevisionRecord

    summary = _summary(3, REVISION_3, "20261014-110000", "급식 중지", "담당자")
    metadata = RevisionMetadata(
        REVISION_3, None, summary.recorded_at, summary.effective_on,
        summary.actor, summary.reason, summary.source,
    )
    entities = {}
    changes = []
    for number, name in enumerate(("김하늘", "김바다"), start=41):
        person_id = UUID(int=number)
        entities[EntityRef(PERSON_KIND, person_id)] = {"name": name}
        entities[EntityRef(MEMBERSHIP_KIND, person_id)] = {
            "person_id": str(person_id), "periods": [{
                "household_id": str(UUID(int=50)),
                "starts_on": "2026-01-01", "ends_on": None,
            }],
        }
        changes.append(AuditChange(
            STATUS_EVENT_KIND, UUID(int=number + 100), ChangeOperation.INSERT, None,
            {"person_id": str(person_id), "meal": "석식", "status": "중지",
             "starts_on": "2026-10-14", "ends_on": None, "source_text": None},
        ))
    record = RevisionRecord(metadata, tuple(changes))
    snapshot = HistoricalSnapshot(REVISION_3, entities)
    database = SimpleNamespace(
        revision_summaries=lambda: (summary,),
        revision=lambda _: record,
        snapshot=lambda _: snapshot,
    )
    parent = QWidget()
    parent.table_model = SimpleNamespace(columns=(
        SimpleNamespace(key="status:dinner", label="저녁"),
    ))
    dialog = GlobalRevisionHistoryDialog(database, parent)
    dialog.show()
    app.processEvents()
    model = dialog._detail_model
    assert model.rowCount() == 1
    assert model.data(model.index(0, 0)) == "김하늘\n김바다"
    assert model.data(model.index(0, 1)) == "저녁"
    assert model.data(model.index(0, 2)) == "석식 중지\n적용 기간: 2026-10-14 ~ "
    assert dialog._detail_table.rowHeight(0) > dialog.fontMetrics().height() * 2
    dialog.close()
    parent.close()


def test_named_branch_view_keeps_shared_history_and_marks_fork_boundary(app):
    from uuid import uuid4
    from src.services.revision_branches import RevisionBranch

    database = _Database()
    main_id, branch_id = uuid4(), uuid4()
    database.branches = lambda: (
        RevisionBranch(main_id, "main", REVISION_3, False),
        RevisionBranch(branch_id, "검토용", REVISION_2, True, REVISION_2, main_id),
    )
    ancestry = {REVISION_1: (REVISION_1,), REVISION_2: (REVISION_1, REVISION_2),
                REVISION_3: (REVISION_1, REVISION_2, REVISION_3)}
    database.branch_revision_ids = lambda revision_id=None: ancestry[revision_id or REVISION_2]
    dialog = GlobalRevisionHistoryDialog(database, change_rows_builder=_safe_rows)
    assert dialog._branch_selector.currentData() == branch_id
    assert "검토용" in dialog._current_branch.text()
    assert dialog._summary_proxy.rowCount() == 2
    assert dialog.selected_revision_id == REVISION_2
    assert dialog._summary_proxy.data(dialog._summary_proxy.index(0, 0)).startswith("분기 시작점 · ")
    assert dialog._summary_proxy.data(dialog._summary_proxy.index(1, 0)).startswith("분기 전 기록 · ")
    _select_revision(dialog, REVISION_1)
    assert dialog.selected_revision_id == REVISION_1
    dialog.configure_actions(SimpleNamespace(path=Path("synthetic.sqlite3")), SimpleNamespace(), lambda: None)
    assert not dialog._checkout_button.isEnabled()
    switches = []
    dialog._restore_controller = SimpleNamespace(checkout=lambda *args: switches.append(args))
    dialog._branch_selector.setCurrentIndex(1)
    assert dialog._summary_proxy.rowCount() == 3
    assert dialog._checkout_button.isEnabled()
    assert dialog.selected_revision_id == REVISION_1
    assert not switches  # Browsing alone never switches the operating state.
    dialog._checkout_selected_branch()
    assert switches[0][:2] == (main_id, "main")
    dialog._branch_selector.setCurrentIndex(0)
    assert dialog._summary_proxy.rowCount() == 3
    assert not dialog._summary_model.branch_labels
    assert not dialog._checkout_button.isEnabled()
    dialog.close()


def test_branch_list_failure_is_observable_and_keeps_audit_available(app):
    database = _Database()
    database.branch_revision_ids = lambda revision_id=None: ()
    def fail():
        raise OSError("private stored data")
    database.branches = fail
    dialog = GlobalRevisionHistoryDialog(database, change_rows_builder=_safe_rows)
    assert "읽지 못했습니다" in dialog._current_branch.text()
    assert not dialog._current_branch.isHidden()
    assert "private stored data" not in dialog._current_branch.text()
    assert dialog._summary_proxy.rowCount() == 3
    dialog.close()


@pytest.mark.parametrize("operation", ["create", "checkout"])
def test_audit_branch_events_show_named_korean_details_without_synthetic_entity(operation, app):
    from src.services.revision_branches import BRANCH_EVENT_KEY, RevisionBranch
    from src.services.revision_restore import RESTORE_TARGET_KEY

    database = _Database()
    branch_id = UUID(int=123)
    database.branches = lambda: (RevisionBranch(branch_id, "검토 분기", REVISION_2, True, REVISION_1),)
    database.branch_revision_ids = lambda revision_id=None: (REVISION_1, REVISION_2)
    database.revision_display_code = lambda revision_id: "20261014-100000"
    original = database.revision
    def revision(revision_id):
        record = original(revision_id)
        record.metadata.decisions = {} if revision_id != REVISION_3 else {
            BRANCH_EVENT_KEY: {"operation": operation, "branch_id": str(branch_id), "name": "검토 분기"},
            RESTORE_TARGET_KEY: str(REVISION_1),
        }
        return record
    database.revision = revision
    dialog = GlobalRevisionHistoryDialog(database, change_rows_builder=_safe_rows)
    dialog._branch_selector.setCurrentIndex(0)
    _select_revision(dialog, REVISION_3)
    rows = dialog._detail_model.rows
    assert len(rows) == 1
    assert rows[0][0] == "운영 데이터"
    assert rows[0][2] == ("분기 생성" if operation == "create" else "분기 전환")
    assert "검토 분기" in rows[0][3] and "20261014-100000" in rows[0][3]
    assert str(branch_id) not in str(rows)
    dialog.close()


def test_encrypted_branch_history_displays_shared_revisions_once(app, history):
    from tests.test_revision_restore import ACTOR
    from tests.test_revision_branches import append_setting
    from src.services.revision_branches import MAIN_BRANCH_ID, create_branch, checkout_branch

    session, first, second, _ = history
    branch = create_branch(session, second, second, ACTOR, name="실제 DB 검토")
    edited = append_setting(session, "검토 값")
    main = checkout_branch(session, MAIN_BRANCH_ID, edited, ACTOR)
    dialog = GlobalRevisionHistoryDialog(session.database)
    assert dialog._branch_selector.currentData() == MAIN_BRANCH_ID
    assert dialog._summary_proxy.rowCount() == 2
    dialog._branch_selector.setCurrentIndex(2)
    visible = tuple(dialog._summary_proxy.data(dialog._summary_proxy.index(row, 0), Qt.ItemDataRole.UserRole)
                    for row in range(dialog._summary_proxy.rowCount()))
    assert visible == (edited, second, first)
    assert branch.revision_id not in visible and main.revision_id not in visible
    assert dialog._summary_model.branch_labels[first] == "분기 전 기록"
    assert dialog._summary_model.branch_labels[second] == "분기 시작점"
    _select_revision(dialog, first)
    assert dialog._detail_model.rowCount() > 0
    dialog._branch_selector.setCurrentIndex(0)
    _select_revision(dialog, main.revision_id)
    assert dialog._detail_model.rows[0][2] == "분기 전환"
    assert "main" in dialog._detail_model.rows[0][3]
    dialog.close()
