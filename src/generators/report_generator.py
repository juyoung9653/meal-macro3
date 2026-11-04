"""일일식수 출력: 내용이 넘쳐도 템플릿의 크기·서식·인쇄 설정을 유지한다."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from copy import copy
from datetime import date
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.cell.rich_text import CellRichText, TextBlock
from openpyxl.cell.text import InlineFont

from ..core.diff_format import strip_diff
from ..core.settings import get_settings
from ..excel.excel_utils import write_literal_cell
from ..excel.publication import WorkbookPublicationResult, publish_workbook
from ..services.delivery_service import DeliveryReport

log = logging.getLogger(__name__)

def get_template_path() -> str:
    return get_settings().get_report_template_path()


TEMPLATE_SHEET = "aaa"


def _format_korean_date(date_str: str) -> str:
    if not date_str:
        return ""
    try:
        parsed = date(int(date_str[:4]), int(date_str[4:6]), int(date_str[6:8]))
    except (ValueError, IndexError):
        return date_str
    return f"{parsed.month}월 {parsed.day}일"


def export_multi(
    results: Sequence[DeliveryReport],
    filepath: str | Path,
    *,
    output_password: str | None = None,
) -> None:
    """여러 날짜 일일식수를 하나의 Excel 파일로 저장 (날짜별 시트)."""
    make_report_publication(
        results,
        filepath,
        get_template_path(),
        output_password=output_password,
    )


def make_report_publication(
    results: Sequence[DeliveryReport],
    filepath: str | Path,
    template_path: str | Path,
    *,
    output_password: str | None = None,
    overwrite: bool = True,
    preserve_template_styles: bool = False,
) -> WorkbookPublicationResult:
    """Publish one verified report workbook from an explicit template."""

    if not results:
        raise ValueError("내보낼 일일식수 결과가 없습니다.")

    out_file = Path(filepath)
    tpl_file = Path(template_path)
    if not tpl_file.is_file():
        raise FileNotFoundError(
            f"일일식수 템플릿을 찾을 수 없습니다: {tpl_file}"
        )

    if preserve_template_styles:
        from src.excel.delivery_templates import validate_delivery_template
        validate_delivery_template(tpl_file, "delivery_report")

    log.info("export: %d시트 → %s", len(results), out_file)
    tpl = load_workbook(tpl_file)
    out = Workbook()
    try:
        src = tpl[TEMPLATE_SHEET]
        default = out.active
        if default is not None:
            out.remove(default)

        for report in results:
            ws = out.create_sheet(title=f"{report.day:%Y%m%d}({report.meal})")
            _copy_sheet(src, ws)
            ws["A1"] = _format_korean_date(report.day.strftime("%Y%m%d"))
            _fill_data(ws, report, preserve_template_styles=preserve_template_styles)

            last_row = ws.max_row
            ws.print_area = f"A1:I{last_row}"

        publication = publish_workbook(out, out_file, password=output_password, overwrite=overwrite)
    finally:
        tpl.close()
        out.close()

    if not out_file.is_file():
        raise OSError(f"일일식수 파일이 생성되지 않았습니다: {out_file}")
    return publication


def export(
    report: DeliveryReport,
    filepath: str | Path,
    *,
    output_password: str | None = None,
) -> None:
    """export_multi 래퍼 (단일 시트)."""
    export_multi((report,), filepath, output_password=output_password)


# ═══════════════════════════════════════════════════
#  내부 함수
# ═══════════════════════════════════════════════════


def _copy_sheet(src, dst) -> None:
    from src.excel.template_sheet import copy_template_sheet
    copy_template_sheet(src, dst)


def _fill_data(ws, report: DeliveryReport, *, preserve_template_styles=False) -> None:
    """Keep template rows 4–8 and repeat row 8 for any extra driver rows.

    Row 9 supplies the total row. The legacy style flag is accepted for callers;
    all reports now preserve the template, including clipped text and print scale.
    """
    count = len(report.rows)
    styles = {r: [copy(ws.cell(r, c)._style) for c in range(1, 10)] for r in range(4, 10)}
    dimensions = {r: copy(ws.row_dimensions[r]) for r in range(4, 10)}
    total_row = 4 + count
    total_label = ws.cell(9, 1).value or "합계"
    ws["A2"] = f"*아동급식 {report.meal}*"
    if count > 5:
        ws.insert_rows(9, count - 5)
    elif count < 5:
        ws.delete_rows(total_row + 1, 5 - count)
    for r in range(4, max(9, total_row) + 1):
        if r in ws.row_dimensions:
            del ws.row_dimensions[r]

    def apply_row(row, prototype):
        dimension = copy(dimensions[prototype])
        dimension.index = row
        dimension.parent = ws
        ws.row_dimensions[row] = dimension
        for column, style in enumerate(styles[prototype], 1):
            ws.cell(row, column)._style = copy(style)

    for row_no, item in enumerate(report.rows, 4):
        apply_row(row_no, min(row_no, 8))
        write_literal_cell(ws.cell(row_no, 1), item.staff_label)
        for column, raw in enumerate(item.household_size_cells, 2):
            value = strip_diff(raw)
            ws.cell(row_no, column).value = int(value) if value != "" else ""
        ws.cell(row_no, 7).value = item.person_total
        write_literal_cell(ws.cell(row_no, 8), item.memo)
        write_literal_cell(ws.cell(row_no, 9), item.reference_areas)

    apply_row(total_row, 9)
    ws.cell(total_row, 1).value = total_label
    for column, count in enumerate(report.totals.household_counts, 2):
        ws.cell(total_row, column).value = count
    ws.cell(total_row, 7).value = report.totals.person_total
    ws.cell(total_row, 8).value = ""
    ws.cell(total_row, 9).value = ""


_FONT_NAME = "맑은 고딕"
_FONT_SIZE = 11


def _make_memo_rich_text(
    text: str,
    font_size: int | list[int] = _FONT_SIZE,
) -> str | CellRichText:
    """비고 텍스트: 줄바꿈 기준 홀수 줄 빨간색.

    font_size가 int면 모든 줄에 동일 적용, list면 줄별 크기 지정.
    """
    if "\n" not in text:
        return text
    lines = text.split("\n")
    sizes = (
        [font_size] * len(lines)
        if isinstance(font_size, int)
        else font_size + [_FONT_SIZE] * max(0, len(lines) - len(font_size))
    )
    blocks = []
    for i, line in enumerate(lines):
        sz = sizes[i] if i < len(sizes) else _FONT_SIZE
        rgb = "FFFF0000" if i % 2 == 0 else "FF000000"
        kw = {"rFont": _FONT_NAME, "sz": sz, "color": rgb, "b": True}
        t = line if i == len(lines) - 1 else line + "\n"
        blocks.append(TextBlock(InlineFont(**kw), t))
    return CellRichText(blocks)
