"""Changed-cell fills and user-selected row layouts survive real XLSX output."""
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Border, Font, PatternFill, Side
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QDialog

from src.desktop_v2.delivery_template import DeliveryTemplateDialog, DeliveryTemplateStore
from src.desktop_v2.document_hub import DocumentAction
from src.desktop_v2.report_template_layout import ReportTemplateLayoutDialog
from src.excel.report_template_layout import ReportTemplateLayout
from src.generators.report_generator import make_report_publication
from src.services.delivery_service import DeliveryReport, DeliveryReportRow, DeliveryReportTotals
from tests.test_delivery_template_profiles import app, preferences
from tests.test_delivery_substitute_v2 import session, selection_for, Messages


def template(path):
    book = Workbook()
    sheet = book.active
    sheet.title = "aaa"
    sheet.merge_cells("A3:I3")
    sheet["A3"] = "양식의 안내 제목"
    sheet["A6"] = "배송기사"
    for row in range(7, 12):
        sheet.row_dimensions[row].height = row * 5
        for column in range(1, 10):
            cell = sheet.cell(row, column)
            cell.fill = PatternFill("solid", fgColor=f"AA00{row:02X}")
            cell.font = Font(name="Arial", size=row + 3, italic=True)
            cell.border = Border(bottom=Side(style="double"))
    sheet["A9"] = "본문과 합계 사이 문구"
    sheet["A10"] = "전체 합계"
    sheet.merge_cells("A12:I12")
    sheet["A12"] = "합계 뒤 안내문"
    sheet["A13"] = "=G10"
    sheet.page_setup.scale = 85
    book.save(path)
    book.close()
    return path


@pytest.mark.parametrize("count", (0, 1, 2, 5))
@pytest.mark.parametrize("copy_row", (7, 8))
def test_custom_body_rows_and_footer_survive_expansion_or_shrink(tmp_path, count, copy_row):
    path = template(tmp_path / "source.xlsx")
    original = path.read_bytes()
    layout = ReportTemplateLayout(7, 8, copy_row, 10)
    rows = tuple(DeliveryReportRow(f"기사 {i}", ("3(+1)", "0(-1)", "2", "", ""), 7, "비고", "지역")
                 for i in range(count))
    report = DeliveryReport(date(2026, 11, 10), "석식", rows,
                            DeliveryReportTotals((3*count, 0, 2*count, 0, 0), 7*count))
    target = tmp_path / "output.xlsx"
    make_report_publication((report,), target, path, report_layout=layout, preserve_template_styles=True)
    book = load_workbook(target)
    try:
        sheet = book.active
        assert sheet["A3"].value == "양식의 안내 제목"
        for row in range(7, 7 + count):
            expected = ((7, 7, 7, 7, 8) if copy_row == 7 else (7, 8, 8, 8, 8)) if count == 5 else (7, 8)
            prototype = expected[row - 7]
            assert sheet.row_dimensions[row].height == prototype * 5
            assert sheet.cell(row, 2).value == 3
            assert sheet.cell(row, 3).value == 0
            for column in (1, 2, 3):
                assert sheet.cell(row, column).fill.fgColor.rgb == "FFC5D9F1"
            assert sheet.cell(row, 4).fill.fgColor.rgb == f"00AA00{prototype:02X}"
            assert sheet.cell(row, 2).font.sz == prototype + 3
            assert sheet.cell(row, 2).border.bottom.style == "double"
        delta = count - 2
        assert sheet.cell(9 + delta, 1).value == "본문과 합계 사이 문구"
        assert sheet.cell(10 + delta, 1).value == "전체 합계"
        assert sheet.cell(10 + delta, 7).value == 7 * count
        assert sheet.row_dimensions[10 + delta].height == 50
        assert sheet.cell(12 + delta, 1).value == "합계 뒤 안내문"
        assert f"A{12+delta}:I{12+delta}" in {str(area) for area in sheet.merged_cells.ranges}
        assert sheet.cell(13 + delta, 1).value == f"=G{10+delta}"
        assert sheet.page_setup.scale == 85
        assert sheet.print_area.endswith(f"$I${13+delta}")
    finally:
        book.close()
    assert path.read_bytes() == original


def test_inserted_copies_push_original_bottom_border_and_formulas_down(tmp_path):
    path = tmp_path / "border-template.xlsx"
    book = Workbook()
    sheet = book.active
    sheet.title = "aaa"
    for row in range(4, 10):
        sheet.row_dimensions[row].height = 20 + row
        for column in range(1, 10):
            sheet.cell(row, column).border = Border(bottom=Side(style="thick" if row == 8 else "thin"))
    sheet["H7"].fill = PatternFill("solid", fgColor="FFFFCC")
    sheet["J7"] = "=G6+G8+$G$9"
    sheet["A9"] = "합계"
    sheet["A10"] = "=G8+G9"
    book.save(path)
    book.close()
    original = path.read_bytes()
    rows = tuple(DeliveryReportRow(f"기사 {index}", ("1", "", "", "", ""), 1, "메모", "")
                 for index in range(7))
    report = DeliveryReport(date(2026, 11, 10), "석식", rows, DeliveryReportTotals((7, 0, 0, 0, 0), 7))
    target = tmp_path / "border-output.xlsx"
    make_report_publication((report,), target, path, report_layout=ReportTemplateLayout())
    book = load_workbook(target)
    try:
        sheet = book.active
        assert [sheet.row_dimensions[row].height for row in range(4, 12)] == [24, 25, 26, 26, 26, 27, 28, 29]
        for column in range(1, 10):
            assert [sheet.cell(row, column).border.bottom.style for row in range(4, 11)] == ["thin"] * 6 + ["thick"]
        assert sheet["H9"].fill.fgColor.rgb.endswith("FFFFCC")
        assert sheet["H7"].fill.fill_type is None
        assert sheet["J9"].value == "=G6+G10+$G$11"
        assert sheet["A11"].value == "합계"
        assert sheet["G11"].value == 7
        assert sheet["A12"].value == "=G10+G11"
    finally:
        book.close()
    assert path.read_bytes() == original


def test_row_dialog_uses_actual_rows_and_rejects_invalid_selection(app, tmp_path):
    path = template(tmp_path / "source.xlsx")
    dialog = ReportTemplateLayoutDialog(path)
    assert dialog.rows["copy_row"].value() == 6
    assert dialog.table.item(2, 0).text() == "양식의 안내 제목"
    for key, value in ReportTemplateLayout(7, 8, 8, 10).manifest().items():
        dialog.table.selectRow(value - 1)
        dialog._use_row(dialog.rows[key])
    dialog.rows["total_row"].setValue(7)
    dialog.accept()
    assert dialog.result() != QDialog.DialogCode.Accepted
    assert "합계" in dialog.error.text()
    dialog.rows["total_row"].setValue(10)
    dialog.accept()
    assert dialog.selection() == ReportTemplateLayout(7, 8, 8, 10)
    assert dialog.result() == QDialog.DialogCode.Accepted
    dialog.deleteLater()


def test_layout_persists_per_template_cancel_keeps_saved_choice(app, tmp_path):
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    store = DeliveryTemplateStore("test", settings=settings)
    path = template(tmp_path / "source.xlsx")
    other = template(tmp_path / "other.xlsx")
    selected = ReportTemplateLayout(7, 8, 8, 10)
    store.save(DocumentAction.DELIVERY_REPORT, path, report_layout=selected)
    assert DeliveryTemplateStore("test", settings=settings).report_layout(path) == selected
    assert store.report_layout(other).copy_row == 6
    assert DeliveryTemplateStore("other-db", settings=settings).report_layout(path).copy_row == 6
    dialog = DeliveryTemplateDialog(DocumentAction.DELIVERY_REPORT, "test", store=store)
    assert "복사 기준 8행" in dialog.report_rows_label.text()
    dialog._edit_report_rows()
    child = dialog._report_rows_dialog
    child.rows["copy_row"].setValue(7)
    child.accept()
    assert "복사 기준 7행" in dialog.report_rows_label.text()
    dialog.reject()
    assert store.report_layout(path) == selected
    dialog.accept()
    assert store.report_layout(path).copy_row == 7
    dialog.deleteLater()


def test_invalid_or_stale_layout_cannot_overwrite_existing_output(tmp_path):
    path = template(tmp_path / "source.xlsx")
    target = tmp_path / "output.xlsx"
    target.write_bytes(b"existing")
    report = DeliveryReport(date(2026, 11, 10), "석식", (), DeliveryReportTotals((0,)*5, 0))
    for layout in (ReportTemplateLayout(3, 8, 6, 10), ReportTemplateLayout(7, 8, 8, 20)):
        with pytest.raises(ValueError):
            make_report_publication((report,), target, path, report_layout=layout)
        assert target.read_bytes() == b"existing"


def test_saved_layout_reaches_generated_file_and_audit(app, session, preferences, tmp_path):
    from src.desktop_v2.document_generation import DocumentGenerationController
    from src.desktop_v2.document_output import next_available_document_output
    path = template(tmp_path / "chosen.xlsx")
    layout = ReportTemplateLayout(7, 8, 8, 10)
    store = preferences(str(session.credentials.database_id))
    store.save(DocumentAction.DELIVERY_REPORT, path, report_layout=layout)
    selected = selection_for(session, tmp_path)
    document = replace(selected.document, period=replace(selected.document.period,
                       action=DocumentAction.DELIVERY_REPORT), substitute_options=None)
    selected = next_available_document_output(document, tmp_path, selected.mode)
    messages = Messages()
    outcome = DocumentGenerationController(session, message_box=messages).run(selected)
    assert outcome.completed, messages.errors
    manifest = session.database.document_runs()[0].template_manifest
    assert manifest["daily_report_rows"] == layout.manifest()
    assert manifest["daily_report_change_fill"] == "C5D9F1"
    book = load_workbook(selected.output_path)
    try:
        assert book.active["A3"].value == "양식의 안내 제목"
        assert book.active["A7"].value is not None
    finally:
        book.close()
