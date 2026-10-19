"""Read-only global revision history browser contracts."""

from __future__ import annotations

import os
from datetime import UTC, date, datetime
from types import SimpleNamespace
from uuid import UUID

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QHeaderView,
    QPlainTextEdit,
    QTableView,
)

from src.desktop_v2.application import DesktopStartupController, StartupStatus
from src.desktop_v2.person_history import PersonHistoryDialog
from src.desktop_v2.revision_history import GlobalRevisionHistoryDialog
from src.domain.revisions import HistoricalSnapshot, RevisionActor, RevisionSource, RevisionSummary
from src.domain.roster import Meal

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

    def revision_summaries(self):
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
    return ((str(snapshot.revision_id), "중식", "개인정보", f"부모: {parent}"),)


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

    dialog._search.setText("김담당")
    app.processEvents()
    assert dialog._summary_proxy.rowCount() == 1
    assert dialog.selected_revision_id == REVISION_3

    dialog._search.setText("같은 번호의 저장")
    app.processEvents()
    assert dialog._summary_proxy.rowCount() == 1
    assert dialog.selected_revision_id == REVISION_2

    dialog._search.setText("20261014-100000")
    app.processEvents()
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
    app.processEvents()
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
    app.processEvents()
    assert dialog.selected_revision_id == REVISION_2
    assert dialog._detail_model.rowCount() == 0
    assert "읽지 못했습니다" in dialog._detail_status.text()
    assert "secret-payload" not in dialog._detail_status.text()
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

    class Window:
        def __init__(self, displayed, *_args, **_kwargs):
            self.roster = displayed
            self.revision_history_requested = Signal()

    shown = []

    class Dialog:
        def __init__(self, database, parent):
            shown.append((database, parent, False))

        def setAttribute(self, attribute):
            assert attribute is Qt.WidgetAttribute.WA_DeleteOnClose

        def show(self):
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
    controller._histories = lambda _session, _roster: {}
    controller._revision_display_code = lambda _session, _roster: "20261014-110000"

    outcome = controller._start_with_dialog(Access(), None)
    assert outcome.status is StartupStatus.READY
    outcome.window.revision_history_requested.emit()
    assert shown == [(session.database, outcome.window, True)]


def test_person_meal_history_prioritizes_wrapped_grouped_status_values(app):
    meal_headings = (
        "기록 시각(KST)",
        "적용일",
        "변경번호",
        "사람 UUID",
        "성명",
        "식종",
        "구분",
        "이전 값",
        "변경 값",
        "접수 채널",
        "사유",
        "변경자",
    )
    rows = ((
        datetime(2026, 10, 14, 12, 30, tzinfo=UTC),
        date(2026, 10, 15),
        "20261014-213000",
        str(UUID(int=41)),
        "합성 대상자",
        "중식",
        "상태 변경",
        "상태: 배송 · 기간: 2026-10-01부터 · 종료일 미지정 · 사유: 이전 사유",
        "상태: 일시중지 · 기간: 2026-10-15 ~ 2026-10-20 · 사유: 가족 일정",
        "전화",
        "상태 변경 접수",
        "합성 담당자",
    ),)
    tables = {
        "정보 변경": (("성명",), ()),
        "식종 변경": (meal_headings, rows),
        "민원": (("성명",), ()),
    }
    person = SimpleNamespace(profile=SimpleNamespace(name="합성 대상자"))
    dialog = PersonHistoryDialog(person, tables, {}, Meal.LUNCH)
    dialog.show()
    app.processEvents()

    view = dialog.findChild(QTableView, "history_식종 변경")
    assert view is not None
    assert view.wordWrap()
    assert view.textElideMode() is Qt.TextElideMode.ElideNone
    assert (
        view.verticalHeader().sectionResizeMode(0)
        is QHeaderView.ResizeMode.ResizeToContents
    )
    assert view.isColumnHidden(meal_headings.index("성명"))
    assert not view.isColumnHidden(meal_headings.index("이전 값"))
    header = view.horizontalHeader()
    assert header.visualIndex(meal_headings.index("변경 값")) < header.visualIndex(
        meal_headings.index("사유")
    )
    assert header.visualIndex(meal_headings.index("사유")) < header.visualIndex(
        meal_headings.index("기록 시각(KST)")
    )
    assert header.visualIndex(meal_headings.index("기록 시각(KST)")) < header.visualIndex(
        meal_headings.index("이전 값")
    )
    changed = view.model().data(
        view.model().index(0, meal_headings.index("변경 값"))
    )
    assert changed == (
        "상태: 일시중지\n기간: 2026-10-15 ~ 2026-10-20\n사유: 가족 일정"
    )
    assert view.rowHeight(0) > view.fontMetrics().height() * 2
    details = dialog.findChild(QPlainTextEdit, "historyDetails_식종 변경")
    detail_text = details.toPlainText()
    assert detail_text.index("변경 값:") < detail_text.index("사유:")
    assert detail_text.index("사유:") < detail_text.index("기록 시각(KST):")
    dialog.close()

    blank_previous = list(rows[0])
    blank_previous[meal_headings.index("이전 값")] = ""
    blank_tables = dict(tables)
    blank_tables["식종 변경"] = (meal_headings, (tuple(blank_previous),))
    blank_dialog = PersonHistoryDialog(person, blank_tables, {}, Meal.LUNCH)
    blank_view = blank_dialog.findChild(QTableView, "history_식종 변경")
    assert blank_view.isColumnHidden(meal_headings.index("이전 값"))
    blank_dialog.close()
