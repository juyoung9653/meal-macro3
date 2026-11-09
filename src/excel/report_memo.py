"""Emphasize generated memo headings while preserving the template font."""
import re
from openpyxl.cell.rich_text import CellRichText, TextBlock
from openpyxl.cell.text import InlineFont

_STATUS_HEADING = re.compile(r"^(?:(?:신규|배송|중지|일시중지)\s+\d{1,2}/\d{1,2}|복원$)")


def write_report_memo(cell, text, *, headings=()):
    text = text or ""
    lines = text.splitlines(keepends=True)
    # Additional headings can contain line breaks. Match the whole generated
    # heading before its recipient line, rather than guessing from row parity.
    emphasized = {index for index, line in enumerate(lines) if _STATUS_HEADING.match(line.strip())}
    for heading in headings:
        heading_lines = heading.splitlines()
        if not heading_lines:
            continue
        size = len(heading_lines)
        for index in range(len(lines) - size):
            if ([line.rstrip("\r\n") for line in lines[index:index + size]] == heading_lines
                    and lines[index + size].strip().startswith("(")):
                emphasized.update(range(index, index + size))
    if not emphasized:
        cell.value = text
        cell.data_type = "s"
        return
    font = cell.font
    red = InlineFont(rFont=font.name, sz=font.sz, b=font.b, i=font.i, u=font.u,
        strike=font.strike, vertAlign=font.vertAlign, charset=font.charset, family=font.family,
        scheme=font.scheme, outline=font.outline, shadow=font.shadow,
        condense=font.condense, extend=font.extend, color="FFFF0000")
    cell.value = CellRichText([TextBlock(red, line) if index in emphasized else line
                               for index, line in enumerate(lines)])
