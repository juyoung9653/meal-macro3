"""Measure the substitute layout in an owned, unsaved Excel instance.

Only values cross COM. Excel never opens or saves a plaintext workbook; the
existing publication boundary serializes and encrypts the resulting openpyxl
workbook in memory. Excel is required because its AutoFit/page-break behavior
cannot be reproduced exactly with a character-count approximation.
"""
from __future__ import annotations

from copy import copy
from math import ceil, floor
from traceback import clear_frames
from xml.etree import ElementTree

from openpyxl.styles import Alignment, Border, Font, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.pagebreak import Break

from src.excel.substitute_page_layout import SheetLayout, apply_page_formatting


class SubstituteExcelError(RuntimeError):
    """Excel is unavailable or could not complete the requested print layout."""


def _measure(workbook, excel_sheet, columns, layout, options, *, preserve_styles=False):
    ws = workbook.active
    used = excel_sheet.Range(excel_sheet.Cells(1, 1),
                             excel_sheet.Cells(layout.used_rows, layout.used_cols))
    # Text formatting must precede Value assignment: strings beginning with '='
    # must never be evaluated, and source phone/sequence leading zeroes survive.
    used.NumberFormat = "@"
    used.Value = tuple(tuple(ws.cell(row, col).value or ""
                            for col in range(1, layout.used_cols + 1))
                       for row in range(1, layout.used_rows + 1))
    for row in range(1, layout.used_rows + 1):
        for col in range(1, layout.used_cols + 1):
            cell = ws.cell(row, col)
            if isinstance(cell.value, (int, float)):
                excel_sheet.Cells(row, col).NumberFormat = "0"
        if ws.cell(row, 1).font.bold:
            excel_sheet.Range(excel_sheet.Cells(row, 1),
                              excel_sheet.Cells(row, layout.used_cols)).Font.Bold = True
    for merged in ws.merged_cells.ranges:
        excel_sheet.Range(str(merged)).Merge()
    excel_sheet.Rows(1).Font.Size = 12
    # Borders affect native AutoFit padding too; preserve the source macro's
    # whole-grid thin borders and BOX outer-edge weights before measurement.
    if not preserve_styles:
        used.Borders.LineStyle = 1
        for group in layout.groups:
            if ws.cell(group.start_row, 1).border.left.style == "thick":
                block = excel_sheet.Range(excel_sheet.Cells(group.start_row, 1),
                                          excel_sheet.Cells(group.end_row, layout.used_cols))
                for edge in (7, 8, 9, 10):
                    block.Borders(edge).Weight = 4
    if preserve_styles:
        # Measure the actual chosen fonts before AutoFit and pagination.
        for row in range(1, layout.used_rows + 1):
            for col in range(1, layout.used_cols + 1):
                cell = ws.cell(row, col)
                target = excel_sheet.Cells(row, col)
                if cell.font.name:
                    target.Font.Name = cell.font.name
                target.Font.Size = cell.font.sz or 11
                target.Font.Bold = bool(cell.font.bold)
                target.Font.Italic = bool(cell.font.italic)
                for edge, side in ((7, cell.border.left), (8, cell.border.top),
                                   (9, cell.border.bottom), (10, cell.border.right)):
                    native = target.Borders(edge)
                    style = side.style if side else None
                    native.LineStyle = {"dashed": -4115, "dotted": -4118, "double": -4119,
                                        "dashDot": 4, "dashDotDot": 5}.get(style, 1) if style else -4142
                    if style:
                        native.Weight = {"hair": 1, "medium": -4138, "thick": 4}.get(style, 2)
    apply_page_formatting(excel_sheet, columns,
                          mode={"none": 0, "dong": 1, "groups": 2}[options.split],
                          max_groups_limit=options.groups_per_page,
                          orientation_pref={"auto": 0, "portrait": 1, "landscape": 2}[options.orientation],
                          sheet_layout=layout, preserve_styles=preserve_styles)
    # OOXML column width includes padding while COM ColumnWidth does not.
    # Derive the Normal font's character width from a spare column so Korean
    # Office defaults and other installed Normal fonts produce identical widths.
    scratch = excel_sheet.Columns(layout.used_cols + 1)
    scratch.ColumnWidth = 20
    width20 = scratch.Width
    scratch.ColumnWidth = 50
    character_width_pt = (scratch.Width - width20) / 30
    for col in range(1, layout.used_cols + 1):
        ws.column_dimensions[get_column_letter(col)].width = floor(
            excel_sheet.Columns(col).Width / character_width_pt * 256) / 256
    # SpreadsheetML exposes unrounded internal row heights. COM RowHeight is
    # rounded for screen DPI and can differ from the native saved/printed size.
    import pythoncom
    xml = used._oleobj_.Invoke(used._oleobj_.GetIDsOfNames("Value"), 0,
                              pythoncom.DISPATCH_PROPERTYGET, True, 11)
    tree = ElementTree.fromstring(xml)
    ns = "{urn:schemas-microsoft-com:office:spreadsheet}"
    xns = "{urn:schemas-microsoft-com:office:excel}"
    table = tree.find(f"{ns}Worksheet/{ns}Table")
    # Excel suppresses the weaker side of a shared BOX edge. Keeping both
    # sides makes that heavy edge repeat at the next printed page's top.
    borders = {}
    for style in tree.findall(f"{ns}Styles/{ns}Style"):
        sides = {edge: Side() for edge in ("left", "right", "top", "bottom")}
        for edge in style.findall(f"{ns}Borders/{ns}Border"):
            position = edge.get(f"{ns}Position").lower()
            weight = int(edge.get(f"{ns}Weight", 1))
            sides[position] = Side(style={1: "thin", 2: "medium", 3: "thick"}[weight],
                                   color=edge.get(f"{ns}Color", "#000000").lstrip("#"))
        borders[style.get(f"{ns}ID")] = Border(**sides)
    for row in ws.iter_rows(min_row=1, max_row=layout.used_rows, max_col=layout.used_cols):
        for cell in row:
            if not preserve_styles:
                cell.border = Border()
    ws.sheet_format.defaultRowHeight = ceil(float(table.get(f"{ns}DefaultRowHeight")) * 20) / 20
    heights = {}
    row_index = 0
    thick_tops, thick_bottoms = set(), set()
    for element in table.findall(f"{ns}Row"):
        row_index = int(element.get(f"{ns}Index", row_index + 1))
        height = float(element.get(f"{ns}Height", table.get(f"{ns}DefaultRowHeight")))
        heights[row_index] = ceil(height * 20) / 20
        col_index = 0
        for cell in element.findall(f"{ns}Cell"):
            col_index = int(cell.get(f"{ns}Index", col_index + 1))
            style_id = cell.get(f"{ns}StyleID", element.get(f"{ns}StyleID", "Default"))
            border = borders[style_id]
            if not preserve_styles:
                ws.cell(row_index, col_index).border = border
            if border.top.style in ("medium", "thick"):
                thick_tops.add(row_index)
            if border.bottom.style in ("medium", "thick"):
                thick_bottoms.add(row_index + int(cell.get(f"{ns}MergeDown", 0)))
            col_index += int(cell.get(f"{ns}MergeAcross", 0))
    if not preserve_styles:
        for merged in ws.merged_cells.ranges:
            merged.format()
    for row in range(1, layout.used_rows + 1):
        ws.row_dimensions[row].thickTop = row in thick_tops or row - 1 in thick_bottoms
        ws.row_dimensions[row].thickBot = row in thick_bottoms or row + 1 in thick_tops
    default = next(style for style in tree.findall(f"{ns}Styles/{ns}Style")
                   if style.get(f"{ns}ID") == "Default")
    font_element = default.find(f"{ns}Font")
    families = {"Automatic": 0, "Roman": 1, "Swiss": 2, "Modern": 3, "Script": 4, "Decorative": 5}
    normal_font = Font(name=font_element.get(f"{ns}FontName"),
                       size=float(font_element.get(f"{ns}Size")),
                       family=families.get(font_element.get(f"{xns}Family"), 0),
                       charset=font_element.get(f"{xns}CharSet"), color="000000")
    alignment_element = default.find(f"{ns}Alignment")
    vertical = alignment_element.get(f"{ns}Vertical", "Bottom").lower() if alignment_element is not None else "bottom"
    horizontal = alignment_element.get(f"{ns}Horizontal") if alignment_element is not None else None
    horizontal = {"General": None, "CenterAcrossSelection": "centerContinuous"}.get(horizontal, horizontal.lower() if horizontal else None)
    alignment = Alignment(horizontal=horizontal, vertical=vertical, wrap_text=True)
    workbook._named_styles[0].font = normal_font
    workbook._named_styles[0].alignment = Alignment(horizontal=horizontal, vertical=vertical)
    for row in range(1, layout.used_rows + 1):
        ws.row_dimensions[row].height = heights[row]
        for col in range(1, layout.used_cols + 1):
            cell = ws.cell(row, col)
            font = copy(cell.font)
            font.name = normal_font.name
            font.family = normal_font.family
            font.charset = normal_font.charset
            font.sz = excel_sheet.Cells(row, col).Font.Size
            if not preserve_styles:
                cell.font = font
            cell.alignment = alignment
    # Legacy's last step removes address breaks only if Excel says they fit.
    if "신주소" in columns:
        col = columns.index("신주소") + 1
        for row in range(3, layout.data_end_row + 1):
            cell = ws.cell(row, col)
            if cell.__class__.__name__ != "MergedCell":
                cell.value = excel_sheet.Cells(row, col).Value or ""
                cell.data_type = "s"
    # Only manual breaks are serialized; native automatic breaks are recomputed
    # by Excel for the same printer and page setup when the published file opens.
    for page_break in excel_sheet.HPageBreaks:
        if page_break.Type == -4135:  # xlPageBreakManual
            ws.row_breaks.append(Break(id=page_break.Location.Row - 1))


def apply_excel_metrics(workbook, columns: list[str], layout: SheetLayout, options, *, preserve_styles=False) -> None:
    try:
        import pythoncom
        import win32com.client
    except ImportError as exc:
        raise SubstituteExcelError("배송대타 서식을 맞추려면 Windows용 Microsoft Excel과 pywin32가 필요합니다.") from exc
    excel = excel_workbook = excel_sheet = None
    failure = None
    pythoncom.CoInitialize()
    try:
        excel = win32com.client.DispatchEx("Excel.Application")
        excel.Visible = False
        excel.DisplayAlerts = False
        excel.ScreenUpdating = False
        excel.EnableEvents = False
        excel.AutomationSecurity = 3
        excel_workbook = excel.Workbooks.Add()
        excel_workbook.EnableAutoRecover = False
        excel_sheet = excel_workbook.Worksheets(1)
        _measure(workbook, excel_sheet, columns, layout, options,
                 **({"preserve_styles": True} if preserve_styles else {}))
    except Exception as exc:
        failure = exc
        # The traceback must not retain dead measurement frames with COM range
        # proxies after their apartment is uninitialized. Keep the exception and
        # traceback locations, release only frame-local references.
        clear_frames(exc.__traceback__)
    finally:
        excel_sheet = None
        try:
            if excel_workbook is not None:
                excel_workbook.Close(SaveChanges=False)
        except Exception as exc:
            if failure is None:
                failure = exc
        finally:
            excel_workbook = None
            try:
                if excel is not None:
                    excel.Quit()
            except Exception as exc:
                if failure is None:
                    failure = exc
            finally:
                excel = None
                pythoncom.CoUninitialize()
    if failure is not None:
        raise SubstituteExcelError("배송대타 Excel 서식 처리에 실패했습니다. Microsoft Excel 설치와 기본 프린터 설정을 확인해주세요.") from failure
