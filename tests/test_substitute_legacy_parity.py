"""Legacy data/style contracts and readable native typography on synthetic rosters."""
from __future__ import annotations

from dataclasses import replace
from datetime import date
from io import BytesIO
from pathlib import Path
import sys
import re
import types
from uuid import uuid4
from xml.etree import ElementTree
from zipfile import ZipFile

import openpyxl
import pandas as pd
import pytest

from src.domain.delivery_substitute import PreparedSubstituteData, SubstituteColumn, SubstituteOptions, SubstituteRow
from src.domain.roster import Meal
from src.excel import substitute_excel_metrics as metrics
from src.generators.substitute_generator import _build_sheet, build_substitute_workbook


ROOT = Path(__file__).resolve().parents[1]


def synthetic(sizes=(1, 2, 3, 4, 5, 6)):
    keys = ("sequence", "jurisdiction", "name", "detail_address", "road_address", "mobile_phone", "note")
    labels = ("배송순서", "관할동", "이름", "상세주소", "신주소", "핸드폰", "비고2")
    rows = []
    for group, size in enumerate(sizes):
        for member in range(size):
            values = dict(zip(keys, (f"{group + 1}번", "가동" if group < 3 else "나동",
                                    f"합성{group}-{member}", "101호", "테스트로 12, 합성아파트 101동",
                                    f"010-0000-{group:04d}", "합성 메모입니다 문 앞에 놓아 주세요"), strict=True))
            rows.append(SubstituteRow(uuid4(), uuid4(), "synthetic", "합성기사", values, 1, group == 1))
    return PreparedSubstituteData(date(2026, 8, 28), Meal.BREAKFAST, tuple(rows),
                                  tuple(SubstituteColumn(k, v) for k, v in zip(keys, labels, strict=True)), keys, {})


def realistic_typography(entitlements=1):
    data = synthetic((3, 1, 2, 2, 1, 1, 3, 2, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 2, 1, 1, 1, 3, 1))
    addresses = (
        "경기도 성남시 분당구 정자일로 230, 한솔마을 청구아파트 107동 1203호",
        "경기도 성남시 분당구 돌마로 486, 양지마을 금호아파트 301동 703호",
        "경기도 성남시 분당구 미금일로 58, 까치마을 신원아파트 205동 1402호",
        "경기도 성남시 분당구 판교역로 102, 백현마을 602동 1104호",
    )
    rows = []
    for index, row in enumerate(data.rows):
        values = {**row.values, "name": f"합성{index:02d}",
                  "road_address": addresses[index % len(addresses)],
                  "detail_address": "한솔마을 청구아파트 107동 1203호",
                  "note": "문 앞에 놓고 벨 누르지 말아주세요 부재 시 보호자에게 연락 부탁드립니다"}
        if index in (3, 8, 9):
            values["mobile_phone"] = ""
        rows.append(replace(row, values=values, entitlement_count=entitlements))
    return replace(data, rows=tuple(rows))


def test_phone_groups_db_entitlements_and_hidden_columns():
    data = synthetic((1,) * 7)
    phones = ("010-0000-1234", "01000001234.0", "", "", "01000001234", "01000009999", "01000001234")
    data = replace(data, rows=tuple(replace(row, values={**row.values, "mobile_phone": phone}, entitlement_count=3)
                                    for row, phone in zip(data.rows, phones, strict=True)))
    wb, _, layout = _build_sheet(data, SubstituteOptions("synthetic", ("name", "sequence")))
    assert [(g.source_start, g.source_end, g.meal_count) for g in layout.groups] == [(0, 1, 2), (2, 2, 1), (3, 3, 1), (4, 4, 1), (5, 5, 1), (6, 6, 1)]
    ws = wb.active
    assert [ws.cell(r, 1).value for r in range(3, 10)] == [row.values["name"] for row in data.rows]
    assert ws["B3"].value == "1번" and "B3:B4" in {str(r) for r in ws.merged_cells.ranges}
    assert ws["C3"].value == 2
    assert ws["C3"].number_format == "General"
    assert ws["A3"].fill.fgColor.rgb.endswith("E6B8B7")
    assert ws["A5"].fill.patternType is None
    assert ws["F10"].value == "5식"
    assert ws["G13"].value == 7
    assert ws["B13"].value == 5 and ws["C13"].value == 1
    assert ws["D13"].value == ""
    assert (ws["B11"].value, ws["C11"].value, ws["G11"].value) == (1, 1, 3)
    assert (ws["B12"].value, ws["G12"].value) == (4, 4)
    assert ws["A11"].font.bold
    assert ws.freeze_panes is None and ws.oddFooter.center.text is None
    serialized = BytesIO()
    wb.save(serialized)
    serialized.seek(0)
    reopened = openpyxl.load_workbook(serialized)
    assert reopened.active["A3"].fill.fgColor.rgb.endswith("E6B8B7")
    assert reopened.active["C3"].value == 2
    assert reopened.active["G13"].value == 7
    reopened.close()
    wb.close()


@pytest.mark.parametrize("meal,expected_counts,expected_total", (
    (Meal.BREAKFAST, (1, 2, 3), 6),
    (Meal.DINNER, (3, 4, 6), 13),
))
def test_household_fill_uses_member_rows_and_nonbreakfast_counts_stay_intact(meal, expected_counts, expected_total):
    data = synthetic((1, 2, 3))
    entitlements = (3, 1, 3, 3, 1, 2)
    data = replace(data, meal=meal, rows=tuple(
        replace(row, entitlement_count=count)
        for row, count in zip(data.rows, entitlements, strict=True)
    ))
    workbook, _, layout = _build_sheet(data, SubstituteOptions("synthetic", ("name", "sequence")))
    sheet = workbook.active
    assert [group.meal_count for group in layout.groups] == list(expected_counts)
    assert sheet["A3"].fill.patternType is None
    assert sheet["A4"].fill.fgColor.rgb.endswith("E6B8B7")
    assert sheet["A6"].fill.fgColor.rgb.endswith("B8CCE4")
    assert sheet["C3"].value == expected_counts[0]
    assert sheet.cell(layout.used_rows, layout.used_cols).value == expected_total
    workbook.close()


@pytest.mark.parametrize("cleanup_failure", [None, "close", "quit"])
def test_measurement_error_is_explicit_and_owned_excel_is_closed(monkeypatch, cleanup_failure):
    events = []
    class Book:
        EnableAutoRecover = True
        def Worksheets(self, index): return object()
        def Close(self, **kwargs):
            events.append(("close", kwargs))
            if cleanup_failure == "close":
                raise OSError("synthetic close failure")
    book = Book()
    class App:
        AutoRecover = types.SimpleNamespace(Enabled=True)
        Workbooks = types.SimpleNamespace(Add=lambda: book)
        def Quit(self):
            events.append(("quit",))
            if cleanup_failure == "quit":
                raise OSError("synthetic cleanup failure")
    app = App()
    import win32com.client
    monkeypatch.setitem(sys.modules, "pythoncom", types.SimpleNamespace(CoInitialize=lambda: events.append(("init",)), CoUninitialize=lambda: events.append(("uninit",))))
    import win32com.client
    monkeypatch.setattr(metrics, "_create_excel_application", lambda: app)
    def fail(*args): raise ValueError("synthetic measurement failure")
    monkeypatch.setattr(metrics, "_measure", fail)
    with pytest.raises(metrics.SubstituteExcelError, match="Microsoft Excel") as error:
        build_substitute_workbook(synthetic((1,)), SubstituteOptions("synthetic", ("name",)))
    assert isinstance(error.value.__cause__, ValueError)
    assert events == [("init",), ("close", {"SaveChanges": False}), ("quit",), ("uninit",)]
    assert book.EnableAutoRecover is False
    assert app.AutoRecover.Enabled is True  # Do not change the user's application preference.


def test_missing_excel_does_not_publish_an_approximation(monkeypatch, tmp_path):
    import win32com.client
    def unavailable(*args): raise OSError("Excel unavailable")
    monkeypatch.setattr(metrics, "_create_excel_application", unavailable)
    from src.generators.substitute_generator import make_substitute_publication
    with pytest.raises(metrics.SubstituteExcelError, match="Microsoft Excel"):
        make_substitute_publication(synthetic((1,)), SubstituteOptions("synthetic", ("name",)),
                                    tmp_path / "missing.xlsx", output_password="test-only")
    assert not list(tmp_path.iterdir())


@pytest.fixture(scope="module")
def legacy():
    """Load the actual active source under a private namespace; rewrite imports only."""
    base = ROOT / "tmp" / "배송 대타 매크로" / "src"
    if not base.exists():
        pytest.skip("Active legacy source is not included in this checkout")
    prefix = "_substitute_legacy_reference"
    for package in (prefix, prefix + ".excel", prefix + ".excel.page_formatting"):
        module = types.ModuleType(package)
        module.__path__ = []
        sys.modules[package] = module
    # groups only needs these two tiny I/O utilities; load their actual source
    # definitions without the source application's optional GUI/I/O imports.
    io_source = (base / "excel_io.py").read_text(encoding="utf-8")
    import ast
    tree = ast.parse(io_source)
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in ("txt", "ord_groups")]
    io_module = types.ModuleType(prefix + ".excel_io")
    io_module.__dict__.update(pd=pd, re=re)
    exec("from __future__ import annotations\n" + "\n".join(ast.get_source_segment(io_source, node) for node in functions), io_module.__dict__)
    sys.modules[io_module.__name__] = io_module
    modules = {}
    for relative in ("config", "excel.colors", "excel.groups", "excel.text_portrait", "excel.sheet_writer", "excel.page_formatting.metrics", "excel.page_formatting.setup", "excel.page_formatting.layout"):
        path = base.joinpath(*relative.split(".")).with_suffix(".py")
        module = types.ModuleType(prefix + "." + relative)
        module.__package__ = module.__name__.rpartition(".")[0]
        sys.modules[module.__name__] = module
        exec(compile(path.read_text(encoding="utf-8").replace("from src.", f"from {prefix}."), str(path), "exec"), module.__dict__)
        modules[relative] = module
    yield modules
    for key in tuple(sys.modules):
        if key.startswith(prefix):
            del sys.modules[key]


@pytest.fixture(scope="module")
def excel():
    pythoncom = pytest.importorskip("pythoncom")
    win32 = pytest.importorskip("win32com.client")
    pythoncom.CoInitialize()
    application = None
    try:
        application = metrics._create_excel_application()
        application.Visible = False
        application.DisplayAlerts = False
        yield application
    finally:
        if application is not None:
            application.Quit()
        application = None
        import gc
        gc.collect()
        pythoncom.CoUninitialize()


def raw_borders(package):
    # openpyxl's merge loader can replace a native thick bottom with the
    # anchor's thin interior border. Inspect serialized perimeter sides directly.
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    with ZipFile(package) as archive:
        styles = ElementTree.fromstring(archive.read("xl/styles.xml"))
        borders = list(styles.find(f"{ns}borders"))
        xfs = list(styles.find(f"{ns}cellXfs"))
        sheet = ElementTree.fromstring(archive.read("xl/worksheets/sheet1.xml"))
    result = {}
    for cell in sheet.iter(f"{ns}c"):
        border = borders[int(xfs[int(cell.get("s", 0))].get("borderId", 0))]
        result[cell.get("r")] = {
            edge: (border.find(f"{ns}{edge}").get("style") if border.find(f"{ns}{edge}") is not None else None)
            for edge in ("left", "right", "top", "bottom")}
    return result


@pytest.mark.parametrize("orientation", ["portrait", "landscape", "auto"])
@pytest.mark.parametrize("split", ["none", "dong", "groups"])
def test_legacy_static_styles_with_corrected_typography(excel, legacy, tmp_path, orientation, split):
    data = synthetic()
    # Grouping columns deliberately hidden for half the combinations.
    columns = ("name", "road_address", "note") if split != "groups" else data.default_columns
    options = SubstituteOptions("synthetic", columns, orientation=orientation, split=split, groups_per_page=2)
    actual, labels, layout = _build_sheet(data, options)
    source = pd.DataFrame([{column.label: row.values[column.key] for column in data.columns} for row in data.rows])
    source["식사 구분"] = ["조식_BOX" if row.box else "조식" for row in data.rows]
    display = legacy["excel.text_portrait"].preprocess_portrait(source[labels])
    ref_layout = legacy["excel.groups"].build_sheet_layout(source, display_col_count=len(labels) + 1)
    reference_book = excel.Workbooks.Add()
    measurement_book = None
    try:
        reference_book.EnableAutoRecover = False
        reference_sheet = reference_book.Worksheets(1)
        legacy["excel.sheet_writer"].write_sheet(reference_sheet, display, data.meal.value, "합성기사", "20260828", layout=ref_layout, source_df=source)
        legacy["excel.page_formatting.layout"].apply_page_formatting(reference_sheet, display, mode={"none": 0, "dong": 1, "groups": 2}[split], max_groups_limit=2, orientation_pref={"auto": 0, "portrait": 1, "landscape": 2}[orientation], sheet_layout=ref_layout)
        expected_path = tmp_path / "reference.xlsx"
        reference_book.SaveAs(str(expected_path), FileFormat=51)
        measurement_book = excel.Workbooks.Add()
        measurement_book.EnableAutoRecover = False
        metrics._measure(actual, measurement_book.Worksheets(1), labels, layout, options)
        buffer = BytesIO()
        actual.save(buffer)
        buffer.seek(0)
        actual_borders, expected_borders = raw_borders(BytesIO(buffer.getvalue())), raw_borders(expected_path)
        measured = openpyxl.load_workbook(buffer)
        expected = openpyxl.load_workbook(expected_path)
        try:
            a, b = measured.active, expected.active
            assert a.max_row == b.max_row and a.max_column == b.max_column
            assert {str(r) for r in a.merged_cells.ranges} == {str(r) for r in b.merged_cells.ranges}
            for row in range(1, a.max_row + 1):
                assert 0 < a.row_dimensions[row].height <= 409.5, ("height", row)
                assert a.row_dimensions[row].thickTop == b.row_dimensions[row].thickTop
                assert a.row_dimensions[row].thickBot == b.row_dimensions[row].thickBot
                for col in range(1, a.max_column + 1):
                    ac, bc = a.cell(row, col), b.cell(row, col)
                    if labels[col - 1:col] == ["신주소"] and 3 <= row <= layout.data_end_row:
                        # The corrected common font may permit removing a break
                        # which the legacy address-only enlargement retained.
                        normalize = lambda value: re.sub(r"[\s,]+", " ", str(value or "")).strip()
                        assert normalize(ac.value) == normalize(bc.value), ("address", row, col)
                    else:
                        assert (ac.value or "") == (bc.value or ""), ("value", row, col)
                    if isinstance(ac.value, (int, float)):
                        assert ac.number_format == bc.number_format == "General", ("numeric format", row, col)
                    if ac.__class__.__name__ != "MergedCell":
                        assert (ac.font.name, ac.font.bold) == (bc.font.name, bc.font.bold), ("font", row, col)
                        if row >= 3:
                            assert ac.font.sz == a.cell(3, 1).font.sz, ("common body font", row, col)
                        assert (ac.alignment.horizontal, ac.alignment.vertical, ac.alignment.wrap_text) == (bc.alignment.horizontal, bc.alignment.vertical, bc.alignment.wrap_text), ("alignment", row, col)
                    assert ac.fill.patternType == bc.fill.patternType, ("fill", row, col)
                    if ac.fill.patternType:
                        assert ac.fill.fgColor.rgb[-6:] == bc.fill.fgColor.rgb[-6:], ("color", row, col)
                    merged = next((r for r in a.merged_cells.ranges if ac.coordinate in r), None)
                    for edge in ("left", "right", "top", "bottom"):
                        # The anchor can carry whole-merge bottom/right borders
                        # in SpreadsheetML; only the perimeter is rendered.
                        if merged and ((edge == "bottom" and row < merged.max_row) or
                                       (edge == "top" and row > merged.min_row) or
                                       (edge == "left" and col > merged.min_col) or
                                       (edge == "right" and col < merged.max_col)):
                            continue
                        assert actual_borders.get(ac.coordinate, {}).get(edge) == expected_borders.get(bc.coordinate, {}).get(edge), ("border", row, col, edge)
            for col in range(1, a.max_column + 1):
                letter = openpyxl.utils.get_column_letter(col)
                assert a.column_dimensions[letter].width > 0, ("width", letter)
            assert a.page_setup.orientation == b.page_setup.orientation
            assert a.page_setup.fitToWidth == (b.page_setup.fitToWidth or 1) == 1
            assert a.page_setup.fitToHeight == b.page_setup.fitToHeight == 0
            assert a.print_options.horizontalCentered == b.print_options.horizontalCentered
            assert a.print_title_rows == b.print_title_rows
            for margin in ("left", "right", "top", "bottom", "header", "footer"):
                assert getattr(a.page_margins, margin) == pytest.approx(getattr(b.page_margins, margin))
            assert all(not (group.start_row <= br.id < group.end_row)
                       for br in a.row_breaks.brk for group in layout.groups)
        finally:
            measured.close()
            expected.close()
    finally:
        actual.close()
        if measurement_book is not None:
            measurement_book.Close(SaveChanges=False)
        reference_book.Close(SaveChanges=False)


def test_formula_like_source_is_never_evaluated_during_measurement(excel):
    data = synthetic((1,))
    values = {**data.rows[0].values, "sequence": "0001", "name": "=1+1", "road_address": "=2+2", "note": "=3+3"}
    data = replace(data, rows=(replace(data.rows[0], values=values),))
    options = SubstituteOptions("synthetic", ("sequence", "name", "road_address", "note"))
    workbook, labels, layout = _build_sheet(data, options)
    measured = excel.Workbooks.Add()
    try:
        measured.EnableAutoRecover = False
        sheet = measured.Worksheets(1)
        metrics._measure(workbook, sheet, labels, layout, options)
        for col, value in enumerate(("0001", "=1+1", "=2+2", "=3+3"), 1):
            assert sheet.Cells(3, col).HasFormula is False
            assert sheet.Cells(3, col).Value == value
            assert workbook.active.cell(3, col).value == value
        assert workbook.active.page_setup.orientation == "portrait"  # Seven used columns.
    finally:
        measured.Close(SaveChanges=False)
        workbook.close()


@pytest.mark.parametrize("orientation,entitlements", [("auto", 1), ("portrait", 1), ("landscape", 1), ("auto", 3)])
def test_real_route_shape_common_font_and_print_settings(excel, orientation, entitlements):
    data = realistic_typography(entitlements)
    options = SubstituteOptions("synthetic", data.default_columns, orientation=orientation)
    workbook, labels, layout = _build_sheet(data, options)
    measured = excel.Workbooks.Add()
    try:
        measured.EnableAutoRecover = False
        sheet = measured.Worksheets(1)
        metrics._measure(workbook, sheet, labels, layout, options)
        ws = workbook.active
        assert len(data.rows) == 38 and len(layout.groups) == 28
        body = ws["C3"].font.sz
        assert body >= 8
        for row in range(3, layout.data_end_row + 1):
            for col in range(1, layout.used_cols + 1):
                cell = ws.cell(row, col)
                if cell.__class__.__name__ != "MergedCell":
                    assert cell.font.sz == body
        assert ws["A1"].font.sz == 12
        assert ws["A2"].font.sz == body + 2
        assert ws.page_setup.orientation == ("landscape" if orientation == "auto" else orientation)
        assert str(ws.page_setup.paperSize) == ws.PAPERSIZE_A4
        assert ws.page_setup.fitToWidth == 1 and ws.page_setup.fitToHeight == 0
        assert ws.print_options.horizontalCentered is True
        assert ws.print_title_rows == "$1:$2"
        assert sheet.PageSetup.Zoom is False
        assert sheet.PageSetup.FitToPagesWide == 1 and sheet.PageSetup.FitToPagesTall is False
        assert ws.page_margins.left == pytest.approx(.3 / 2.54)
        assert ws.page_margins.right == ws.page_margins.top == ws.page_margins.bottom == ws.page_margins.left
        assert ws.page_margins.header == ws.page_margins.footer == 0
        # Width must fit without Excel silently scaling readable text back down.
        paper_width = 842 if ws.page_setup.orientation == "landscape" else 595
        available = paper_width - sheet.PageSetup.LeftMargin - sheet.PageSetup.RightMargin
        assert sum(sheet.Columns(c).Width for c in range(1, layout.used_cols + 1)) <= available
        # Breakfast substitute sheets count recipients even when each receives
        # a three-meal bundle; daily meal reports retain the entitlement total.
        assert ws.cell(layout.used_rows, max(5, max(g.meal_count for g in layout.groups)) + 2).value == 38
        from src.excel.substitute_page_layout import page_chunks_from_breaks
        assert all(len(page) <= 18 for page in page_chunks_from_breaks(sheet, layout.groups))
        for page_break in sheet.HPageBreaks:
            row = page_break.Location.Row
            assert all(not (group.start_row < row <= group.end_row) for group in layout.groups)
        assert not ws.freeze_panes and not ws.oddFooter.center.text
    finally:
        measured.Close(SaveChanges=False)
        workbook.close()


@pytest.mark.parametrize("orientation", ["portrait", "landscape"])
def test_mixed_notes_route_keeps_common_font(excel, orientation):
    data = realistic_typography()
    data = replace(data, rows=tuple(replace(row, values={**row.values,
        "note": row.values["note"] if index % 7 == 0 else "문 앞 배송" if index % 5 == 0 else ""})
        for index, row in enumerate(data.rows)))
    options = SubstituteOptions("synthetic", data.default_columns, orientation=orientation)
    workbook, labels, layout = _build_sheet(data, options)
    measured = excel.Workbooks.Add()
    try:
        measured.EnableAutoRecover = False
        sheet = measured.Worksheets(1)
        metrics._measure(workbook, sheet, labels, layout, options)
        ws = workbook.active
        assert ws["C3"].font.sz == ws["E3"].font.sz >= 8
        assert ws.cell(layout.used_rows, max(5, max(g.meal_count for g in layout.groups)) + 2).value == 38
        for page_break in sheet.HPageBreaks:
            assert all(not (group.start_row < page_break.Location.Row <= group.end_row)
                       for group in layout.groups)
        target = ROOT / "artifacts" / "delivery-substitute-parity" / "typography"
        target.mkdir(parents=True, exist_ok=True)
        workbook.save(target / f"synthetic-mixed-notes-{orientation}.xlsx")
    finally:
        measured.Close(SaveChanges=False)
        workbook.close()


def test_measured_merged_text_height_is_not_capped_back_to_100(excel):
    data = synthetic((2,))
    note = " ".join(f"합성메모{number:02d}" for number in range(16))
    data = replace(data, rows=tuple(replace(row, values={**row.values, "note": note}) for row in data.rows))
    options = SubstituteOptions("synthetic", data.default_columns, orientation="landscape")
    workbook, labels, layout = _build_sheet(data, options)
    measured = excel.Workbooks.Add()
    try:
        measured.EnableAutoRecover = False
        sheet = measured.Worksheets(1)
        metrics._measure(workbook, sheet, labels, layout, options)
        probe = sheet.Cells(layout.used_rows + 2, layout.used_cols + 2)
        probe.NumberFormat = "@"
        probe.Value = sheet.Cells(3, 7).Value
        probe.Font.Name = sheet.Cells(3, 7).Font.Name
        probe.Font.Size = sheet.Cells(3, 7).Font.Size
        probe.WrapText = True
        probe.EntireColumn.ColumnWidth = sheet.Columns(7).ColumnWidth
        probe.EntireRow.AutoFit()
        required = probe.RowHeight
        assert required > 200  # The old 100pt-per-row cap clips this two-row merge.
        assert sheet.Rows(3).RowHeight + sheet.Rows(4).RowHeight >= required
    finally:
        measured.Close(SaveChanges=False)
        workbook.close()


def test_automatic_break_before_sixteenth_household_has_no_orphan(excel, tmp_path):
    from src.excel.substitute_page_layout import (
        SheetGroup, apply_base_page_setup, apply_page_breaks_by_groups,
        page_chunks_from_breaks,
    )
    sizes = (3,1,2,2,1,1,3,2,1,1,1,1,1,1,1,1,1,1,1,1,1,1,2,1,1,1,3,1)
    heights = (24,16.5,16.5,16.5,16.5,48,24,24,24,24,47,60,16.5,16.5,16.5,24,24,48,48,47,47,47,60,47,48,48,47,84,47,47,72,42,42,72,47,48,16.5,16.5,16.5,47,16.5,16.5,16.5,16.5)
    book = excel.Workbooks.Add()
    reopened = None
    try:
        book.EnableAutoRecover = False
        sheet = book.Worksheets(1)
        book.Styles("Normal").Font.Name = "맑은 고딕"
        book.Styles("Normal").Font.Size = 11
        sheet.Range("A1:H44").Font.Size = 9
        for column, width in enumerate((8.63,6.75,5.38,11.75,23.63,12,13.25,4.88), 1):
            sheet.Columns(column).ColumnWidth = width
        groups = []
        row = 3
        for index, size in enumerate(sizes):
            groups.append(SheetGroup(row-3, row+size-4, row, row+size-1, "합성동", size))
            for member in range(size):
                sheet.Cells(row+member, 3).Value = f"TEST-{index+1}-{member+1}"
            sheet.Range(sheet.Cells(row,8), sheet.Cells(row+size-1,8)).Merge()
            row += size
        for row in range(41, 45):
            sheet.Cells(row, 1).Value = f"SUMMARY-{row}"
        for row, height in enumerate(heights, 1):
            sheet.Rows(row).RowHeight = height
        apply_base_page_setup(sheet, 44, 8, 1)
        # The old fixed boundary isolates the 16th household after native overflow.
        sheet.HPageBreaks.Add(Before=sheet.Rows(26))
        assert [len(page) for page in page_chunks_from_breaks(sheet, tuple(groups))] == [15,1,12]
        apply_page_breaks_by_groups(sheet, tuple(groups), 2, 16)
        assert [len(page) for page in page_chunks_from_breaks(sheet, tuple(groups))] == [15,13]
        target = tmp_path / "pagination.xlsx"
        book.SaveAs(str(target), FileFormat=51)
        book.Close(SaveChanges=False)
        book = None
        reopened = excel.Workbooks.Open(str(target), ReadOnly=True)
        assert [len(page) for page in page_chunks_from_breaks(reopened.Worksheets(1), tuple(groups))] == [15,13]
        reopened.Worksheets(1).ExportAsFixedFormat(0, str(tmp_path / "pagination.pdf"))
        import pymupdf
        with pymupdf.open(tmp_path / "pagination.pdf") as pdf:
            pages = [page.get_text() for page in pdf]
            household_counts = [len(set(re.findall(r"TEST-(\d+)-", text))) for text in pages]
            assert [count for count in household_counts if count] == [15, 13]
            assert "TEST-16-1" in pages[1] and "TEST-17-1" in pages[1]
            assert sum(len(re.findall(r"TEST-\d+-\d+", text)) for text in pages) == 38
            assert all(f"SUMMARY-{row}" in "\n".join(pages) for row in range(41,45))
    finally:
        if reopened is not None:
            reopened.Close(SaveChanges=False)
        if book is not None:
            book.Close(SaveChanges=False)


def test_extra_spacing_reserves_tall_rows_without_lowering_minima():
    from src.excel.substitute_page_layout import SheetGroup, _distribute_extra_height_uniform
    rows = {index: types.SimpleNamespace(RowHeight=height)
            for index, height in enumerate((10,10,20,90,10),1)}
    sheet = types.SimpleNamespace(Rows=lambda row: rows[row])
    groups = [SheetGroup(0,0,3,3,"합성동",1), SheetGroup(1,2,4,5,"합성동",2)]
    _distribute_extra_height_uniform(sheet, [groups], 5, 180)
    assert sum(rows[row].RowHeight for row in (3,4,5)) <= 155
    assert rows[3].RowHeight >= 20
    assert rows[4].RowHeight >= 90
    assert rows[5].RowHeight >= 10
    assert rows[4].RowHeight > rows[5].RowHeight
