"""DB substitute sheets with legacy styling and readable native Excel layout."""
from __future__ import annotations

from collections import Counter
from copy import copy
from pathlib import Path
import re

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.page import PageMargins

from src.domain.delivery_substitute import PreparedSubstituteData, SubstituteOptions
from src.domain.roster import Meal
from src.excel.delivery_templates import load_substitute_styles, substitute_styles_are_default, STYLE_COLUMNS
from src.excel.publication import WorkbookPublicationResult, publish_workbook
from src.excel.substitute_excel_metrics import apply_excel_metrics
from src.excel.substitute_page_layout import SheetGroup, SheetLayout

SUBSTITUTE_RENDER_CONTRACT_VERSION = 7
_LABELS = {"name": "이름", "road_address": "신주소", "note": "비고2"}
_FILLS = {2: "E6B8B7", 3: "B8CCE4", 4: "D8E4BC", 5: "CCC0DA"}
_THIN = Side(style="thin", color="000000")
_THICK = Side(style="thick", color="000000")


def _put(ws, row, column, value, *, bold=False, heading=False):
    cell = ws.cell(row, column, value)
    if isinstance(value, str):
        cell.data_type = "s"
        cell.number_format = "@"
    else:
        cell.number_format = "General"
    cell.font = Font(size=12 if row == 1 else 10 if row == 2 else 8,
                     bold=bold or heading, color="000000")
    # The source macro uses Excel's default alignment, transferred after measurement.
    cell.alignment = Alignment(vertical="bottom", wrap_text=True)
    cell.border = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)
    if heading:
        cell.fill = PatternFill("solid", fgColor="DCE6F1")
    return cell


def _phone(value: str) -> str:
    return re.sub(r"\D", "", re.sub(r"\.0+$", "", value.strip()))


def _preprocess(value: str, *, address=False, note=False) -> str:
    if not (address or note):
        return value
    value = re.sub(r"[\r\n]+", " ", value).strip()
    if address:
        return re.sub(r"(([로|길])\s*\d+(?:-\d+)?)(?:[,\s]+)",
                      r"\1\n", value).replace("  ", " ")
    lines, current = [], ""
    for token in filter(None, value.split(" ")):
        if current and len(current) + 1 + len(token) > 8:
            lines.append(current)
            current = token
        else:
            current = f"{current} {token}" if current else token
    if current:
        lines.append(current)
    return "\n".join(lines)


def _grouping_value(row, key: str, attribute: str) -> str:
    value = getattr(row, attribute)
    return row.values.get(key, "") if value is None else value


def _build_sheet(data: PreparedSubstituteData, options: SubstituteOptions):
    """Build values/styles independently of the Excel measurement boundary."""
    rows = data.selected_rows(options)
    groups = []
    start = 0
    for index in range(1, len(rows) + 1):
        previous_phone = _phone(_grouping_value(rows[index - 1], "mobile_phone", "grouping_phone"))
        if index < len(rows) and previous_phone and previous_phone == _phone(_grouping_value(rows[index], "mobile_phone", "grouping_phone")):
            continue
        members = rows[start:index]
        meal_count = (len(members) if data.meal is Meal.BREAKFAST else
                      sum(member.entitlement_count for member in members))
        groups.append(SheetGroup(start, index - 1, start + 3, index + 2,
                                 _grouping_value(members[0], "jurisdiction", "grouping_jurisdiction"),
                                 meal_count))
        start = index
    labels = {column.key: _LABELS.get(column.key, column.label) for column in data.columns}
    if options.fallbacks is not None:
        labels = {column.key: column.label for column in data.columns}
    elif data.calculation_manifest.get("column_mapping", {}).get("setting") is not None:
        labels.update(road_address="주소", note="비고")
    columns = [labels[key] for key in options.columns]
    sizes = tuple(range(1, max(5, max(group.meal_count for group in groups)) + 1))
    used_cols = max(len(columns) + 1, len(sizes) + 2)
    wb = Workbook()
    ws = wb.active
    ws.title = "배송대타"
    for col in range(1, used_cols + 1):
        _put(ws, 1, col, "", bold=True)
    _put(ws, 1, 1, f"식종: {data.meal.value} | 배송담당: {rows[0].driver_label} | 날짜: {data.day:%Y%m%d}", bold=True)
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=used_cols)
    for col in range(1, used_cols + 1):
        _put(ws, 2, col, ([*columns, "요약"] + [""] * used_cols)[col - 1], heading=True)
    for row_number, item in enumerate(rows, 3):
        for col in range(1, used_cols + 1):
            key = options.columns[col - 1] if col <= len(columns) else None
            value = item.values.get(key, "") if key else ""
            if key:
                value = _preprocess(value, address=key == "road_address", note=key == "note" or labels[key] == "비고2")
            _put(ws, row_number, col, value)
    summaries = {}
    for group in groups:
        summaries.setdefault(group.dong, Counter())[group.meal_count] += 1
        _put(ws, group.start_row, len(columns) + 1, group.meal_count)
        if group.meal_count >= 2:
            for col, key in enumerate((*options.columns, "__summary"), 1):
                if key != "name" and group.start_row < group.end_row:
                    ws.merge_cells(start_row=group.start_row, end_row=group.end_row,
                                   start_column=col, end_column=col)
        color = _FILLS.get(group.source_end - group.source_start + 1)
        for row in range(group.start_row, group.end_row + 1):
            for col in range(1, used_cols + 1):
                if color:
                    ws.cell(row, col).fill = PatternFill("solid", fgColor=color)
        if any(item.box for item in rows[group.source_start:group.source_end + 1]):
            for row in range(group.start_row, group.end_row + 1):
                for col in range(1, used_cols + 1):
                    cell = ws.cell(row, col)
                    # Preserve merged cells' interior edges as absent.
                    border = cell.border
                    cell.border = Border(left=_THICK if col == 1 else border.left,
                                         right=_THICK if col == used_cols else border.right,
                                         top=_THICK if row == group.start_row else border.top,
                                         bottom=_THICK if row == group.end_row else border.bottom)
    row_number = len(rows) + 3
    headers = ["", *(f"{size}식" for size in sizes), "식수 합계"]
    for col in range(1, used_cols + 1):
        _put(ws, row_number, col, (headers + [""] * used_cols)[col - 1], heading=True)
    total = Counter()
    for area, counts in summaries.items():
        total.update(counts)
        row_number += 1
        values = [area or "관할동 미지정", *(counts[size] or "" for size in sizes),
                  sum(size * counts[size] for size in sizes)]
        for col in range(1, used_cols + 1):
            _put(ws, row_number, col, (values + [""] * used_cols)[col - 1], bold=True)
    row_number += 1
    values = ["전체", *(total[size] or "" for size in sizes), sum(size * total[size] for size in sizes)]
    for col in range(1, used_cols + 1):
        _put(ws, row_number, col, (values + [""] * used_cols)[col - 1], heading=True)
    orientation = options.orientation
    if orientation == "auto":
        orientation = "landscape" if used_cols >= 8 else "portrait"
    ws.page_setup.orientation = orientation
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.print_options.horizontalCentered = True
    ws.page_margins = PageMargins(left=.3 / 2.54, right=.3 / 2.54,
                                 top=.3 / 2.54, bottom=.3 / 2.54, header=0, footer=0)
    ws.print_title_rows = "1:2"
    ws.print_area = f"A1:{get_column_letter(used_cols)}{row_number}"
    layout = SheetLayout(tuple(groups), len(rows) + 2, row_number, used_cols)
    return wb, columns, layout


def _apply_template_styles(workbook, layout, data, options, styles):
    ws = workbook.active
    for row in range(1, layout.used_rows + 1):
        role = ("title" if row == 1 else "header" if row == 2 else
                "body" if row <= layout.data_end_row else
                "summary_header" if row == layout.data_end_row + 1 else
                "total" if row == layout.used_rows else "summary")
        for col in range(1, layout.used_cols + 1):
            key = options.columns[col - 1] if col <= len(options.columns) else "__summary"
            source_col = STYLE_COLUMNS.index(key) if key in STYLE_COLUMNS else 6
            if row > layout.data_end_row:
                source_col = min(col - 1, 7)
            if row == 1:
                source_col = 0
            font, fill, border = styles[role][source_col]
            cell = ws.cell(row, col)
            cell.font, cell.fill, cell.border = copy(font), copy(fill), copy(border)
    rows = data.selected_rows(options)

    for group in layout.groups:
        fill = styles.get(f"household{group.source_end - group.source_start + 1}")
        box = any(item.box for item in rows[group.source_start:group.source_end + 1])
        for row in range(group.start_row, group.end_row + 1):
            for col in range(1, layout.used_cols + 1):
                cell = ws.cell(row, col)
                key = options.columns[col - 1] if col <= len(options.columns) else "__summary"
                source_col = STYLE_COLUMNS.index(key) if key in STYLE_COLUMNS else 6
                if fill:
                    cell.fill = copy(fill[source_col][1])
                box_border = styles["box"][source_col][2]
                if box:
                    border = copy(cell.border)
                    if col == 1:
                        border.left = copy(styles["box"][0][2].left)
                    if col == layout.used_cols:
                        border.right = copy(styles["box"][7][2].right)
                    if row == group.start_row:
                        border.top = copy(box_border.top)
                    if row == group.end_row:
                        border.bottom = copy(box_border.bottom)
                    cell.border = border


def build_substitute_workbook(data: PreparedSubstituteData, options: SubstituteOptions,
                              *, template_path: str | Path | None = None) -> Workbook:
    if template_path is None:
        from src.core.settings import get_settings
        template_path = get_settings().get_substitute_template_path()
    styles = load_substitute_styles(template_path)
    if substitute_styles_are_default(styles):
        styles = None
    workbook, columns, layout = _build_sheet(data, options)
    try:
        if styles is not None:
            _apply_template_styles(workbook, layout, data, options, styles)
        apply_excel_metrics(workbook, columns, layout, options,
                            **({"preserve_styles": True} if styles is not None else {}))
    except Exception:
        workbook.close()
        raise
    return workbook


def make_substitute_publication(data: PreparedSubstituteData, options: SubstituteOptions,
                                filepath: str | Path, *, output_password: str | None = None,
                                overwrite: bool = True,
                                template_path: str | Path | None = None) -> WorkbookPublicationResult:
    workbook = build_substitute_workbook(data, options, template_path=template_path)
    return publish_workbook(workbook, filepath, password=output_password, overwrite=overwrite)
