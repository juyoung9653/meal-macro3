"""Synthetic Excel template edits, UI persistence and generation boundaries."""
from copy import copy
from dataclasses import replace
from datetime import date
from pathlib import Path

import openpyxl
import pytest
from openpyxl.styles import Border, Font, PatternFill, Side
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from src.desktop_v2.delivery_template import DeliveryTemplateDialog, DeliveryTemplateStore
from src.desktop_v2.document_hub import DocumentAction
from src.excel.delivery_templates import create_substitute_template, validate_delivery_template
from src.generators import substitute_generator, report_generator
from src.services.delivery_service import DeliveryReport, DeliveryReportRow, DeliveryReportTotals
from src.services.sqlite_delivery_substitute_service import prepare_delivery_substitute_data
from src.domain.roster import Meal
from tests.test_delivery_substitute_v2 import source, options, DAY


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_real_template_column_styles_reach_measurement_and_serialized_output(tmp_path, monkeypatch):
    path = tmp_path / "template.xlsx"
    create_substitute_template(path)
    wb = openpyxl.load_workbook(path)
    ws = wb["배송대타"]
    ws["C3"].font = Font(name="Arial", size=17, bold=True, italic=True, color="123456")
    ws["C3"].border = Border(bottom=Side(style="double", color="654321"))
    ws["C6"].font = copy(ws["C3"].font)
    ws["C6"].border = copy(ws["C3"].border)
    ws["C6"].fill = PatternFill("solid", fgColor="135790")
    ws["A18"].border = Border(left=Side(style="double", color="246810"), top=Side(style="thick", color="246810"))
    wb.save(path)
    wb.close()
    snapshot, _ = source(sizes=(3, 1, 1))
    data = prepare_delivery_substitute_data(snapshot, DAY, Meal.BREAKFAST)
    data = replace(data, rows=tuple(
        replace(row, entitlement_count=count)
        for row, count in zip(data.rows, (3, 1, 2, 3, 1), strict=True)
    ))
    selected = replace(options(data), columns=("name", "road_address"))
    observed = []
    def measure(workbook, columns, layout, selected, *, preserve_styles=False):
        observed.append(preserve_styles)
        assert workbook.active["A3"].font.name == "Arial"
        assert workbook.active["A3"].font.sz == 17
    monkeypatch.setattr(substitute_generator, "apply_excel_metrics", measure)
    result = substitute_generator.build_substitute_workbook(data, selected, template_path=path)
    output = tmp_path / "generated.xlsx"
    result.save(output)
    result.close()
    actual = openpyxl.load_workbook(output).active
    assert observed == [True]
    assert actual["A3"].font.color.rgb == "00123456"
    assert actual["A3"].fill.fgColor.rgb == "00135790"
    assert actual["C3"].value == 3
    assert actual["A3"].border.left.style == "double"
    assert actual["A6"].border.bottom.style == "double"
    assert actual["B3"].font.name != "Arial"


def test_default_asset_is_consumed_and_preserves_group_fonts(tmp_path, monkeypatch):
    from src.core.settings import get_settings
    path = tmp_path / "default.xlsx"
    create_substitute_template(path)
    monkeypatch.setattr(get_settings(), "get_substitute_template_path", lambda profile="급식": str(path))
    snapshot, _ = source()
    data = prepare_delivery_substitute_data(snapshot, DAY, Meal.BREAKFAST)
    flags = []
    monkeypatch.setattr(substitute_generator, "apply_excel_metrics", lambda *args, **kwargs: flags.append(kwargs.get("preserve_styles", False)))
    substitute_generator.build_substitute_workbook(data, options(data)).close()
    wb = openpyxl.load_workbook(path)
    wb["배송대타"]["C3"].font = Font(name="Arial", size=18, color="123456")
    wb.save(path)
    wb.close()
    result = substitute_generator.build_substitute_workbook(data, options(data))
    assert flags == [True, True]
    assert result.active["C3"].font.sz == 8
    assert result.active["C5"].font.sz == 18
    result.close()


def test_report_multiline_memo_repeated_rows_and_summary_keep_styles(tmp_path):
    path = tmp_path / "report.xlsx"
    wb = openpyxl.load_workbook("assets/급식/일일식수템플릿.xlsx")
    ws = wb["aaa"]
    for row in range(4, 9):
        ws.cell(row, 8).font = Font(name="Arial", size=16, italic=True, color="123456")
        ws.cell(row, 8).border = Border(bottom=Side(style="double", color="654321"))
        ws.cell(row, 8).fill = PatternFill("solid", fgColor="ABCDEF")
    ws["G9"].font = Font(name="Arial", size=20, color="987654")
    wb.save(path)
    wb.close()
    rows = tuple(DeliveryReportRow(f"합성기사{i}", ("1", "0", "0", "0", "0"), 1, "첫 줄\n다음 줄", "") for i in range(7))
    report = DeliveryReport(date(2026, 8, 28), "석식", rows, DeliveryReportTotals((7, 0, 0, 0, 0), 7))
    output = tmp_path / "out.xlsx"
    report_generator.make_report_publication((report,), output, path, preserve_template_styles=True)
    wb = openpyxl.load_workbook(output)
    try:
        ws = wb.active
        for row in range(4, 11):
            cell = ws.cell(row, 8)
            assert cell.value == "첫 줄\n다음 줄"
            assert cell.font.name == "Arial" and cell.font.sz == 16
            assert cell.font.color.rgb == "00123456"
            assert cell.fill.fgColor.rgb == "00ABCDEF"
            assert cell.border.bottom.style == "double"
        assert ws["G11"].font.sz == 20
    finally:
        wb.close()


def test_missing_or_corrupt_template_never_changes_output(tmp_path):
    path = tmp_path / "bad.xlsx"
    path.write_text("broken", encoding="utf-8")
    output = tmp_path / "existing.xlsx"
    output.write_bytes(b"keep me")
    snapshot, _ = source()
    data = prepare_delivery_substitute_data(snapshot, DAY, Meal.BREAKFAST)
    for template in (path, tmp_path / "missing.xlsx"):
        with pytest.raises(ValueError):
            substitute_generator.make_substitute_publication(data, options(data), output, template_path=template)
        assert output.read_bytes() == b"keep me"


def test_dialog_copy_open_cancel_apply_and_database_scope(tmp_path, app):
    settings = QSettings(str(tmp_path / "preferences.ini"), QSettings.Format.IniFormat)
    store = DeliveryTemplateStore("database-a", settings=settings, root=tmp_path / "forms")
    opened = []
    action = DocumentAction.DELIVERY_SUBSTITUTE
    dialog = DeliveryTemplateDialog(action, "database-a", store=store, opener=lambda url: opened.append(url) or True)
    dialog._open()
    assert dialog.path.is_file() and opened
    validate_delivery_template(dialog.path, action.value)
    dialog.reject()
    assert store.selected(action) is None
    dialog.accept()
    selected = store.selected(action)
    assert selected == dialog.path
    assert DeliveryTemplateStore("database-b", settings=settings).selected(action) is None
    reopened = DeliveryTemplateDialog(action, "database-a", store=store)
    reopened._reset()
    reopened.reject()
    assert store.selected(action) == selected
    reopened.accept()
    assert store.selected(action) is None
    assert selected.is_file()


def test_report_managed_copy_is_data_free_and_leaves_bundle_unchanged(tmp_path, app):
    source = Path("assets/급식/일일식수템플릿.xlsx")
    before = source.read_bytes()
    store = DeliveryTemplateStore("synthetic", settings=QSettings(str(tmp_path / "prefs.ini"), QSettings.Format.IniFormat), root=tmp_path / "forms")
    target = store.create(DocumentAction.DELIVERY_REPORT)
    assert target != source and source.read_bytes() == before
    wb = openpyxl.load_workbook(target)
    try:
        assert all(cell.value is None for row in wb["aaa"].iter_rows(min_row=4, max_row=8, max_col=9) for cell in row)
    finally:
        wb.close()



def test_generation_freezes_template_bytes_before_external_save(tmp_path, app, monkeypatch):
    from hashlib import sha256
    from src.core.settings import get_settings
    from src.desktop_v2.document_generation import DocumentGenerationController
    from src.excel.publication import publish_workbook
    from src.storage.database_session import DatabaseSession
    from tests.test_delivery_substitute_v2 import initial_record, selection_for, Messages
    path = tmp_path / "default.xlsx"
    create_substitute_template(path)
    original = path.read_bytes()
    monkeypatch.setattr(get_settings(), "get_substitute_template_path", lambda profile="급식": str(path))
    backups = tmp_path / "backups"
    backups.mkdir()
    temporary_paths = []
    def writer(data, options, output, *, template_path, **kwargs):
        temporary_paths.append(template_path)
        assert template_path != path
        assert template_path.read_bytes() == original
        path.write_bytes(b"an external editor saved different bytes")
        wb = openpyxl.Workbook()
        wb.active["A1"] = "synthetic output"
        return publish_workbook(wb, output, password=kwargs["output_password"], overwrite=False)
    with DatabaseSession.create(tmp_path / "source.sqlite3", "database-password", backup_directory=backups) as session:
        snapshot, _ = source()
        session.append_revision(initial_record(snapshot))
        messages = Messages()
        outcome = DocumentGenerationController(session, message_box=messages, substitute_writer=writer).run(selection_for(session, tmp_path))
        assert outcome.completed, messages.errors
        manifest = session.database.document_runs()[0].template_manifest
        assert manifest["template_sha256"] == sha256(original).hexdigest()
        assert manifest["template_byte_size"] == len(original)
        assert manifest["template_filename"] == path.name
    assert temporary_paths and not temporary_paths[0].exists()


def test_native_measurement_accepts_cleared_template_border_sides(monkeypatch):
    from unittest.mock import MagicMock
    from src.excel import substitute_excel_metrics as metrics
    snapshot, _ = source()
    data = prepare_delivery_substitute_data(snapshot, DAY, Meal.BREAKFAST)
    selected = replace(options(data), columns=("name",))
    wb, columns, layout = substitute_generator._build_sheet(data, selected)
    wb.active["A3"].border = Border(bottom=Side(style="double", color="654321"))
    excel_sheet = MagicMock()
    class LayoutReached(Exception):
        pass
    def layout_reached(*args, **kwargs):
        assert kwargs["preserve_styles"] is True
        raise LayoutReached
    monkeypatch.setattr(metrics, "apply_page_formatting", layout_reached)
    try:
        with pytest.raises(LayoutReached):
            metrics._measure(wb, excel_sheet, columns, layout, selected, preserve_styles=True)
        # Cleared sides are sent to Excel as xlLineStyleNone, not dereferenced.
        assert excel_sheet.Cells.return_value.Borders.return_value.LineStyle in (-4142, 1, -4119)
    finally:
        wb.close()
