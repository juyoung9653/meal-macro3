"""Current-sheet exports preserve the visible view and protected publication."""
from dataclasses import replace
from io import BytesIO
from types import SimpleNamespace

import msoffcrypto
import openpyxl
import pytest
from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QApplication, QMessageBox

from src.desktop_v2.current_sheet_export import (
    CurrentSheetExportController, CurrentSheetSnapshot, build_current_sheet_workbook,
)
from src.desktop_v2.roster_window import RosterWindow
from tests.test_desktop_v2_roster import SERVICE_DAY, _roster


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def notices():
    errors, successes = [], []
    return SimpleNamespace(
        critical=lambda *args: errors.append(args),
        information=lambda *args: successes.append(args),
        question=lambda *args: QMessageBox.StandardButton.No,
        errors=errors, successes=successes,
    )


def sample():
    return CurrentSheetSnapshot("배송 연락", ("성명", "순서", "비고"), (
        ("샘플 대상자", "001", "첫째 줄\n둘째 줄"),
        ("=1+1", "010", "=SUM(A1:A2)"),
    ))


def test_capture_active_sheet_visible_order_aliases_filters_and_freezes(app, tmp_path):
    roster = _roster()
    persons = tuple(replace(person, profile=replace(person.profile,
        note="메모\n여러 줄")) for person in roster.persons)
    window = RosterWindow(replace(roster, persons=persons),
        column_settings=QSettings(str(tmp_path / "view.ini"), QSettings.Format.IniFormat))
    try:
        window._set_day(SERVICE_DAY)
        model = window.table_model
        model.set_archive_filter(frozenset(), False)
        window._add_sheet("연락처", {"name", "sequence", "note"})
        columns = {column.key: index for index, column in enumerate(model.columns)}
        model.set_column_alias("name", "수령인")
        header = window._table.horizontalHeader()
        header.moveSection(header.visualIndex(columns["note"]), 0)
        model.sort(columns["name"], Qt.SortOrder.DescendingOrder)
        snapshot = window.current_sheet_snapshot()
        assert snapshot.sheet_name == "연락처"
        assert snapshot.headers == ("비고", "수령인", "순서")
        assert [row[1] for row in snapshot.rows] == ["홍길동", "김아침"]
        assert snapshot.rows[0][0] == "메모 여러 줄"
        assert "001" in snapshot.rows[0][2]
        model.set_search("김아침")
        filtered = window.current_sheet_snapshot()
        assert [row[1] for row in filtered.rows] == ["김아침"]
        assert len(snapshot.rows) == 2
        captured = []
        window.current_sheet_export_requested.connect(captured.append)
        window._current_sheet_export_action.trigger()
        assert captured == [filtered]
        model.set_search("존재하지 않는 이름")
        assert window.current_sheet_snapshot().rows == ()
        for index in range(model.columnCount()):
            window._table.setColumnHidden(index, True)
        with pytest.raises(ValueError, match="칼럼"):
            window.current_sheet_snapshot()
    finally:
        window.close()


def test_workbook_single_sheet_literal_text_leading_zeroes_and_multiline():
    snapshot = sample()
    workbook = build_current_sheet_workbook(snapshot)
    data = BytesIO()
    workbook.save(data)
    workbook.close()
    data.seek(0)
    loaded = openpyxl.load_workbook(data)
    try:
        assert loaded.sheetnames == [snapshot.sheet_name]
        sheet = loaded.active
        assert tuple(sheet.values) == (snapshot.headers, *snapshot.rows)
        assert sheet["B2"].value == "001"
        assert sheet["A3"].data_type == "s"
        assert sheet["C3"].data_type == "s"
        assert sheet["C2"].alignment.wrap_text
    finally:
        loaded.close()


def test_empty_rows_export_headers_and_invalid_excel_name_is_safe():
    workbook = build_current_sheet_workbook(CurrentSheetSnapshot("긴/시트:*?[]" * 6, ("성명",), ()))
    try:
        assert len(workbook.sheetnames) == 1
        assert len(workbook.active.title) <= 31
        assert not set("[]:*?/\\") & set(workbook.active.title)
        assert tuple(workbook.active.values) == (("성명",),)
    finally:
        workbook.close()


def test_real_encrypted_export_roundtrip(app, tmp_path):
    target = tmp_path / "current.xlsx"
    messages = notices()
    result = CurrentSheetExportController(message_box=messages,
        file_dialog=SimpleNamespace(getSaveFileName=lambda *a, **k: (str(target), "")),
    ).run(sample(), "test-export-password", directory=tmp_path)
    assert result is not None and result.password_protected
    assert not messages.errors and len(messages.successes) == 1
    with target.open("rb") as source:
        office = msoffcrypto.OfficeFile(source)
        assert office.is_encrypted()
        office.load_key(password="test-export-password")
        plaintext = BytesIO()
        office.decrypt(plaintext)
    workbook = openpyxl.load_workbook(plaintext)
    try:
        assert workbook.sheetnames == [sample().sheet_name]
        assert tuple(workbook.active.values) == (sample().headers, *sample().rows)
    finally:
        workbook.close()


@pytest.mark.parametrize("password", [None, "", " "])
def test_missing_password_cannot_publish_plaintext(app, tmp_path, password):
    target = tmp_path / "must-not-exist.xlsx"
    messages = notices()
    result = CurrentSheetExportController(message_box=messages,
        file_dialog=SimpleNamespace(getSaveFileName=lambda *a, **k: (str(target), "")),
    ).run(sample(), password, directory=tmp_path)
    assert result is None and messages.errors and not messages.successes
    assert not target.exists()


@pytest.mark.parametrize("mode", ["file_cancel", "overwrite_cancel", "write_failure"])
def test_cancel_and_failure_preserve_existing_file(app, tmp_path, mode):
    target = tmp_path / "existing.xlsx"
    target.write_bytes(b"original file")
    messages = notices()
    if mode == "write_failure":
        messages.question = lambda *args: QMessageBox.StandardButton.Yes

    class BrokenWorkbook:
        def save(self, destination):
            raise OSError("storage failure")
        def close(self):
            pass

    result = CurrentSheetExportController(message_box=messages,
        file_dialog=SimpleNamespace(getSaveFileName=lambda *a, **k:
            ("" if mode == "file_cancel" else str(target), "")),
        workbook_builder=lambda *a, **k: BrokenWorkbook(),
    ).run(sample(), "test-export-password", directory=tmp_path)
    assert result is None and not messages.successes
    assert bool(messages.errors) is (mode == "write_failure")
    assert target.read_bytes() == b"original file"
    assert not list(tmp_path.glob(".existing.xlsx.*"))

@pytest.mark.parametrize("fail", [False, True])
def test_progress_stays_open_and_responsive_until_worker_finishes(app, tmp_path, fail):
    from threading import Event, get_ident
    from PySide6.QtCore import QTimer
    from src.desktop_v2.current_sheet_export import CurrentSheetExportProgressDialog

    started, released = Event(), Event()
    ui_thread = get_ident()
    observed = []
    messages = notices()
    target = tmp_path / "responsive.xlsx"

    def builder(snapshot, **kwargs):
        assert get_ident() != ui_thread
        started.set()
        assert released.wait(5), "UI event loop did not continue"
        if fail:
            raise OSError("worker failure")
        return build_current_sheet_workbook(snapshot, **kwargs)

    def heartbeat():
        if not started.is_set() or released.is_set():
            return
        dialog = next((widget for widget in app.topLevelWidgets()
            if isinstance(widget, CurrentSheetExportProgressDialog) and widget.isVisible()), None)
        if dialog is None:
            return
        dialog.reject()
        dialog.close()
        observed.append((get_ident(), dialog.isVisible()))
        released.set()

    timer = QTimer()
    timer.timeout.connect(heartbeat)
    timer.start(10)
    try:
        result = CurrentSheetExportController(message_box=messages,
            workbook_builder=builder,
            file_dialog=SimpleNamespace(getSaveFileName=lambda *a, **k: (str(target), "")),
        ).run(sample(), "test-export-password", directory=tmp_path)
    finally:
        timer.stop()
        released.set()
    assert observed == [(ui_thread, True)]
    assert (result is None) is fail
    assert bool(messages.errors) is fail
    assert target.exists() is not fail
