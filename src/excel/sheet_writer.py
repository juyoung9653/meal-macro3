"""배송대타 시트 쓰기 — openpyxl Worksheet에 데이터 채움."""

import pandas as pd
from openpyxl.styles import Border, Side

from src.core.settings import get_settings
from src.excel.colors import group_fill, header_fill
from src.excel.groups import get_groups

_S = get_settings()
COL_NAME = _S.get_delivery_config("col_name")
COL_ORD = _S.get_delivery_config("col_ord")
COL_SUM = _S.get_delivery_config("col_sum")

_THIN = Side(style="thin")
_THIN_BORDER = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)


def _col_map(cols: list[str]) -> dict[str, int]:
    return {n: i + 1 for i, n in enumerate(cols)}


def write_sheet(ws, df: pd.DataFrame, meal: str, ship: str, date_str: str) -> None:
    """Write delivery data to an openpyxl Worksheet. Fonts set by layout._apply_fonts."""
    if df.empty:
        return

    cols = [*df.columns.tolist(), COL_SUM]
    idx = _col_map(cols)
    c_cnt = len(cols)

    # 1행: 전체 정보 병합 (fonts delegated to layout._apply_fonts)
    ws.cell(
        row=1, column=1
    ).value = f"식종: {meal} | 배송담당: {ship} | 날짜: {date_str}"
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=c_cnt)

    # 2행부터 데이터
    data = df.astype(object).where(pd.notna(df), "").values.tolist()
    for r in data:
        r.append("")
    values = [cols, *data]
    r_cnt = len(values)

    for ri, row_vals in enumerate(values, 2):
        for ci, val in enumerate(row_vals, 1):
            ws.cell(row=ri, column=ci).value = val

    sum_row = r_cnt + 2

    # 그룹별 병합 및 색상 적용 (fill + border)
    if COL_ORD not in df.columns:
        ws.cell(row=sum_row, column=1).value = "총합계"
        ws.cell(row=sum_row, column=idx[COL_SUM]).value = ""
    else:
        merge_cols = [c for c in cols if c != COL_NAME]
        groups_ = get_groups(df)

        total = 0
        for s0, e0 in groups_:
            r1, r2 = s0 + 3, e0 + 3
            n = (e0 - s0) + 1
            total += n

            ws.cell(row=r1, column=idx[COL_SUM]).value = n

            if n >= 2:
                for name in merge_cols:
                    c = idx[name]
                    ws.merge_cells(
                        start_row=r1, start_column=c, end_row=r2, end_column=c
                    )

            gf = group_fill(n)
            if gf is not None:
                for r in range(r1, r2 + 1):
                    for c in range(1, c_cnt + 1):
                        ws.cell(row=r, column=c).fill = gf

        ws.cell(row=sum_row, column=1).value = "총합계"
        ws.cell(row=sum_row, column=idx[COL_SUM]).value = total

    # 2행(컬럼 헤더) 및 마지막 행(합계) 스타일 (fill + bold만, font는 layout._apply_fonts)
    hf = header_fill()
    for row_num in (2, sum_row):
        for c in range(1, c_cnt + 1):
            cell = ws.cell(row=row_num, column=c)
            cell.fill = hf

    # 테두리
    for r in range(1, sum_row + 1):
        for c in range(1, c_cnt + 1):
            ws.cell(row=r, column=c).border = _THIN_BORDER
