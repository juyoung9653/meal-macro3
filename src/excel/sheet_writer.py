import pandas as pd

from src.core.settings import get_settings
from src.excel.colors import bgr, set_fill
from src.excel.groups import get_groups

_S = get_settings()
COL_NAME = _S.get_delivery_config("col_name")
COL_ORD = _S.get_delivery_config("col_ord")
COL_SUM = _S.get_delivery_config("col_sum")

FILL = {2: bgr("E6B8B7"), 3: bgr("B8CCE4"), 4: bgr("D8E4BC"), 5: bgr("CCC0DA")}
HDR_FILL = bgr("DCE6F1")


def _col_map(cols: list[str]) -> dict[str, int]:
    return {n: i + 1 for i, n in enumerate(cols)}


def write_sheet(ws, df: pd.DataFrame, meal: str, ship: str, date_str: str) -> None:
    if df.empty:
        return

    cols = [*df.columns.tolist(), COL_SUM]
    idx = _col_map(cols)
    c_cnt = len(cols)

    # 1행: 전체 정보 병합 (폰트 12)
    ws.Cells(1, 1).Value = f"식종: {meal} | 배송담당: {ship} | 날짜: {date_str}"
    info = ws.Range(ws.Cells(1, 1), ws.Cells(1, c_cnt))
    info.Merge()
    info.Font.Bold = True
    info.Font.Size = 12

    # 2행부터 들어갈 데이터 구성
    data = df.astype(object).where(pd.notna(df), "").values.tolist()
    for r in data:
        r.append("")
    values = [cols, *data]

    r_cnt = len(values)
    ws.Range(ws.Cells(2, 1), ws.Cells(r_cnt + 1, c_cnt)).Value = values

    ws.PageSetup.PrintTitleRows = "$1:$2"
    ws.Rows(1).Font.Bold = True

    sum_row = r_cnt + 2

    # 그룹별 병합 및 색상 적용
    if COL_ORD not in df.columns:
        ws.Cells(sum_row, 1).Value = "총합계"
        ws.Cells(sum_row, idx[COL_SUM]).Value = ""
    else:
        merge_cols = [c for c in cols if c != COL_NAME]
        groups = get_groups(df)

        total = 0
        for s0, e0 in groups:
            r1, r2 = s0 + 3, e0 + 3
            n = (e0 - s0) + 1
            total += n

            ws.Cells(r1, idx[COL_SUM]).Value = n

            if n >= 2:
                for name in merge_cols:
                    c = idx[name]
                    ws.Range(ws.Cells(r1, c), ws.Cells(r2, c)).Merge()

            color = FILL.get(n)
            if color is not None:
                set_fill(ws.Range(ws.Cells(r1, 1), ws.Cells(r2, c_cnt)), color)

        ws.Cells(sum_row, 1).Value = "총합계"
        ws.Cells(sum_row, idx[COL_SUM]).Value = total

    # 2행(컬럼 헤더) 및 마지막 행(합계) 스타일 지정
    head = ws.Range(ws.Cells(2, 1), ws.Cells(2, c_cnt))
    tail = ws.Range(ws.Cells(sum_row, 1), ws.Cells(sum_row, c_cnt))
    for rng in (head, tail):
        set_fill(rng, HDR_FILL)
        rng.Font.Bold = True

    ws.Range(ws.Cells(1, 1), ws.Cells(sum_row, c_cnt)).Borders.LineStyle = 1
