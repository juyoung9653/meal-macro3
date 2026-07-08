"""배송대타 Excel 출력 — 템플릿 기반 행 삽입 방식.

assets/급식_배송대타_템플릿.xlsx 를 로드하여 3행에 데이터 행을 삽입하고
페이지당 최대 행 높이를 기준으로 페이지 구분선 + 남은 높이 분배를 수행한다.
"""

from __future__ import annotations

import logging
import os
import tempfile

import openpyxl
import pandas as pd
from openpyxl.styles import Alignment, Border, Font, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.pagebreak import Break

from src.core.paths import asset_path
from src.excel.colors import group_fill

log = logging.getLogger(__name__)

TEMPLATE_FILENAME = "assets/급식_배송대타_템플릿.xlsx"

# 페이지당 최대 추가 데이터 행 높이 (pt)
MAX_ROW_HEIGHT: dict[str, float] = {
    "전체": 1306.5,
    "상세주소X": 1237.5,
}

# 템플릿 컬럼명 → 데이터 컬럼명 매핑
_COL_MAP: dict[str, str] = {
    "배송순서": "배송순서",
    "관할동": "관할동",
    "이름": "이름",
    "상세주소": "상세주소",
    "신주소": "신주소",
    "핸드폰": "핸드폰",
    "비고": "비고2",
}

_FOOTER_ROW_HEIGHT = 24.0  # 합계 row
_DATA_ROW_HEIGHT = 24.0

_THIN = Side(style="thin")
_THIN_BORDER = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)
_THICK = Side(style="medium")
_THICK_BORDER = Border(left=_THICK, right=_THICK, top=_THICK, bottom=_THICK)
_CENTER_WRAP = Alignment(horizontal="center", vertical="center", wrap_text=True)
_CENTER_NOWRAP = Alignment(horizontal="center", vertical="center", wrap_text=False)


def show_excel(
    df: pd.DataFrame,
    meal: str,
    ship: str,
    date_str: str,
    template_choice: str,
) -> None:
    """배송대타 데이터를 템플릿에 행 삽입하여 출력."""
    tpl_path = asset_path(TEMPLATE_FILENAME)
    log.info("Loading template: %s (sheet: %s)", tpl_path, template_choice)

    wb = openpyxl.load_workbook(tpl_path)
    ws = wb[template_choice]
    ncols = ws.max_column

    # ── 템플릿 컬럼명 읽기 (Row 2) ──
    template_cols: list[str] = []
    for c in range(1, ncols + 1):
        val = ws.cell(row=2, column=c).value
        name = str(val).replace("\n", "").strip() if val else ""
        template_cols.append(name)

    log.info("Template columns: %s", template_cols)

    # 컬럼 인덱스
    try:
        ord_col_idx = template_cols.index("배송순서") + 1
    except ValueError:
        ord_col_idx = 1
    try:
        sum_col_idx = template_cols.index("요약") + 1
    except ValueError:
        sum_col_idx = ncols
    try:
        phone_col_idx = template_cols.index("핸드폰") + 1
    except ValueError:
        phone_col_idx = None

    n_data = len(df)

    # ── 3행에 데이터 행 삽입 (합계 row가 아래로 밀림) ──
    if n_data > 0:
        ws.insert_rows(3, n_data)

    # ── 선택되지 않은 시트 제거 ──
    for sn in list(wb.sheetnames):
        if sn != template_choice:
            del wb[sn]

    # ── 인쇄 영역 / 페이지 설정 갱신 ──
    last_col_letter = get_column_letter(ncols)
    last_row = 2 + n_data + 1  # header(2) + data(n) + footer(1)
    ws.print_area = f"A1:{last_col_letter}{last_row}"
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.print_title_rows = "$1:$2"

    # ── Row 1 제목 채우기 ──
    ws.cell(
        row=1, column=1
    ).value = f"식종: {meal} | 배송담당: {ship} | 날짜: {date_str}"

    # ── 데이터 행 채우기 ──
    font_data = Font(name="맑은 고딕", size=16)
    for i in range(n_data):
        row_num = 3 + i
        row_data = df.iloc[i]
        for c, tcol in enumerate(template_cols, 1):
            dcol = _COL_MAP.get(tcol)
            cell = ws.cell(row=row_num, column=c)
            if dcol and dcol in df.columns:
                val = row_data[dcol]
                cell.value = val if pd.notna(val) else ""
            elif tcol == "요약":
                cell.value = ""  # 하단 그룹 루프에서 채움
            else:
                cell.value = ""
            cell.font = font_data
            cell.border = _THIN_BORDER
            if c == phone_col_idx:
                cell.alignment = _CENTER_NOWRAP
            else:
                cell.alignment = _CENTER_WRAP

    # ── _BOX 행 굵은 테두리 (식사 구분 컬럼에 "_BOX" 포함된 행) ──
    _BOX_COL = "식사 구분"
    if _BOX_COL in df.columns:
        for i in range(n_data):
            meal_val = str(df.iloc[i][_BOX_COL])
            if "_BOX" in meal_val:
                row_num = 3 + i
                for c in range(1, ncols + 1):
                    ws.cell(row=row_num, column=c).border = _THICK_BORDER

    # ── 그룹 탐지 (배송순서 기준) ──
    groups = _detect_groups(ws, n_data, ord_col_idx)

    # ── 그룹별 병합 / 요약 / 색상 ──
    name_col_idx = template_cols.index("이름") + 1 if "이름" in template_cols else -1
    merge_cols = [c for c in range(1, ncols + 1) if c != name_col_idx]

    total_count = 0
    for s0, e0 in groups:
        r1, r2 = 3 + s0, 3 + e0
        n = e0 - s0 + 1
        total_count += n

        # 요약 컬럼에 그룹 인원수
        sum_cell = ws.cell(row=r1, column=sum_col_idx)
        sum_cell.value = n

        # 그룹 배경색
        gf = group_fill(n)
        if gf is not None:
            for r in range(r1, r2 + 1):
                for c in range(1, ncols + 1):
                    ws.cell(row=r, column=c).fill = gf

        # 그룹 내 셀 병합 (2인 이상, 이름 컬럼 제외)
        if n >= 2:
            for c in merge_cols:
                ws.merge_cells(start_row=r1, start_column=c, end_row=r2, end_column=c)

    # ── 합계 행 (Row 3 + n_data) ──
    sum_row = 3 + n_data
    ws.cell(row=sum_row, column=sum_col_idx).value = total_count

    # ── 행 높이 설정 ──
    ws.row_dimensions[1].height = 24
    ws.row_dimensions[2].height = 52.5
    for i in range(n_data):
        ws.row_dimensions[3 + i].height = _DATA_ROW_HEIGHT
    ws.row_dimensions[sum_row].height = _FOOTER_ROW_HEIGHT

    # ── 페이지 구분선 ──
    max_h = MAX_ROW_HEIGHT.get(template_choice, 1306.5)
    _apply_page_breaks(ws, groups, max_h, _DATA_ROW_HEIGHT)

    # ── 페이지별 남은 높이 분배 ──
    _distribute_extra_height(ws, groups, max_h, _DATA_ROW_HEIGHT)

    # ── 임시 파일 저장 후 열기 ──
    tmp = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
    tmp_path = tmp.name
    tmp.close()

    try:
        wb.save(tmp_path)
        os.startfile(tmp_path)
        log.info("Delivery excel saved to %s", tmp_path)
    except Exception:
        log.exception("Failed to save/open delivery excel file")
        raise


# ── 헬퍼 ────────────────────────────────────────────────────


def _detect_groups(ws, n_data: int, ord_col_idx: int) -> list[tuple[int, int]]:
    """데이터 행에서 배송순서 기준 그룹의 (start, end) 인덱스 반환."""
    if n_data == 0:
        return []

    groups: list[tuple[int, int]] = []
    i = 0
    while i < n_data:
        j = i
        val_i = str(ws.cell(row=3 + i, column=ord_col_idx).value or "")
        while j + 1 < n_data:
            val_next = str(ws.cell(row=3 + j + 1, column=ord_col_idx).value or "")
            if val_next == val_i:
                j += 1
            else:
                break
        groups.append((i, j))
        i = j + 1

    return groups


def _apply_page_breaks(
    ws,
    groups: list[tuple[int, int]],
    max_height: float,
    row_h: float,
) -> None:
    """그룹 단위로 페이지당 최대 행 높이를 넘지 않도록 페이지 구분선 삽입."""
    if not groups:
        return

    accumulated = 0.0
    for gi, (s0, e0) in enumerate(groups):
        n = e0 - s0 + 1
        group_h = n * row_h
        if accumulated + group_h > max_height and gi > 0:
            ws.row_breaks.append(Break(id=3 + s0))
            log.info(
                "Page break at row %d (group %d, accumulated=%.1f)",
                3 + s0,
                gi,
                accumulated,
            )
            accumulated = 0.0
        accumulated += group_h


def _distribute_extra_height(
    ws,
    groups: list[tuple[int, int]],
    max_height: float,
    row_h: float,
) -> None:
    """페이지별 남은 높이를 그룹에 균등 분배하여 행 높이를 키운다."""
    if not groups:
        return

    # page break 기준으로 그룹을 페이지 청크로 분할
    page_breaks = sorted(b.id for b in ws.row_breaks.brk)
    chunks = _chunks_from_breaks(groups, page_breaks)

    if not chunks:
        return

    for ci, pg in enumerate(chunks):
        groups_in_page = len(pg)
        if groups_in_page <= 0:
            continue

        total_rows = sum(e - s + 1 for s, e in pg)
        used = total_rows * row_h
        overflow = max_height - used

        if overflow <= 0:
            continue

        log.info(
            "Page %d: groups=%d rows=%d used=%.1f overflow=%.1f",
            ci,
            groups_in_page,
            total_rows,
            used,
            overflow,
        )

        # 그룹별 overflow 균등 분배 (마지막 그룹이 반올림 손실분 흡수)
        extra_per_group = overflow / groups_in_page
        distributed = 0.0
        for i, (s0, e0) in enumerate(pg):
            n_rows = e0 - s0 + 1
            group_extra = extra_per_group
            if i == len(pg) - 1:
                group_extra = overflow - distributed
            per_row = group_extra / n_rows
            h = round(row_h + per_row, 1)
            for r in range(3 + s0, 3 + e0 + 1):
                ws.row_dimensions[r].height = h
            distributed += group_extra


def _chunks_from_breaks(
    groups: list[tuple[int, int]],
    page_breaks: list[int],
) -> list[list[tuple[int, int]]]:
    """page break 위치 기준으로 그룹을 페이지 청크로 분할.

    page_break 행은 새 페이지의 첫 그룹 시작 행.
    """
    chunks: list[list[tuple[int, int]]] = []
    cur: list[tuple[int, int]] = []
    bi = 0

    for g in groups:
        s0, _ = g
        start_row = 3 + s0
        while bi < len(page_breaks) and start_row >= page_breaks[bi]:
            if cur:
                chunks.append(cur)
            cur = []
            bi += 1
        cur.append(g)

    if cur:
        chunks.append(cur)

    return chunks
