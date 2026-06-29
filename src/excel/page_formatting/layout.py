import math

import pandas as pd

from src.core.settings import get_settings
from src.excel.groups import apply_page_breaks_by_groups, get_groups

_S = get_settings()
COL_ADDR = _S.get_delivery_config("col_addr")
COL_SUM = _S.get_delivery_config("col_sum")
FONT_BASE_SIZE = _S.get_delivery_config("font_base_size")

from .metrics import measure_page_height_pt, page_chunks_from_breaks, total_width_pt
from .setup import apply_base_page_setup, page_limits_pt


def apply_page_formatting(
    ws,
    df: pd.DataFrame,
    mode: int,
    max_groups_limit: int,
    orientation_pref: int = 0,
) -> None:
    """
    워크시트에 인쇄용 페이지 포맷을 적용합니다.

    :param ws: Excel COM Worksheet
    :param df: 출력 대상 DataFrame
    :param mode: 페이지 분할 모드(0/1/2)
    :param max_groups_limit: 페이지당 최대 그룹 수(>0일 때만 제한)
    :param orientation_pref: 0(자동) / 1(세로) / 2(가로)
    """
    used_rows = len(df) + 3
    used_cols = len(df.columns) + 1

    actual_orientation = apply_base_page_setup(
        ws, used_rows, used_cols, orientation_pref
    )
    groups = get_groups(df)

    apply_page_breaks_by_groups(ws, df, groups, mode, max_groups_limit)
    calc_optimal_font_size(
        ws, df, used_rows, used_cols, groups, mode, actual_orientation
    )

    _clean_address_newlines(ws, used_rows, [*df.columns.tolist(), COL_SUM], COL_ADDR)


def calc_optimal_font_size(
    ws,
    df: pd.DataFrame,
    used_rows: int,
    used_cols: int,
    groups: list[tuple[int, int]],
    mode: int,
    actual_orientation: int,
) -> float:
    """컬럼 내용 길이 기반 폰트 크기 직접 계산. AutoFit 0회.

    각 컬럼 최대 문자 수 × 폰트 계수로 예상 폭/높이를 계산하고,
    A4 가용 영역을 넘지 않는 최대 폰트 크기를 한 번에 찾는다.
    """
    limit_h, limit_w = page_limits_pt(ws, actual_orientation)
    safe_w, safe_h = limit_w - 10, limit_h - 10
    all_cols = [*df.columns.tolist(), COL_SUM]

    # 각 컬럼별 최대 문자 길이 (헤더명 vs 데이터 중 긴 쪽)
    col_chars: dict[str, int] = {}
    for col in all_cols:
        hdr_len = len(str(col))
        if col in df.columns:
            data_len = df[col].astype(str).map(len).max()
        else:
            data_len = 0
        col_chars[col] = max(hdr_len, data_len)

    def estimate_width(pt: float) -> float:
        """예상 총 폭 (pt). 계수 0.5 + 패딩 4pt."""
        total = 0.0
        for chars in col_chars.values():
            total += math.ceil(chars * pt * 0.5 + 4)
        return total

    def estimate_height(pt: float, n_rows: int) -> float:
        """예상 총 높이 (pt). 행 높이 = pt * 1.5 (맑은고딕 기준)."""
        row_h = round(pt * 1.5, 1)
        return (2 + n_rows + 1) * row_h  # 정보행1 + 헤더행1 + 데이터N + 합계행1

    n = len(df)
    best = FONT_BASE_SIZE

    # 0.5pt 단위로 max→min 탐색 (한 번에)
    for pt in [x * 0.5 for x in range(32, 9, -1)]:  # 16.0 → 5.0
        w = estimate_width(pt)
        h = estimate_height(pt, n)
        if w <= safe_w and h <= safe_h:
            best = pt
            break

    # 폰트 적용 + 1회 AutoFit (최종 보정만)
    _set_fonts(ws, used_rows, all_cols, best - FONT_BASE_SIZE)
    _apply_autofit(ws, used_rows, used_cols)

    _widen_address_column_if_gap(ws, used_cols, all_cols, safe_w)

    chunks = (
        page_chunks_from_breaks(ws, groups)
        if mode != 0
        else ([groups] if groups else [])
    )
    if chunks:
        _distribute_extra_height_uniform(ws, chunks, used_rows, safe_h)

    return best


def _set_fonts(ws, used_rows: int, all_cols: list[str], offset: int) -> None:
    ws.Rows(2).Font.Size = FONT_BASE_SIZE + 2 + offset
    ws.Rows(2).Font.Bold = True

    size = max(5, FONT_BASE_SIZE + offset)
    for i, _ in enumerate(all_cols, 1):
        ws.Range(ws.Cells(3, i), ws.Cells(used_rows, i)).Font.Size = size


def _apply_autofit(ws, used_rows: int, used_cols: int) -> None:
    # 데이터 영역(3행~)만 WrapText (헤더는 줄바꿈 안 됨)
    if used_rows >= 3:
        data_area = ws.Range(ws.Cells(3, 1), ws.Cells(used_rows, used_cols))
        data_area.WrapText = True

    ws.Columns.AutoFit()
    ws.Rows.AutoFit()

    # 정보행(1행): 고정 높이, 줄바꿈 해제
    ws.Rows(1).WrapText = False
    ws.Rows(1).RowHeight = 24

    # 헤더행(2행): 고정 높이, 줄바꿈 해제
    ws.Rows(2).WrapText = False
    if ws.Rows(2).RowHeight < 22:
        ws.Rows(2).RowHeight = 22


def _widen_address_column_if_gap(
    ws, used_cols: int, all_cols: list[str], safe_w: float
) -> None:
    w = total_width_pt(ws, used_cols)
    if not (0 < w < safe_w):
        return

    gap = safe_w - w
    try:
        addr_idx = all_cols.index(COL_ADDR) + 1
        col = ws.Columns(addr_idx)
        current_width = col.Width
        if current_width > 0:
            ratio = (current_width + gap * 0.9) / current_width
            col.ColumnWidth *= ratio
    except (ValueError, ZeroDivisionError):
        for c in range(1, used_cols + 1):
            col = ws.Columns(c)
            col.ColumnWidth += (
                (col.Width / w) * gap * 0.9 * (col.ColumnWidth / col.Width)
            )


def _distribute_extra_height_uniform(
    ws,
    chunks: list[list[tuple[int, int]]],
    used_rows: int,
    safe_h: float,
    offset: int = 3,
) -> None:
    if not chunks:
        return

    h_hdr = ws.Rows(1).RowHeight + ws.Rows(2).RowHeight
    footer_h = ws.Rows(used_rows).RowHeight if used_rows > 0 else 0
    available_body_h = safe_h - h_hdr - footer_h - 5

    max_groups_in_page = max(len(pg) for pg in chunks) if chunks else 0
    if max_groups_in_page <= 0:
        return

    target_group_height = available_body_h / max_groups_in_page

    # Collect ranges per height (no Union)
    range_groups: dict[float, list[tuple[int, int]]] = {}
    for pg in chunks:
        for s0, e0 in pg:
            start_row = s0 + offset
            end_row = e0 + offset
            num_rows = e0 - s0 + 1

            target_range = ws.Range(ws.Rows(start_row), ws.Rows(end_row))
            current_h = target_range.RowHeight
            if current_h is None:
                max_autofit_h = max(
                    ws.Rows(r).RowHeight for r in range(start_row, end_row + 1)
                )
            else:
                max_autofit_h = current_h

            calc_row_h = target_group_height / num_rows
            final_h = round(min(max(calc_row_h, max_autofit_h, 12), 100), 2)

            range_groups.setdefault(final_h, []).append((start_row, end_row))

    # Merge contiguous ranges and apply
    for h, ranges in range_groups.items():
        ranges.sort()
        merged = []
        for s, e in ranges:
            if merged and merged[-1][1] + 1 == s:
                merged[-1] = (merged[-1][0], e)
            else:
                merged.append((s, e))
        for s, e in merged:
            ws.Range(ws.Rows(s), ws.Rows(e)).RowHeight = h


def _clean_address_newlines(ws, used_rows, all_cols, col_name):
    """AutoFit 없이 줄바꿈만 콤마로 변환."""
    try:
        col_idx = all_cols.index(col_name) + 1
    except ValueError:
        return
    for r in range(3, used_rows):
        cell = ws.Cells(r, col_idx)
        text = str(cell.Value or "")
        if any(b in text for b in ("\n", "\r")):
            cell.Value = (
                text.replace("\r\n", ", ")
                .replace("\n", ", ")
                .replace("\r", ", ")
                .strip()
            )
