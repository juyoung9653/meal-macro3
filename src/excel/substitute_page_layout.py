"""Legacy substitute presentation with readable, consistent native Excel sizing.

Automatic pagination handles route length instead of shrinking every full page.
Address cleanup preserves the common body font; narrow pages wrap long text.
No source files from tmp are imported. All measurements use an unsaved worksheet.
"""
from __future__ import annotations

from dataclasses import dataclass

COL_ADDR = "신주소"
COL_SUM = "요약"
FONT_BASE_SIZE = 8
PAGE_H, PAGE_W = 842, 595


@dataclass(frozen=True)
class SheetGroup:
    source_start: int
    source_end: int
    start_row: int
    end_row: int
    dong: str
    meal_count: int


@dataclass(frozen=True)
class SheetLayout:
    groups: tuple[SheetGroup, ...]
    data_end_row: int
    used_rows: int
    used_cols: int




def apply_base_page_setup(
    ws, used_rows: int, used_cols: int, orientation_pref: int
) -> int:
    """
    워크시트 기본 페이지 설정을 적용하고 실제 방향값을 반환합니다.

    :param ws: Excel COM Worksheet
    :param used_rows: 사용 행 수
    :param used_cols: 사용 열 수
    :param orientation_pref: 0(자동) / 1(세로) / 2(가로)
    :return: 실제 적용된 방향(1/2)
    """
    ps, excel = ws.PageSetup, ws.Application

    ps.PrintArea = ws.Range(ws.Cells(1, 1), ws.Cells(used_rows, used_cols)).Address
    ps.PaperSize = 9  # A4
    ps.Zoom = False
    ps.FitToPagesWide = 1
    ps.FitToPagesTall = False
    ps.PrintTitleRows = "$1:$2"

    m = excel.CentimetersToPoints(0.3)
    ps.LeftMargin = m
    ps.RightMargin = m
    ps.TopMargin = m
    ps.BottomMargin = m
    ps.HeaderMargin = 0
    ps.FooterMargin = 0
    ps.CenterHorizontally = True

    actual = orientation_pref if orientation_pref != 0 else (2 if used_cols >= 8 else 1)
    ps.Orientation = actual
    return actual


def page_limits_pt(ws, actual_orientation: int) -> tuple[float, float]:
    """
    페이지 사용 가능 영역(포인트)을 반환합니다.

    :param ws: Excel COM Worksheet
    :param actual_orientation: 1(세로) / 2(가로)
    :return: (limit_h, limit_w)
    """
    ps = ws.PageSetup
    is_landscape = actual_orientation == 2

    h_buffer = 30 if is_landscape else 20
    w_buffer = 20

    limit_h = (PAGE_W if is_landscape else PAGE_H) - (
        ps.TopMargin + ps.BottomMargin + h_buffer
    )
    limit_w = (PAGE_H if is_landscape else PAGE_W) - (
        ps.LeftMargin + ps.RightMargin + w_buffer
    )
    return limit_h, limit_w



def total_width_pt(ws, used_cols: int) -> float:
    """
    현재 시트의 1..used_cols 총 폭(포인트)을 반환합니다.

    :param ws: Excel COM Worksheet
    :param used_cols: 사용 열 수
    """
    return sum(ws.Columns(c).Width for c in range(1, used_cols + 1))


def page_chunks_from_breaks(
    ws, groups: tuple[SheetGroup, ...]
) -> list[list[SheetGroup]]:
    """
    ws.HPageBreaks 기준으로 '페이지별 그룹 리스트'로 분할합니다.

    :param ws: Excel COM Worksheet
    :param groups: 실제 Excel 행 위치가 계산된 배송 그룹 목록
    """
    if not groups:
        return []

    breaks = sorted(pb.Location.Row for pb in ws.HPageBreaks)

    chunks: list[list[SheetGroup]] = []
    cur: list[SheetGroup] = []
    bi = 0

    for group in groups:
        start_row = group.start_row

        while bi < len(breaks) and start_row >= breaks[bi]:
            if cur:
                chunks.append(cur)
            cur = []
            bi += 1

        cur.append(group)

    if cur:
        chunks.append(cur)

    return chunks


def apply_page_breaks_by_groups(
    ws,
    groups: tuple[SheetGroup, ...],
    mode: int,
    max_groups_limit: int,
) -> None:
    """페이지 구분선을 적용하며, 한 가구가 페이지 경계에서 잘리지 않게 합니다."""
    ws.ResetAllPageBreaks()
    if mode == 0 or not groups:
        return

    curr_g = 0
    curr_dong = ""
    page_start = groups[0].start_row
    breaks = sorted(pb.Location.Row for pb in ws.HPageBreaks)

    for group in groups:
        # Excel may have started a page before the count limit was reached.
        # Count households on that physical page, not from the route's origin.
        preceding = [row for row in breaks if page_start < row <= group.start_row]
        if preceding:
            page_start = preceding[-1]
            curr_g = 0
        split_inside = any(group.start_row < row <= group.end_row for row in breaks)
        dong_changed = mode == 1 and curr_dong and group.dong != curr_dong
        limit_reached = max_groups_limit > 0 and curr_g >= max_groups_limit
        if (split_inside or dong_changed or limit_reached) and group.start_row > page_start:
            ws.HPageBreaks.Add(Before=ws.Rows(group.start_row))
            page_start = group.start_row
            curr_g = 0
            # A manual break changes all subsequent automatic boundaries.
            breaks = sorted(pb.Location.Row for pb in ws.HPageBreaks)
        curr_dong = group.dong
        curr_g += 1


MIN_FONT_SIZE = 5


def apply_page_formatting(
    ws,
    columns: list[str],
    mode: int,
    max_groups_limit: int,
    sheet_layout: SheetLayout,
    orientation_pref: int = 0,
    preserve_styles: bool = False,
) -> None:
    """
    워크시트에 인쇄용 페이지 포맷을 적용합니다.

    :param ws: Excel COM Worksheet
    :param columns: 출력 칼럼 이름
    :param mode: 페이지 분할 모드(0/1/2)
    :param max_groups_limit: 페이지당 최대 그룹 수(>0일 때만 제한)
    :param orientation_pref: 0(자동) / 1(세로) / 2(가로)
    """
    used_rows = sheet_layout.used_rows
    used_cols = sheet_layout.used_cols

    actual_orientation = apply_base_page_setup(
        ws, used_rows, used_cols, orientation_pref
    )
    groups = sheet_layout.groups

    apply_page_breaks_by_groups(ws, groups, mode, max_groups_limit)
    _optimize_font_and_layout(ws, columns, used_rows, used_cols, groups, actual_orientation, preserve_styles)

    apply_page_breaks_by_groups(ws, groups, mode, max_groups_limit)
    _optimize_font_and_layout(ws, columns, used_rows, used_cols, groups, actual_orientation, preserve_styles)

    _clean_address_redundant_breaks(
        ws, sheet_layout.data_end_row, [*columns, COL_SUM], COL_ADDR
    )
    # Final row heights can move native automatic breaks after optimization.
    apply_page_breaks_by_groups(ws, groups, mode, max_groups_limit)


def _optimize_font_and_layout(
    ws,
    columns: list[str],
    used_rows: int,
    used_cols: int,
    groups: tuple[SheetGroup, ...],
    actual_orientation: int,
    preserve_styles: bool = False,
) -> None:
    limit_h, limit_w = page_limits_pt(ws, actual_orientation)
    safe_w, safe_h = limit_w - 10, limit_h - 10
    all_cols = [*columns, COL_SUM]

    offset = 0
    if not preserve_styles:
        _set_fonts(ws, used_rows, used_cols, offset)
    _apply_autofit(ws, used_rows, used_cols)

    def fits() -> bool:
        if total_width_pt(ws, used_cols) > safe_w:
            return False
        # FitToPagesTall=False permits more pages. Comparing a naturally full
        # automatic page with a smaller safety budget shrinks the whole route
        # repeatedly without solving pagination. Only indivisible groups need
        # a height guard; a summary can occupy its own page.
        header_height = ws.Rows(1).RowHeight + ws.Rows(2).RowHeight
        return all(header_height + sum(ws.Rows(row).RowHeight
                   for row in range(group.start_row, group.end_row + 1)) <= safe_h
                   for group in groups)

    wrapped_at_base_size = _wrap_long_columns_to_page(ws, columns, used_cols, safe_w)
    if wrapped_at_base_size:
        ws.Rows.AutoFit()

    step = -1 if not fits() else 1
    current_sizes = _font_sizes(offset)
    for _ in range(0 if preserve_styles or (wrapped_at_base_size and fits()) else 18):
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
    _fit_merged_text_heights(ws, columns, groups, used_rows, used_cols)

    chunks = page_chunks_from_breaks(ws, groups)
    if chunks:
        _distribute_extra_height_uniform(ws, chunks, used_rows, safe_h)



def _wrap_long_columns_to_page(ws, columns: list[str], used_cols: int, safe_w: float) -> bool:
    """Keep the eight-point baseline by wrapping only existing long-text fields."""
    width = total_width_pt(ws, used_cols)
    if width <= safe_w:
        return False
    text_cols = [index for index, label in enumerate(columns, 1)
                 if label in ("상세주소", COL_ADDR, "비고2")]
    text_width = sum(ws.Columns(index).Width for index in text_cols)
    available = safe_w - (width - text_width)
    if not text_cols or available <= 0:
        # A very wide optional-column selection has no space to recover here.
        return False
    ratio = available / text_width
    for index in text_cols:
        column = ws.Columns(index)
        # Leave one native pixel of headroom per wrapped column for rounding.
        target_points = max(1, column.Width * ratio - .75)
        column.ColumnWidth *= target_points / column.Width
    return True


def _fit_merged_text_heights(ws, columns: list[str], groups: tuple[SheetGroup, ...],
                             used_rows: int, used_cols: int) -> None:
    """Excel AutoFit ignores merged cells; measure their text in an unmerged cell."""
    text_cols = [index for index, label in enumerate(columns, 1)
                 if label in ("상세주소", COL_ADDR, "비고2")]
    scratch = ws.Cells(used_rows + 1, used_cols + 1)
    try:
        scratch.NumberFormat = "@"
        scratch.WrapText = True
        for group in groups:
            if group.start_row == group.end_row:
                continue
            current = sum(ws.Rows(row).RowHeight for row in range(group.start_row, group.end_row + 1))
            required = current
            for index in text_cols:
                source = ws.Cells(group.start_row, index)
                scratch.Value = source.Value or ""
                scratch.Font.Name = source.Font.Name
                scratch.Font.Size = source.Font.Size
                scratch.Font.Bold = source.Font.Bold
                scratch.EntireColumn.ColumnWidth = ws.Columns(index).ColumnWidth
                scratch.EntireRow.AutoFit()
                required = max(required, scratch.RowHeight)
            if required > current:
                extra = (required - current) / (group.end_row - group.start_row + 1)
                for row in range(group.start_row, group.end_row + 1):
                    ws.Rows(row).RowHeight += extra
    finally:
        scratch.Clear()


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
    # Reserve the measured heights first. max(average, measured) per group
    # overfills pages whenever short and tall households occur together.
    extra_per_group = min(
        max(0, available_body_h
            - (summary_height if pg is chunks[-1] else 0)
            - sum(ws.Rows(row).RowHeight for group in pg
                  for row in range(group.start_row, group.end_row + 1))) / len(pg)
        for pg in chunks if pg
    )
    if extra_per_group <= 0:
        return
    for pg in chunks:
        for group in pg:
            extra_per_row = extra_per_group / (group.end_row - group.start_row + 1)
            for row in range(group.start_row, group.end_row + 1):
                current = ws.Rows(row).RowHeight
                # Round down to Excel's pixel grid, preserving each row's minimum.
                target = min(100, (current + extra_per_row) * 4 // 1 / 4)
                if target > current:
                    ws.Rows(row).RowHeight = target


def _clean_address_redundant_breaks(
    ws, data_end_row: int, all_cols: list[str], col_name: str
) -> None:
    """
    본문 글꼴과 열 너비를 유지하며 한 줄에 들어가는 주소의 줄바꿈만 제거합니다.
    주소만 별도로 확대하면 다른 칼럼과 크기가 달라지고 인쇄 배율도 작아집니다.

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
