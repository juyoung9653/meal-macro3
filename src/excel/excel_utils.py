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


def copy_style(src, dst):
    """셀 서식만 복사 (값/수식 제외).

    border, font, fill, number_format, alignment, protection 을
    src → dst 로 복사하되 dst.value 는 건드리지 않음.
    """
    if src.has_style:
        dst.font = copy(src.font)
        dst.border = copy(src.border)
        dst.fill = copy(src.fill)
        dst.number_format = copy(src.number_format)
        dst.protection = copy(src.protection)
        dst.alignment = copy(src.alignment)


def build_merged_anchor_map(ws) -> dict[tuple[int, int], tuple[int, int]]:
    """워크시트의 모든 MergedCell → 앵커셀 (row, col) 매핑.

    한 번 빌드하면 셀 값 쓰기 시 O(1) 조회.
    """
    anchor_map: dict[tuple[int, int], tuple[int, int]] = {}
    for mr in ws.merged_cells.ranges:
        anchor = (mr.min_row, mr.min_col)
        for r in range(mr.min_row, mr.max_row + 1):
            for c in range(mr.min_col, mr.max_col + 1):
                if r != anchor[0] or c != anchor[1]:
                    anchor_map[(r, c)] = anchor
    return anchor_map


def adjust_formula(formula: str, row_offset: int) -> str:
    """엑셀 formula 내 행 번호를 row_offset 만큼 증가"""
    if not formula or not str(formula).startswith("="):
        return formula
    import re as _re

    def _shift_row(m):
        return f"{m.group(1)}{int(m.group(2)) + row_offset}"

    return _re.sub(r"([A-Z]+)(\d+)", _shift_row, str(formula))
