import pandas as pd

from src.config import COL_ADDR, COL_SUM, FONT_BASE_SIZE
from src.excel.groups import (
    SheetGroup,
    SheetLayout,
    apply_page_breaks_by_groups,
    build_sheet_layout,
)

from .metrics import measure_page_height_pt, page_chunks_from_breaks, total_width_pt
from .setup import apply_base_page_setup, page_limits_pt

MIN_FONT_SIZE = 5


def apply_page_formatting(
    ws,
    df: pd.DataFrame,
    mode: int,
    max_groups_limit: int,
    orientation_pref: int = 0,
    sheet_layout: SheetLayout | None = None,
) -> None:
    """
    워크시트에 인쇄용 페이지 포맷을 적용합니다.

    :param ws: Excel COM Worksheet
    :param df: 출력 대상 DataFrame
    :param mode: 페이지 분할 모드(0/1/2)
    :param max_groups_limit: 페이지당 최대 그룹 수(>0일 때만 제한)
    :param orientation_pref: 0(자동) / 1(세로) / 2(가로)
    """
    sheet_layout = sheet_layout or build_sheet_layout(df)
    used_rows = sheet_layout.used_rows
    used_cols = sheet_layout.used_cols

    actual_orientation = apply_base_page_setup(
        ws, used_rows, used_cols, orientation_pref
    )
    groups = sheet_layout.groups

    apply_page_breaks_by_groups(ws, groups, mode, max_groups_limit)
    _optimize_font_and_layout(ws, df, used_rows, used_cols, groups, actual_orientation)

    apply_page_breaks_by_groups(ws, groups, mode, max_groups_limit)
    _optimize_font_and_layout(ws, df, used_rows, used_cols, groups, actual_orientation)

    _clean_address_redundant_breaks(
        ws, sheet_layout.data_end_row, [*df.columns.tolist(), COL_SUM], COL_ADDR
    )


def _optimize_font_and_layout(
    ws,
    df: pd.DataFrame,
    used_rows: int,
    used_cols: int,
    groups: tuple[SheetGroup, ...],
    actual_orientation: int,
) -> None:
    limit_h, limit_w = page_limits_pt(ws, actual_orientation)
    safe_w, safe_h = limit_w - 10, limit_h - 10
    all_cols = [*df.columns.tolist(), COL_SUM]

    offset = 0
    _set_fonts(ws, used_rows, used_cols, offset)
    _apply_autofit(ws, used_rows, used_cols)

    def chunks_for_fit() -> list[list[SheetGroup]]:
        # 수동 분할이 없어도 Excel의 자동 페이지 구분을 기준으로 계산합니다.
        return page_chunks_from_breaks(ws, groups)

    def fits() -> bool:
        w = total_width_pt(ws, used_cols)
        chunks = chunks_for_fit()

        if not chunks:
            return w <= safe_w

        max_page_h = 0.0
        for i, pg in enumerate(chunks):
            max_page_h = max(
                max_page_h,
                measure_page_height_pt(
                    ws, pg, used_rows, is_last_page=(i == len(chunks) - 1)
                ),
            )
        return w <= safe_w and max_page_h <= safe_h

    step = -1 if not fits() else 1
    current_sizes = _font_sizes(offset)
    for _ in range(18):
        next_offset = offset + step
        next_sizes = _font_sizes(next_offset)
        if step == -1 and next_sizes == current_sizes:
            # 최소 글꼴에서도 페이지에 다 들어가지 않으면 Excel의 자동 분할에 맡깁니다.
            break

        offset = next_offset
        current_sizes = next_sizes
        _set_fonts(ws, used_rows, used_cols, offset)
        _apply_autofit(ws, used_rows, used_cols)
        ok = fits()
        if step == -1 and ok:
            break
        if step == 1 and not ok:
            offset -= step
            _set_fonts(ws, used_rows, used_cols, offset)
            _apply_autofit(ws, used_rows, used_cols)
            break

    _widen_address_column_if_gap(ws, used_cols, all_cols, safe_w)

    chunks = chunks_for_fit()
    if chunks:
        _distribute_extra_height_uniform(ws, chunks, used_rows, safe_h)


def _font_sizes(offset: int) -> tuple[int, int]:
    """Excel이 허용하는 범위 안에서 (헤더, 본문) 글꼴 크기를 반환합니다."""
    return (
        max(MIN_FONT_SIZE, FONT_BASE_SIZE + 2 + offset),
        max(MIN_FONT_SIZE, FONT_BASE_SIZE + offset),
    )


def _set_fonts(ws, used_rows: int, used_cols: int, offset: int) -> None:
    header_size, body_size = _font_sizes(offset)
    ws.Rows(2).Font.Size = header_size
    ws.Rows(2).Font.Bold = True

    for i in range(1, used_cols + 1):
        ws.Range(ws.Cells(3, i), ws.Cells(used_rows, i)).Font.Size = body_size


def _apply_autofit(ws, used_rows: int, used_cols: int) -> None:
    rng = ws.Range(ws.Cells(1, 1), ws.Cells(used_rows, used_cols))
    rng.WrapText = True

    for c in range(1, used_cols + 1):
        ws.Columns(c).ColumnWidth = 50

    ws.Columns.AutoFit()
    ws.Rows.AutoFit()

    if ws.Rows(1).RowHeight < 20:
        ws.Rows(1).RowHeight = 24


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
        pts_to_unit = col.ColumnWidth / col.Width
        col.ColumnWidth += gap * pts_to_unit
    except (ValueError, ZeroDivisionError):
        for c in range(1, used_cols + 1):
            col = ws.Columns(c)
            col.ColumnWidth += (col.Width / w) * gap * (col.ColumnWidth / col.Width)


def _distribute_extra_height_uniform(
    ws,
    chunks: list[list[SheetGroup]],
    used_rows: int,
    safe_h: float,
) -> None:
    if not chunks:
        return

    h_hdr = ws.Rows(1).RowHeight + ws.Rows(2).RowHeight
    available_body_h = safe_h - h_hdr - 5

    if not any(chunks):
        return

    last_group_end = chunks[-1][-1].end_row
    summary_height = sum(
        ws.Rows(row).RowHeight for row in range(last_group_end + 1, used_rows + 1)
    )
    target_group_height = min(
        max(
            0,
            available_body_h - (summary_height if pg is chunks[-1] else 0),
        )
        / len(pg)
        for pg in chunks
        if pg
    )

    app = ws.Application  # ws에서 바로 Application 객체를 추출
    app.ScreenUpdating = False

    # 동일한 높이를 적용할 범위를 저장할 딕셔너리
    range_groups = {}

    for pg in chunks:
        for group in pg:
            start_row = group.start_row
            end_row = group.end_row
            num_rows = group.source_end - group.source_start + 1

            # 현재 범위 지정
            target_range = ws.Range(ws.Rows(start_row), ws.Rows(end_row))

            # 1. RowHeight 읽기 최적화
            current_h = target_range.RowHeight
            if current_h is None:
                # 행마다 높이가 다르면 수동 계산
                max_autofit_h = max(
                    ws.Rows(r).RowHeight for r in range(start_row, end_row + 1)
                )
            else:
                max_autofit_h = current_h

            calc_row_h = target_group_height / num_rows
            final_h = round(min(max(calc_row_h, max_autofit_h, 12), 100), 2)

            # 2. 동일한 높이끼리 범위 묶기 (Union)
            if final_h not in range_groups:
                range_groups[final_h] = target_range
            else:
                # ws.Application.Union을 사용하여 누적
                range_groups[final_h] = app.Union(range_groups[final_h], target_range)

    # 3. 한 번에 쓰기
    for h, combined_range in range_groups.items():
        combined_range.RowHeight = h

    app.ScreenUpdating = True


def _clean_address_redundant_breaks(
    ws, data_end_row: int, all_cols: list[str], col_name: str
) -> None:
    """
    1) 데이터 영역 폰트를 기존 열 너비 내에서 가능한 만큼 키운 뒤,
    2) 한 줄에 들어가는 값의 줄바꿈만 제거합니다(너비 초과 시 원복).

    :param ws: Excel COM Worksheet
    :param data_end_row: 마지막 데이터 행
    :param all_cols: 전체 컬럼명 리스트(요약 컬럼 포함)
    :param col_name: 대상 컬럼명
    """
    try:
        col_idx = all_cols.index(col_name) + 1
    except ValueError:
        return

    col = ws.Columns(col_idx)
    original_width = col.ColumnWidth

    data_rng = ws.Range(ws.Cells(3, col_idx), ws.Cells(data_end_row, col_idx))

    cur_size = data_rng.Font.Size or 10
    while True:
        next_size = cur_size + 0.5
        data_rng.Font.Size = next_size

        col.ColumnWidth = 50
        col.AutoFit()

        if col.ColumnWidth > original_width:
            data_rng.Font.Size = cur_size
            col.ColumnWidth = original_width
            break

        cur_size = next_size
        if cur_size >= 20:
            break

    for r in range(3, data_end_row + 1):
        cell = ws.Cells(r, col_idx)
        original_text = str(cell.Value or "")

        if not original_text or not any(b in original_text for b in ("\n", "\r")):
            continue

        cleaned = (
            original_text.replace("\r\n", ", ")
            .replace("\n", ", ")
            .replace("\r", ", ")
            .strip()
        )

        cell.Value = cleaned

        col.ColumnWidth = original_width + 50
        col.AutoFit()
        fitted_width = col.ColumnWidth

        if fitted_width > original_width:
            cell.Value = original_text

        col.ColumnWidth = original_width
