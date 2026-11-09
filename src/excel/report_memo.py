"""Emphasize status headings without replacing the template's font."""
import re
from openpyxl.cell.rich_text import CellRichText, TextBlock
from openpyxl.cell.text import InlineFont

_STATUS_HEADING = re.compile(r"^(?:(?:신규|배송|중지|일시중지)\s+\d{1,2}/\d{1,2}|복원$)")


def write_report_memo(cell, text):
    text = text or ""
    lines = text.splitlines(keepends=True)
    if not any(_STATUS_HEADING.match(line.strip()) for line in lines):
        cell.value = text
        cell.data_type = "s"
        return
    font = cell.font
    red = InlineFont(rFont=font.name, sz=font.sz, b=font.b, i=font.i, u=font.u,
        strike=font.strike, vertAlign=font.vertAlign, charset=font.charset, family=font.family,
        scheme=font.scheme, outline=font.outline, shadow=font.shadow,
        condense=font.condense, extend=font.extend, color="FFFF0000")
    cell.value = CellRichText([TextBlock(red, line) if _STATUS_HEADING.match(line.strip()) else line
                               for line in lines])
