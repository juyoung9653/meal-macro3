"""Excel 조작 유틸리티 — 셀 복사, 병합셀 처리, 수식 조정."""

from copy import copy

from openpyxl.styles import Alignment

# ── 상수 ──
CENTER_ALIGN = Alignment(horizontal="center", vertical="center")


def copy_cell(src, dst):
    """셀 값 + 서식 복사"""
    dst.value = src.value
    if src.has_style:
        dst.font = copy(src.font)
        dst.border = copy(src.border)
        dst.fill = copy(src.fill)
        dst.number_format = copy(src.number_format)
        dst.protection = copy(src.protection)
        dst.alignment = copy(src.alignment)


def safe_set_cell_value(ws, row, column, value):
    """병합셀 고려 안전한 값 쓰기. MergedCell이면 앵커셀 찾아 씀."""
    from openpyxl.cell.cell import MergedCell

    cell = ws.cell(row=row, column=column)
    if isinstance(cell, MergedCell):
        for mr in ws.merged_cells.ranges:
            if mr.min_row <= row <= mr.max_row and mr.min_col <= column <= mr.max_col:
                ws.cell(row=mr.min_row, column=mr.min_col).value = value
                return
        return
    cell.value = value


def adjust_formula(formula: str, row_offset: int) -> str:
    """엑셀 formula 내 행 번호를 row_offset 만큼 증가"""
    if not formula or not str(formula).startswith("="):
        return formula
    import re as _re

    def _shift_row(m):
        return f"{m.group(1)}{int(m.group(2)) + row_offset}"

    return _re.sub(r"([A-Z]+)(\d+)", _shift_row, str(formula))
