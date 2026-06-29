import logging
import math

import pandas as pd

from src.core.settings import get_settings
from src.excel.groups import (
    apply_page_breaks_by_groups,
    compute_page_chunks,
    get_groups,
)

log = logging.getLogger(__name__)

_S = get_settings()
COL_ADDR = _S.get_delivery_config("col_addr")
COL_PHONE = _S.get_delivery_config("col_phone")
COL_SUM = _S.get_delivery_config("col_sum")
FONT_BASE_SIZE = _S.get_delivery_config("font_base_size")

from .metrics import measure_page_height_pt, total_width_pt
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
    """
    used_rows = len(df) + 3
    used_cols = len(df.columns) + 1

    actual_orientation = apply_base_page_setup(
        ws, used_rows, used_cols, orientation_pref
    )
    groups = get_groups(df)

    _clean_address_newlines(ws, used_rows, [*df.columns.tolist(), COL_SUM], COL_ADDR)
    limit_h, _ = page_limits_pt(ws, actual_orientation)
    safe_h = limit_h - 5

    best = calc_optimal_font_size(
        ws, df, used_rows, used_cols, groups, mode, actual_orientation
    )

    # 폰트/컬럼폭 확정 후 페이지 나누기 (Rows AutoFit 없이 → break 안정적)
    apply_page_breaks_by_groups(ws, df, groups, mode, max_groups_limit)

    min_row_h = round(best * 1.5, 1)

    if mode != 0:
        chunks = compute_page_chunks(groups, mode, max_groups_limit, df)
        if chunks:
            for ci, pg in enumerate(chunks):
                rows_in_page = sum(e - s + 1 for s, e in pg)
                log.info("chunk %d: groups=%d rows=%d", ci, len(pg), rows_in_page)
            _distribute_extra_height_uniform(
                ws, chunks, used_rows, safe_h, min_row_h=min_row_h
            )

    if mode == 0:
        for r in range(3, used_rows + 1):
            if ws.Rows(r).RowHeight < min_row_h:
                ws.Rows(r).RowHeight = min_row_h


def calc_optimal_font_size(
    ws,
    df: pd.DataFrame,
    used_rows: int,
    used_cols: int,
    groups: list[tuple[int, int]],
    mode: int,
    actual_orientation: int,
) -> float:
    """컬럼 내용 길이 기반 폰트 크기 직접 계산 + 컬럼 폭 AutoFit.
    (행 높이는 건드리지 않음 — apply_page_formatting에서 page break 후 distribution)
    """
    limit_h, limit_w = page_limits_pt(ws, actual_orientation)
    safe_w, safe_h = limit_w - 5, limit_h - 5
    all_cols = [*df.columns.tolist(), COL_SUM]

    col_chars: dict[str, int] = {}
    for col in all_cols:
        hdr_len = len(str(col))
        if col in df.columns:
            data_len = df[col].astype(str).map(len).max()
        else:
            data_len = 0
        col_chars[col] = max(hdr_len, data_len)

    def estimate_width(pt: float) -> float:
        total = 0.0
        for chars in col_chars.values():
            total += math.ceil(chars * pt * 0.5 + 4)
        return total

    def estimate_height(pt: float, n_rows: int) -> float:
        row_h = round(pt * 1.5, 1)
        return (2 + n_rows + 1) * row_h

    n = len(df)
    best = FONT_BASE_SIZE

    for pt in [x * 0.5 for x in range(32, 9, -1)]:
        w = estimate_width(pt)
        h = estimate_height(pt, n)
        if w <= safe_w and h <= safe_h:
            best = pt
            break

    best = max(best, 10.0)

    log.info(
        "calc_opt_font: safe_h=%.1f safe_w=%.1f n=%d best=%.1f limit_h=%.1f limit_w=%.1f",
        safe_h,
        safe_w,
        n,
        best,
        limit_h,
        limit_w,
    )

    _set_fonts(ws, used_rows, all_cols, best - FONT_BASE_SIZE)
    _autofit_columns(ws, used_rows, used_cols)
    _widen_address_column_if_gap(ws, used_cols, all_cols, safe_w)
    _format_phone_column(ws, used_rows, all_cols, best)

    return best


def _set_fonts(ws, used_rows: int, all_cols: list[str], offset: int) -> None:
    ws.Rows(2).Font.Size = FONT_BASE_SIZE + 2 + offset
    ws.Rows(2).Font.Bold = True

    size = max(5, FONT_BASE_SIZE + offset)
    for i, _ in enumerate(all_cols, 1):
        ws.Range(ws.Cells(3, i), ws.Cells(used_rows, i)).Font.Size = size


def _autofit_columns(ws, used_rows: int, used_cols: int) -> None:
    """컬럼 폭만 AutoFit (Rows AutoFit은 page break 유지 위해 생략)."""
    if used_rows >= 3:
        data_area = ws.Range(ws.Cells(3, 1), ws.Cells(used_rows, used_cols))
        data_area.WrapText = True

    ws.Columns.AutoFit()

    # 정보행(1행): 고정 높이, 줄바꿈 해제
    ws.Rows(1).WrapText = False
    ws.Rows(1).RowHeight = 24

    # 헤더행(2행): 고정 높이, 줄바꿈 해제
    ws.Rows(2).WrapText = False
    if ws.Rows(2).RowHeight < 22:
        ws.Rows(2).RowHeight = 22


def _format_phone_column(
    ws, used_rows: int, all_cols: list[str], base_size: float
) -> None:
    """핸드폰 컬럼: 줄바꿈 해제 + 최소 11pt."""
    try:
        col_idx = all_cols.index(COL_PHONE) + 1
    except ValueError:
        return
    phone_range = ws.Range(ws.Cells(3, col_idx), ws.Cells(used_rows, col_idx))
    phone_range.WrapText = False
    phone_range.Font.Size = max(11.0, base_size)
    # 폰트 변경 후 컬럼 폭 재조정 (짤림 방지)
    ws.Columns(col_idx).AutoFit()


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
            ratio = (current_width + gap * 0.5) / current_width
            col.ColumnWidth *= ratio
    except (ValueError, ZeroDivisionError):
        for c in range(1, used_cols + 1):
            col = ws.Columns(c)
            col.ColumnWidth += (
                (col.Width / w) * gap * 0.5 * (col.ColumnWidth / col.Width)
            )


_PAGE_BOTTOM_MARGIN = 8  # 페이지 하단 여백 (pt, ≈3mm)


def _distribute_extra_height_uniform(
    ws,
    chunks: list[list[tuple[int, int]]],
    used_rows: int,
    safe_h: float,
    offset: int = 3,
    min_row_h: float = 0,
) -> None:
    if not chunks:
        return

    h_hdr = ws.Rows(1).RowHeight + ws.Rows(2).RowHeight
    footer_h = ws.Rows(used_rows).RowHeight if used_rows > 0 else 0

    for ci, pg in enumerate(chunks):
        groups_in_page = len(pg)
        if groups_in_page <= 0:
            continue

        # 마지막 청크는 합계행(footer) 공간 확보, 나머지는 헤더만 제외
        is_last = ci == len(chunks) - 1
        avail = safe_h - h_hdr - (footer_h if is_last else 0) - _PAGE_BOTTOM_MARGIN

        # 총 행 수 기준 1행당 높이 계산 (min_row_h를 floor로)
        total_rows = sum(e - s + 1 for s, e in pg)
        row_h = round(max(avail / total_rows, min_row_h), 2)

        if ci == 0:
            log.info(
                "page%d: groups=%d total_rows=%d avail=%.1f row_h=%.1f (min=%.1f)",
                ci,
                groups_in_page,
                total_rows,
                avail,
                row_h,
                min_row_h,
            )

        # 적용: 동일 행 높이로 그룹 병합 적용
        range_groups: dict[float, list[tuple[int, int]]] = {}
        for s0, e0 in pg:
            start_row = s0 + offset
            end_row = e0 + offset
            range_groups.setdefault(row_h, []).append((start_row, end_row))

        for h, ranges in range_groups.items():
            ranges.sort()
            merged = []
            for s, e in ranges:
                if merged and merged[-1][1] + 1 == s:
                    merged[-1] = (merged[-1][0], e)
                else:
                    merged.append((s, e))
            for s, e in merged:
                try:
                    ws.Range(ws.Rows(s), ws.Rows(e)).RowHeight = h
                except Exception:
                    for r in range(s, e + 1):
                        try:
                            ws.Rows(r).RowHeight = h
                        except Exception:
                            pass

        if ci == 0:
            log.info("  heights: row_h=%.1f (all rows equal)", row_h)


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
