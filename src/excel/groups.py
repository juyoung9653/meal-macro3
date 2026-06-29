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
    ws,
    df: pd.DataFrame,
    groups: list[tuple[int, int]],
    mode: int,
    max_groups_limit: int,
    offset: int = 3,
) -> None:
    """페이지 구분선을 적용하며, 그룹이 페이지 경계에서 잘리지 않도록 조정합니다."""
    ws.ResetAllPageBreaks()
    if not groups:
        return

    log.info(
        "page_breaks: mode=%d max_limit=%d total_groups=%d",
        mode,
        max_groups_limit,
        len(groups),
    )

    curr_g = 0
    curr_dong = ""
    break_count = 0

    for s0, _ in groups:
        row_idx = s0 + offset
        need_break = False

        if mode == 1:
            dong = str(df.iloc[s0]["관할동"]) if "관할동" in df.columns else ""

            dong_changed = curr_dong and dong != curr_dong
            limit_exceeded = max_groups_limit > 0 and curr_g >= max_groups_limit

            if dong_changed or limit_exceeded:
                need_break = True

            curr_dong = dong

        elif mode == 2 and max_groups_limit > 0:
            if curr_g >= max_groups_limit:
                need_break = True

        if need_break:
            log.info("  break at group %d -> row %d (curr_g=%d)", s0, row_idx, curr_g)
            ws.HPageBreaks.Add(Before=ws.Rows(row_idx))
            curr_g = 0
            break_count += 1

        curr_g += 1

    log.info("  total_breaks_added=%d", break_count)

    _prevent_group_splits(ws, groups, offset)

    log.info(
        "  after _prevent: HPageBreaks.Count=%d",
        ws.HPageBreaks.Count if ws.HPageBreaks else 0,
    )


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
    """그룹 리스트를 mode/limit 기준으로 페이지 청크로 분할 (Pure Python, COM 불필요).

    mode=2: 전체 페이지 수를 최소화하면서 각 페이지에 균등 배분.
            예) 30그룹 limit=18 → 2페이지 × 15그룹 (18/12보다 15/15가 균등)
    mode=1: 관할동 기준 분할 + 최대 그룹 수 제한.

    Returns:
        chunks: [[(s0,e0), ...], [...]]  — 페이지별 그룹 리스트
    """
    if mode == 0 or not groups:
        return [groups[:]] if groups else []

    total = len(groups)

    if mode == 1 and df is not None:
        # 관할동 기준 분할 (기존 로직 유지)
        chunks: list[list[tuple[int, int]]] = []
        cur: list[tuple[int, int]] = []
        curr_g = 0
        curr_dong = ""
        for g in groups:
            s0, _ = g
            need_break = False
            dong = str(df.iloc[s0].get("관할동", ""))
            dong_changed = curr_dong and dong != curr_dong
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
    num_pages = max(
        1, (total + max_groups_limit - 1) // max_groups_limit
    )  # ceil division
    base = total // num_pages
    remainder = total % num_pages

    chunks = []
    idx = 0
    for p in range(num_pages):
        size = base + (1 if p < remainder else 0)
        chunks.append(groups[idx : idx + size])
        idx += size

    return chunks


def _prevent_group_splits(ws, groups: list[tuple[int, int]], offset: int) -> None:
    """그룹 중간에 페이지가 잘리는 경우 해당 그룹 시작점으로 페이지를 넘깁니다.

    Excel이 자동 추가한 페이지 나누기(Extent==2)는 무시하여
    AutoFit 전후에 생성되는 예상치 못한 구분선이 그룹을 자르지 않도록 방지합니다.
    """
    idx = 0
    while idx < len(ws.HPageBreaks):
        pb = ws.HPageBreaks(idx + 1)
        # 자동 페이지 나누기(xlPageBreakPartial=2)는 무시
        if getattr(pb, "Extent", 1) == 2:
            idx += 1
            continue
        pb_row = pb.Location.Row

        for s0, e0 in groups:
            start_row = s0 + offset
            end_row = e0 + offset

            if start_row < pb_row <= end_row:
                ws.HPageBreaks.Add(Before=ws.Rows(start_row))
                break

        idx += 1
