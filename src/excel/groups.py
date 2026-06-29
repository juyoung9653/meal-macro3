"""배송 그룹 분석 — 순서번호 기반 그룹화 및 페이지 나누기."""

import logging

import pandas as pd

from src.core.settings import get_settings
from src.excel._parsing import _to_str as txt

log = logging.getLogger(__name__)

_S = get_settings()
COL_ORD = _S.get_delivery_config("col_ord")


def get_groups(df: pd.DataFrame) -> list[tuple[int, int]]:
    """DataFrame에서 배송 그룹 인덱스 범위를 추출합니다."""
    if df.empty or COL_ORD not in df.columns:
        return [(i, i) for i in range(len(df))]
    return ord_groups(df[COL_ORD].map(txt).tolist())


def apply_page_breaks_by_groups(
    df: pd.DataFrame,
    groups: list[tuple[int, int]],
    mode: int,
    max_groups_limit: int,
    offset: int = 3,
) -> list[int]:
    """페이지 구분선 위치 계산. COM 조작 대신 break row 인덱스 리스트 반환.

    mode=1: 관할동 기준 분할 + 최대 그룹 수 제한
    mode=2: compute_page_chunks와 동일한 균등 분할 사용

    Returns:
        페이지 나누기가 필요한 행 인덱스 리스트 (1-based, 오름차순)
    """
    if not groups:
        return []

    log.info(
        "page_breaks: mode=%d max_limit=%d total_groups=%d",
        mode,
        max_groups_limit,
        len(groups),
    )

    # mode=2: compute_page_chunks와 동일한 균등 분할
    if mode == 2 and max_groups_limit > 0:
        chunks = compute_page_chunks(groups, mode, max_groups_limit, None)
        chunk_breaks = []
        cumul = 0
        for ch in chunks[:-1]:
            cumul += len(ch)
            s0, _ = groups[cumul]
            chunk_breaks.append(s0 + offset)
        chunk_breaks = _prevent_group_splits(chunk_breaks, groups, offset)
        log.info("  total_breaks=%d (even split)", len(chunk_breaks))
        return chunk_breaks

    breaks: list[int] = []
    curr_g = 0
    curr_dong = ""

    for s0, _ in groups:
        row_idx = s0 + offset
        need_break = False

        if mode == 1:
            dong = str(df.iloc[s0]["관할동"]) if "관할동" in df.columns else ""
            dong_changed = bool(curr_dong) and dong != curr_dong
            limit_exceeded = max_groups_limit > 0 and curr_g >= max_groups_limit

            if dong_changed or limit_exceeded:
                need_break = True
            curr_dong = dong

        if need_break:
            log.info("  break at group %d -> row %d (curr_g=%d)", s0, row_idx, curr_g)
            breaks.append(row_idx)
            curr_g = 0
        else:
            curr_g += 1

    breaks = _prevent_group_splits(breaks, groups, offset)
    log.info("  total_breaks=%d", len(breaks))
    return breaks


def ord_groups(vals: list[str]) -> list[tuple[int, int]]:
    """연속된 동일 값 그룹의 (start, end) 인덱스 반환."""
    out: list[tuple[int, int]] = []
    i, n = 0, len(vals)
    while i < n:
        j = i
        while j + 1 < n and vals[j + 1] == vals[i]:
            j += 1
        out.append((i, j))
        i = j + 1
    return out


def compute_page_chunks(
    groups: list[tuple[int, int]],
    mode: int,
    max_groups_limit: int,
    df: pd.DataFrame | None = None,
) -> list[list[tuple[int, int]]]:
    """그룹 리스트를 mode/limit 기준으로 페이지 청크로 분할 (Pure Python).

    mode=2: 전체 페이지 수를 최소화하면서 각 페이지에 균등 배분.
    mode=1: 관할동 기준 분할 + 최대 그룹 수 제한.

    Returns:
        chunks: [[(s0,e0), ...], [...]]  — 페이지별 그룹 리스트
    """
    if mode == 0 or not groups:
        return [groups[:]] if groups else []

    total = len(groups)

    if mode == 1 and df is not None:
        chunks: list[list[tuple[int, int]]] = []
        cur: list[tuple[int, int]] = []
        curr_g = 0
        curr_dong = ""
        for g in groups:
            s0, _ = g
            need_break = False
            dong = str(df.iloc[s0].get("관할동", ""))
            dong_changed = bool(curr_dong) and dong != curr_dong
            limit_exceeded = max_groups_limit > 0 and curr_g >= max_groups_limit
            if dong_changed or limit_exceeded:
                need_break = True
            if need_break:
                if cur:
                    chunks.append(cur)
                cur = []
                curr_g = 0
            cur.append(g)
            curr_g += 1
            curr_dong = dong
        if cur:
            chunks.append(cur)
        return chunks

    # mode=2: 균등 배분
    num_pages = max(1, (total + max_groups_limit - 1) // max_groups_limit)
    base = total // num_pages
    remainder = total % num_pages

    chunks = []
    idx = 0
    for p in range(num_pages):
        size = base + (1 if p < remainder else 0)
        chunks.append(groups[idx : idx + size])
        idx += size

    return chunks


def _prevent_group_splits(
    breaks: list[int], groups: list[tuple[int, int]], offset: int
) -> list[int]:
    """그룹 중간에 페이지가 잘리지 않도록 break 위치를 조정.

    break가 그룹 중간에 있으면 그룹 시작점으로 이동.
    """
    adjusted: list[int] = []
    for pb_row in breaks:
        new_pos = pb_row
        for s0, e0 in groups:
            start_row = s0 + offset
            end_row = e0 + offset
            if start_row < pb_row <= end_row:
                new_pos = start_row
                break
        if new_pos not in adjusted:
            adjusted.append(new_pos)
    return sorted(adjusted)
