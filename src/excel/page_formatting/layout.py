"""페이지 레이아웃 최적화 — openpyxl 기반 폰트/컬럼폭/행높이 설정."""

import logging

import pandas as pd
from openpyxl.styles import Alignment, Font
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.pagebreak import Break

from src.core.settings import get_settings
from src.excel.groups import PagePlan, SplitMode, build_page_plan

log = logging.getLogger(__name__)

_S = get_settings()
COL_ADDR = _S.get_delivery_config("col_addr")
COL_PHONE = _S.get_delivery_config("col_phone")
COL_SUM = _S.get_delivery_config("col_sum")
FONT_BASE_SIZE = _S.get_delivery_config("font_base_size")

from .setup import apply_base_page_setup, page_limits_pt


def apply_page_formatting(
    ws,
    df: pd.DataFrame,
    mode: int,
    max_groups_limit: int,
    orientation_pref: int = 0,
) -> None:
    """워크시트에 인쇄용 페이지 포맷을 적용합니다."""
    used_rows = len(df) + 3
    used_cols = len(df.columns) + 1

    actual_orientation = apply_base_page_setup(
        ws, used_rows, used_cols, orientation_pref
    )
    page_plan = build_page_plan(df, mode, max_groups_limit)
    all_cols = [*df.columns.tolist(), COL_SUM]

    _clean_address_newlines(ws, used_rows, all_cols, COL_ADDR)
    limit_h, limit_w = page_limits_pt(actual_orientation)
    safe_h, safe_w = limit_h - 5, limit_w - 5

    def _visual_width(s: str) -> int:
        """문자열의 시각적 폭: 한글/한자/CJK = 2, ASCII = 1."""
        return sum(2 if ord(c) > 0x2E80 else 1 for c in s)

    # 컬럼별 최대 시각적 문자 수 계산
    col_chars: dict[str, int] = {}
    for col in all_cols:
        hdr_len = _visual_width(str(col))
        if col in df.columns:
            column_index = list(df.columns).index(col)
            data_len = max(
                (_visual_width(str(value)) for value in df.iloc[:, column_index]),
                default=0,
            )
        else:
            data_len = 0
        col_chars[col] = max(hdr_len, data_len)

    best = calc_optimal_font_size(
        col_chars, len(df), used_rows, used_cols, actual_orientation
    )

    # PagePlan row는 worksheet 기준 1-based다. OpenXML 변환은 이 계층에서만 한다.
    for next_page_start_row in page_plan.next_page_start_rows:
        # OpenXML brk/@id is zero-based and the break is above that row.
        ws.row_breaks.append(Break(id=next_page_start_row - 1))

    # 폰트/컬럼폭/행높이 적용
    min_row_h = round(best * 1.5, 1)
    _apply_fonts(ws, used_rows, all_cols, best)
    _apply_column_widths(ws, all_cols, col_chars, best, safe_w)
    _apply_row_heights(ws, used_rows, min_row_h, all_cols)

    # 페이지별 여유 높이 균등 배분도 같은 PagePlan의 청크를 사용한다.
    if page_plan.split_mode is not SplitMode.NONE and page_plan.page_chunks:
        _distribute_extra_height_overflow(
            ws, page_plan, used_rows, safe_h, min_row_h=min_row_h
        )


def calc_optimal_font_size(
    col_chars: dict[str, int],
    data_len: int,
    used_rows: int,
    used_cols: int,
    actual_orientation: int,
) -> float:
    """세로 높이 기준 최적 폰트 크기 계산.

    FitToWidth=1 설정으로 Excel이 가로 폭을 자동 스케일링하므로,
    가로 제약은 무시하고 세로(페이지 높이)만 고려하여 최대 폰트 선정.
    """
    limit_h, _ = page_limits_pt(actual_orientation)
    safe_h = limit_h - 5

    def estimate_height(pt: float, n_rows: int) -> float:
        row_h = round(pt * 1.5, 1)
        return (2 + n_rows + 1) * row_h  # 2 headers + data + 1 footer = used_rows

    n = data_len
    best = FONT_BASE_SIZE

    for pt in [x * 0.5 for x in range(32, 9, -1)]:
        h = estimate_height(pt, n)
        if h <= safe_h:
            best = pt
            break

    best = max(best, 10.0)

    log.info(
        "calc_opt_font: safe_h=%.1f n=%d best=%.1f",
        safe_h,
        n,
        best,
    )

    return best


# ── 적용 함수 ──────────────────────────────────────────────


def _apply_fonts(ws, used_rows: int, all_cols: list[str], best: float) -> None:
    """워크시트 전체 폰트 적용 (sheet_writer는 값을 쓰기만 함)."""
    offset = best - FONT_BASE_SIZE

    # 1행: 정보행
    ws.cell(row=1, column=1).font = Font(bold=True, size=12)

    # 2행: 헤더행
    ws.cell(row=2, column=1).font = Font(bold=True, size=FONT_BASE_SIZE + 2 + offset)
    for c in range(2, len(all_cols) + 1):
        ws.cell(row=2, column=c).font = Font(
            bold=True, size=FONT_BASE_SIZE + 2 + offset
        )

    # 3행+: 데이터
    size = max(5, FONT_BASE_SIZE + offset)
    for c in range(1, len(all_cols) + 1):
        for r in range(3, used_rows + 1):
            ws.cell(row=r, column=c).font = Font(size=size)


def _apply_column_widths(
    ws,
    all_cols: list[str],
    col_chars: dict[str, int],
    best: float,
    safe_w: float,
) -> None:
    """컬럼 폭을 인쇄 가능 폭 내로 계산 (overflow 방지).

    openpyxl width 1 = '0' 문자 폭 ≈ 7px (Calibri 11pt 기준).
    pt 변환: width * 5.25.
    모든 컬럼 폭 합이 safe_w를 넘지 않도록 비례 축소.
    """
    scale = best / 11.0 if best > 0 else 1.0

    # 각 컬럼의 희망 폭 계산
    desired = {}
    for i, col in enumerate(all_cols):
        chars = col_chars.get(col, 8)
        w = round((chars + 2) * scale + 0.5, 1)
        desired[col] = max(w, 8.0)

    # 총 폭(pt)이 safe_w 이내가 되도록 비례 축소
    total_pt = sum(desired.values()) * 5.25
    if total_pt > safe_w and len(desired) > 0:
        ratio = safe_w / total_pt
        for col, width in desired.items():
            desired[col] = max(round(width * ratio, 1), 6.0)

    # 적용
    for i, col in enumerate(all_cols, 1):
        ws.column_dimensions[get_column_letter(i)].width = desired.get(col, 8.0)

    # 주소 컬럼에 여유 공간 추가 (있으면)
    current_total_pt = sum(desired.values()) * 5.25
    gap = safe_w - current_total_pt
    if gap > 10:
        try:
            addr_idx = all_cols.index(COL_ADDR) + 1
            addr_col = get_column_letter(addr_idx)
            extra = round(gap * 0.5 / 5.25, 1)
            ws.column_dimensions[addr_col].width = desired[COL_ADDR] + extra
        except ValueError:
            pass


def _apply_row_heights(
    ws, used_rows: int, min_row_h: float, all_cols: list[str] | None = None
) -> None:
    """행 높이 설정 및 데이터 영역 가운데 정렬.

    핸드폰 컬럼은 줄바꿈 해제.
    """
    ws.row_dimensions[1].height = 24
    ws.row_dimensions[2].height = max(22, min_row_h)

    center_wrap = Alignment(horizontal="center", vertical="center", wrap_text=True)
    center_nowrap = Alignment(horizontal="center", vertical="center", wrap_text=False)

    # 핸드폰 컬럼 인덱스 (줄바꿈 해제 대상)
    phone_col_idx = None
    if all_cols:
        try:
            phone_col_idx = all_cols.index(COL_PHONE) + 1
        except ValueError:
            pass

    for r in range(3, used_rows + 1):
        ws.row_dimensions[r].height = min_row_h
        for c in range(1, ws.max_column + 1):
            if c == phone_col_idx:
                ws.cell(row=r, column=c).alignment = center_nowrap
            else:
                ws.cell(row=r, column=c).alignment = center_wrap

    ws.row_dimensions[2].alignment = center_nowrap


# ── 주소 줄바꿈 처리 ─────────────────────────────────────────


def _clean_address_newlines(ws, used_rows, all_cols, col_name):
    """줄바꿈을 콤마로 변환 (AutoFit 없이 수동 처리)."""
    try:
        col_idx = all_cols.index(col_name) + 1
    except ValueError:
        return
    for r in range(3, used_rows + 1):
        cell = ws.cell(row=r, column=col_idx)
        text = str(cell.value or "")
        if any(b in text for b in ("\n", "\r")):
            cell.value = (
                text.replace("\r\n", ", ")
                .replace("\n", ", ")
                .replace("\r", ", ")
                .strip()
            )


# ── 페이지 높이 균등 배분 ────────────────────────────────────


_PAGE_BOTTOM_MARGIN = 8


def _distribute_extra_height_overflow(
    ws,
    page_plan: PagePlan,
    used_rows: int,
    safe_h: float,
    offset: int = 3,
    min_row_h: float = 0,
) -> None:
    """페이지 여유 높이를 그룹별 균등 배분 (순서 유지).

    모든 행을 min_row_h로 설정 후, overflow를 그룹 개수로 균등 분할.
    각 그룹은 자기 몫을 자기 행들에 균등 배분.
    그룹 내 행 높이는 동일, 그룹 간 행 높이는 행 수에 반비례.
    (그룹 순서 유지, 정렬하지 않음)
    """
    if not page_plan.page_chunks:
        return

    h_hdr = (ws.row_dimensions[1].height or 0) + (ws.row_dimensions[2].height or 0)
    footer_h = ws.row_dimensions[used_rows].height or 0 if used_rows > 0 else 0

    for ci, pg in enumerate(page_plan.page_chunks):
        groups_in_page = len(pg)
        if groups_in_page <= 0:
            continue

        is_last = ci == len(page_plan.page_chunks) - 1
        total_rows = sum(group.row_count for group in pg)
        min_total = h_hdr + total_rows * min_row_h + _PAGE_BOTTOM_MARGIN
        if is_last:
            min_total += footer_h
        overflow = safe_h - min_total

        if ci == 0:
            log.info(
                "page%d: groups=%d total_rows=%d min_total=%.1f overflow=%.1f min_row_h=%.1f",
                ci,
                groups_in_page,
                total_rows,
                min_total,
                overflow,
                min_row_h,
            )

        if overflow <= 0:
            for group in pg:
                for r in range(group.start_index + offset, group.end_index + offset + 1):
                    ws.row_dimensions[r].height = min_row_h
            continue

        # 1) 모든 행을 min_row_h로 설정
        for group in pg:
            for r in range(group.start_index + offset, group.end_index + offset + 1):
                ws.row_dimensions[r].height = min_row_h

        # 2) 각 그룹에 overflow 균등 배분 (그룹 순서 유지)
        extra_per_group = overflow / groups_in_page
        distributed = 0.0
        for i, group in enumerate(pg):
            n_rows = group.row_count
            group_extra = extra_per_group
            # 마지막 그룹: 반올림 손실분 흡수
            if i == len(pg) - 1:
                group_extra = overflow - distributed
            per_row = group_extra / n_rows
            h = round(min_row_h + per_row, 1)
            for r in range(group.start_index + offset, group.end_index + offset + 1):
                ws.row_dimensions[r].height = h
            distributed += group_extra
