import pandas as pd

from src.core.settings import get_settings
from src.excel._parsing import _to_str as txt

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
    if mode == 0 or not groups:
        return

    curr_g = 0
    curr_dong = ""

    for s0, _ in groups:
        row_idx = s0 + offset
        need_break = False

        if mode == 1:
            dong = str(df.iloc[s0]["관할동"]) if "관할동" in df.columns else ""

            # 조건 1: 관할동이 변경되었을 때
            dong_changed = curr_dong and dong != curr_dong
            # 조건 2: 그룹 제한 개수를 초과했을 때 (max_groups_limit > 0 일 때만 작동)
            limit_exceeded = max_groups_limit > 0 and curr_g >= max_groups_limit

            if dong_changed or limit_exceeded:
                need_break = True

            curr_dong = dong

        elif mode == 2 and max_groups_limit > 0:
            if curr_g >= max_groups_limit:
                need_break = True

        if need_break:
            ws.HPageBreaks.Add(Before=ws.Rows(row_idx))
            curr_g = 0  # 구분선 삽입 후 그룹 카운트 초기화

        curr_g += 1

    _prevent_group_splits(ws, groups, offset)


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


def _prevent_group_splits(ws, groups: list[tuple[int, int]], offset: int) -> None:
    """그룹 중간에 페이지가 잘리는 경우 해당 그룹 시작점으로 페이지를 넘깁니다."""
    # HPageBreaks는 1-based index 접근을 지원하는 COM 컬렉션이라 순회가 약간 특이합니다.
    idx = 0
    while idx < len(ws.HPageBreaks):
        pb_row = ws.HPageBreaks(idx + 1).Location.Row

        for s0, e0 in groups:
            start_row = s0 + offset
            end_row = e0 + offset

            if start_row < pb_row <= end_row:
                ws.HPageBreaks.Add(Before=ws.Rows(start_row))
                break

        idx += 1
