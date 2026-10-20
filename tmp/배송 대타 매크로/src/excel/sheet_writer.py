import pandas as pd

from src.config import BOX_SUFFIX, COL_MEAL, COL_NAME, COL_ORD, COL_SUM
from src.excel.colors import bgr, set_fill
from src.excel.groups import SheetLayout, build_sheet_layout
from src.excel_io import txt

FILL = {2: bgr("E6B8B7"), 3: bgr("B8CCE4"), 4: bgr("D8E4BC"), 5: bgr("CCC0DA")}
HDR_FILL = bgr("DCE6F1")
XL_THICK = 4
XL_OUTSIDE_EDGES = (7, 8, 9, 10)  # left, top, bottom, right


def _col_map(cols: list[str]) -> dict[str, int]:
    return {n: i + 1 for i, n in enumerate(cols)}


def _pad_row(values: list[object], width: int) -> list[object]:
    return [*values, *([""] * (width - len(values)))]


def box_group_ranges(
    source_df: pd.DataFrame,
    layout: SheetLayout,
) -> list[tuple[int, int]]:
    """Return Excel row ranges for households containing a BOX meal."""
    if COL_MEAL not in source_df.columns:
        return []

    box_rows = source_df[COL_MEAL].map(txt).str.endswith(BOX_SUFFIX).tolist()
    return [
        (group.start_row, group.end_row)
        for group in layout.groups
        if any(box_rows[group.source_start : group.source_end + 1])
    ]


def write_sheet(
    ws,
    df: pd.DataFrame,
    meal: str,
    ship: str,
    date_str: str,
    layout: SheetLayout | None = None,
    source_df: pd.DataFrame | None = None,
) -> SheetLayout:
    layout = layout or build_sheet_layout(df)
    source_df = source_df if source_df is not None else df
    if df.empty:
        return layout

    cols = [*df.columns.tolist(), COL_SUM]
    idx = _col_map(cols)
    used_cols = layout.used_cols

    # 1행: 전체 정보 병합 (폰트 12)
    ws.Cells(1, 1).Value = f"식종: {meal} | 배송담당: {ship} | 날짜: {date_str}"
    info = ws.Range(ws.Cells(1, 1), ws.Cells(1, used_cols))
    info.Merge()
    info.Font.Bold = True
    info.Font.Size = 12

    # 2행부터 헤더와 데이터를 연속으로 쓰고, 동별 식수표는 맨 아래에 둡니다.
    data = df.astype(object).where(pd.notna(df), "").values.tolist()
    values: list[list[object]] = [_pad_row(cols, used_cols)]
    values.extend(_pad_row([*row, ""], used_cols) for row in data)

    if layout.summary_header_row is not None:
        summary_header: list[object] = [""] * used_cols
        for col_idx, meal_size in enumerate(layout.meal_sizes, start=1):
            summary_header[col_idx] = f"{meal_size}식"
        summary_header[len(layout.meal_sizes) + 1] = "식수 합계"
        values.append(summary_header)

        for summary in [*layout.dong_meal_summaries, layout.overall_summary]:
            if summary is None:
                continue
            summary_row: list[object] = [""] * used_cols
            summary_row[0] = summary.label
            for col_idx, count in enumerate(summary.household_counts, start=1):
                summary_row[col_idx] = count or ""
            summary_row[len(layout.meal_sizes) + 1] = summary.total_meals
            values.append(summary_row)

    ws.Range(ws.Cells(2, 1), ws.Cells(layout.used_rows, used_cols)).Value = values

    ws.PageSetup.PrintTitleRows = "$1:$2"
    ws.Rows(1).Font.Bold = True

    # 배송순서별 병합 및 색상 적용
    if COL_ORD in df.columns or layout.summary_header_row is not None:
        merge_cols = [c for c in cols if c != COL_NAME]
        for group in layout.groups:
            n = group.meal_count
            ws.Cells(group.start_row, idx[COL_SUM]).Value = n

            if n >= 2:
                for name in merge_cols:
                    c = idx[name]
                    ws.Range(
                        ws.Cells(group.start_row, c), ws.Cells(group.end_row, c)
                    ).Merge()

            color = FILL.get(n)
            if color is not None:
                set_fill(
                    ws.Range(
                        ws.Cells(group.start_row, 1),
                        ws.Cells(group.end_row, used_cols),
                    ),
                    color,
                )

    # 배송 헤더, 요약 헤더, 요약 전체 행을 강조합니다.
    header_rows = [2]
    if layout.summary_header_row is not None:
        header_rows.append(layout.summary_header_row)
    if layout.overall_summary is not None:
        header_rows.append(layout.overall_summary.row)

    for row in header_rows:
        rng = ws.Range(ws.Cells(row, 1), ws.Cells(row, used_cols))
        set_fill(rng, HDR_FILL)
        rng.Font.Bold = True

    for summary in layout.dong_meal_summaries:
        ws.Range(
            ws.Cells(summary.row, 1), ws.Cells(summary.row, used_cols)
        ).Font.Bold = True

    ws.Range(
        ws.Cells(1, 1), ws.Cells(layout.used_rows, used_cols)
    ).Borders.LineStyle = 1

    # Emphasize the entire delivery row/group when it contains a BOX meal.
    if len(source_df) == len(df):
        for start_row, end_row in box_group_ranges(source_df, layout):
            box_group = ws.Range(
                ws.Cells(start_row, 1),
                ws.Cells(end_row, used_cols),
            )
            for edge in XL_OUTSIDE_EDGES:
                box_group.Borders(edge).Weight = XL_THICK
    return layout
