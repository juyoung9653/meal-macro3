import pandas as pd
import win32com.client as win32

from src.config import COL_NAME, COL_ORD, COL_SUM
from src.excel_io import ord_groups, txt


def bgr(hex_rgb: str) -> int:
    hx = hex_rgb.strip().lstrip("#")
    r, g, b = int(hx[0:2], 16), int(hx[2:4], 16), int(hx[4:6], 16)
    return (b << 16) | (g << 8) | r


FILL = {2: bgr("E6B8B7"), 3: bgr("B8CCE4"), 4: bgr("D8E4BC"), 5: bgr("CCC0DA")}
HDR_FILL = bgr("DCE6F1")


def write_sheet(ws, df: pd.DataFrame, meal: str, ship: str, date_str: str) -> None:
    if df.empty:
        return

    ws.Cells(1, 1).Value = f"식종: {meal} | 배송담당: {ship} | 날짜: {date_str}"

    # 정보행 스타일 설정
    info_range = ws.Range(ws.Cells(1, 1), ws.Cells(1, 3))
    info_range.Font.Bold = True
    info_range.Font.Size = 12

    cols = [*df.columns.tolist(), COL_SUM]
    idx = {name: i + 1 for i, name in enumerate(cols)}

    merge_range = ws.Range(ws.Cells(1, 1), ws.Cells(1, len(cols)))
    merge_range.Merge()

    data = df.astype(object).where(pd.notna(df), "").values.tolist()
    for r in data:
        r.append("")  # 요약 칸 비워두기

    values = [cols] + data
    r_cnt, c_cnt = len(values), len(cols)

    # 데이터 쓰기: 2행부터 시작 (2행: 헤더, 3행~: 데이터)
    area = ws.Range(ws.Cells(2, 1), ws.Cells(r_cnt + 1, c_cnt))
    area.Value = values

    ws.PageSetup.PrintTitleRows = "$1:$2"
    ws.Rows(1).Font.Bold = True

    # 총합계 행 위치 계산: 1행(정보) + r_cnt(헤더+데이터) + 1
    sum_row = r_cnt + 2

    if COL_ORD not in df.columns:
        ws.Cells(sum_row, 1).Value = "총합계"
        ws.Cells(sum_row, idx[COL_SUM]).Value = ""
    else:
        merge_cols = [c for c in cols if c != COL_NAME]
        groups = ord_groups(df[COL_ORD].map(txt).tolist())

        total_sum = 0
        for s0, e0 in groups:
            # 데이터 시작이 3행이므로 s0(0-based) + 3을 해야 함
            r1, r2 = s0 + 3, e0 + 3
            nrows = (e0 - s0) + 1
            total_sum += nrows

            ws.Cells(r1, idx[COL_SUM]).Value = nrows

            if nrows >= 2:
                for name in merge_cols:
                    c = idx[name]
                    ws.Range(ws.Cells(r1, c), ws.Cells(r2, c)).Merge()

            color = FILL.get(nrows)
            if color is not None:
                g_area = ws.Range(ws.Cells(r1, 1), ws.Cells(r2, c_cnt))
                g_area.Interior.Pattern = 1
                g_area.Interior.Color = color

        ws.Cells(sum_row, 1).Value = "총합계"
        ws.Cells(sum_row, idx[COL_SUM]).Value = total_sum

    # 스타일 적용 (헤더/총합계)
    head = ws.Range(ws.Cells(2, 1), ws.Cells(2, c_cnt))  # 1행은 정보행이므로 헤더는 2행
    tail = ws.Range(ws.Cells(sum_row, 1), ws.Cells(sum_row, c_cnt))
    for a in (head, tail):
        a.Interior.Pattern = 1
        a.Interior.Color = HDR_FILL
        a.Font.Bold = True

    ws.Columns.AutoFit()
    ws.Rows.AutoFit()

    # 전체 테두리 (1행부터 합계행까지)
    tbl = ws.Range(ws.Cells(1, 1), ws.Cells(sum_row, c_cnt))
    tbl.Borders.LineStyle = 1


def get_max_rows_per_page(df: pd.DataFrame, mode: int, max_rows: int) -> int:
    """
    설정된 모드에 따라 한 페이지에 들어갈 '최대 데이터 행 수'를 계산합니다.
    """
    if mode == 1:  # 동 기준
        # 각 동별 행 개수 중 가장 큰 값을 찾습니다.
        return min(max_rows, df.groupby("관할동").size().max())
    elif mode == 2:  # 행 기준
        return max_rows
    return len(df)


def maximize_font_size(
    ws, rows_to_fit: int, cols_count: int, max_pt: int = 16, min_pt: int = 7
):
    """
    AutoFit을 적용하면서도 가로/세로 한계치를 넘지 않는 최대 폰트 크기를 찾습니다.
    """
    ps = ws.PageSetup
    # A4 가용 범위 (포인트 단위, 여백 제외)
    # 세로 방향(Portrait)일 때 가로 약 530, 가로 방향(Landscape)일 때 약 780
    is_landscape = ps.Orientation == 2
    LIMIT_WIDTH = 780 if is_landscape else 530
    LIMIT_HEIGHT = 530 if is_landscape else 760

    area = ws.Range(ws.Cells(1, 1), ws.Cells(rows_to_fit + 2, cols_count))

    final_pt = min_pt
    for pt in range(max_pt, min_pt - 1, -1):
        area.Font.Size = pt

        # [사용자님의 필수 요구사항] 모든 열과 행 자동 맞춤
        ws.Columns.AutoFit()
        ws.Rows.AutoFit()

        # 1. 현재 영역의 가로 너비 합계 계산
        current_width = 0
        for c in range(1, cols_count + 1):
            current_width += ws.Columns(c).Width

        # 2. 현재 영역의 세로 높이 합계 계산
        current_height = 0
        for r in range(1, rows_to_fit + 3):
            current_height += ws.Rows(r).RowHeight

        # 가로와 세로 중 하나라도 한계를 넘으면 배율이 100% 미만으로 깎이기 시작함
        if current_width <= LIMIT_WIDTH and current_height <= LIMIT_HEIGHT:
            final_pt = pt
            break

        # 만약 끝까지 갔는데도 한계를 넘는다면, 그나마 제일 나은 작은 폰트 적용
        final_pt = pt

    # 최종 적용
    ws.Cells.Font.Size = final_pt
    ws.Columns.AutoFit()
    ws.Rows.AutoFit()


def apply_page_breaks(ws, df: pd.DataFrame, mode: int, max_rows: int = 25):
    ws.ResetAllPageBreaks()
    if mode == 0 or COL_ORD not in df.columns:
        return

    # 배송순서별 그룹 범위 계산 (0-based index)
    groups = ord_groups(df[COL_ORD].map(txt).tolist())

    current_page_rows = 0
    current_dong = ""

    for s0, e0 in groups:
        group_size = (e0 - s0) + 1
        dong = str(df.iloc[s0]["관할동"])

        # 페이지 분할 조건:
        # 1. (모드 1) 관할동이 바뀌었을 때
        # 2. (공통) 현재 페이지에 이 그룹을 더하면 max_rows를 초과할 때
        should_break = False
        if mode == 1 and current_dong != "" and dong != current_dong:
            should_break = True
        elif current_page_rows + group_size > max_rows and current_page_rows > 0:
            should_break = True

        if should_break:
            ws.HPageBreaks.Add(Before=ws.Rows(s0 + 3))  # 데이터 시작이 3행이므로 s0+3
            current_page_rows = group_size
        else:
            current_page_rows += group_size

        current_dong = dong


def apply_page_formatting(
    ws, df: pd.DataFrame, mode: int, max_rows: int, orientation_pref: int = 0
):
    ps = ws.PageSetup
    # 인쇄 범위 수정: 정보(1) + 헤더(1) + 데이터(N) + 합계행(1) = len(df) + 3
    # 열 범위 수정: 기존 열 + 요약 열(1) = len(df.columns) + 1
    used_rows = len(df) + 3
    used_cols = len(df.columns) + 1

    ws.ResetAllPageBreaks()

    ps.PrintArea = ws.Range(ws.Cells(1, 1), ws.Cells(used_rows, used_cols)).Address
    ps.PaperSize = 9  # xlPaperA4
    ps.Zoom = False
    ps.FitToPagesWide = 1

    ps.LeftMargin = 15
    ps.RightMargin = 15
    ps.TopMargin = 20
    ps.BottomMargin = 20
    ps.CenterHorizontally = True
    ps.PrintTitleRows = "$1:$2"

    if mode == 0:
        ps.FitToPagesTall = 1
        max_rows_for_font = len(df)
    else:
        ps.FitToPagesTall = False
        # 그룹을 유지하면서 한 페이지에 들어가는 '실제 최대 행수' 계산 (폰트 최적화용)
        groups = ord_groups(df[COL_ORD].map(txt).tolist())
        max_found_rows = 0
        current_page_rows = 0
        current_dong = ""

        for s0, e0 in groups:
            group_size = (e0 - s0) + 1
            dong = str(df.iloc[s0]["관할동"])
            if (mode == 1 and current_dong != "" and dong != current_dong) or (
                current_page_rows + group_size > max_rows and current_page_rows > 0
            ):
                max_found_rows = max(max_found_rows, current_page_rows)
                current_page_rows = group_size
            else:
                current_page_rows += group_size
            current_dong = dong
        max_rows_for_font = max(max_found_rows, current_page_rows)

    if orientation_pref == 0:
        ps.Orientation = 2 if used_cols >= 8 else 1
    else:
        ps.Orientation = orientation_pref

    maximize_font_size(ws, max_rows_for_font, used_cols)


def show_excel(
    df: pd.DataFrame,
    title: str,
    meal: str,
    ship: str,
    date_str: str,
    ori: int,
    split: int,
    max_rows: int,
) -> None:
    excel = win32.DispatchEx("Excel.Application")
    excel.Visible = True
    excel.DisplayAlerts = False

    wb = excel.Workbooks.Add()
    ws = wb.Worksheets(1)
    ws.Name = title[:31]

    # 1. 데이터 쓰기 및 스타일 적용
    write_sheet(ws, df, meal, ship, date_str)

    max_rows_per_page = get_max_rows_per_page(df, split, max_rows)

    apply_page_formatting(
        ws, df, split, max_rows=max_rows_per_page, orientation_pref=ori
    )

    apply_page_breaks(ws, df, split, max_rows_per_page)
