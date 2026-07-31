"""리포트 엑셀 출력 — openpyxl만 사용. 템플릿 복사 + water-filling + 페이지설정."""

from __future__ import annotations

import logging
from copy import copy
from datetime import date
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.cell.rich_text import CellRichText, TextBlock
from openpyxl.cell.text import InlineFont
from openpyxl.styles import Border, Font, PatternFill, Side
from openpyxl.worksheet.page import PageMargins

from ..core.diff_format import has_diff, strip_diff
from ..core.settings import get_settings

log = logging.getLogger(__name__)

_DEFAULT_ROW_HEIGHT = 15


def get_template_path() -> str:
    return get_settings().get_report_template_path()


TEMPLATE_SHEET = "aaa"


def _format_korean_date(date_str: str) -> str:
    if not date_str:
        return ""
    try:
        parsed = date(int(date_str[:4]), int(date_str[4:6]), int(date_str[6:8]))
    except (ValueError, IndexError):
        return date_str
    return f"{parsed.month}월 {parsed.day}일"


def export_multi(
    results: list[tuple[str, str, list[list]]],
    filepath: str | Path,
) -> None:
    """여러 날짜 리포트를 하나의 Excel 파일로 저장 (날짜별 시트)."""
    if not results:
        raise ValueError("내보낼 리포트 결과가 없습니다.")

    out_file = Path(filepath)
    tpl_file = Path(get_template_path())
    if not tpl_file.is_file():
        raise FileNotFoundError(
            f"배송 리포트 템플릿을 찾을 수 없습니다: {tpl_file}"
        )

    log.info("export: %d시트 → %s", len(results), out_file)
    tpl = load_workbook(tpl_file)
    out = Workbook()
    try:
        src = tpl[TEMPLATE_SHEET]
        default = out.active
        if default is not None:
            out.remove(default)

        for date_str, meal, table in results:
            ws = out.create_sheet(title=f"{date_str}({meal})")
            _copy_sheet(src, ws)
            ws["A1"] = _format_korean_date(date_str)
            _fill_data(ws, table, meal)

            last_row = ws.max_row
            ws.print_area = f"A1:I{last_row}"
            _stretch_rows_to_page(ws, last_row)
            _apply_page_setup(ws)

        out.save(out_file)
    finally:
        out.close()
        tpl.close()

    if not out_file.is_file():
        raise OSError(f"리포트 파일이 생성되지 않았습니다: {out_file}")


def export(
    table: list[list],
    filepath: str | Path,
    date_str: str,
    meal: str,
) -> None:
    """export_multi 래퍼 (단일 시트)."""
    export_multi([(date_str, meal, table)], filepath)


# ═══════════════════════════════════════════════════
#  내부 함수
# ═══════════════════════════════════════════════════


def _copy_sheet(src, dst) -> None:
    """src 워크시트의 서식/값/병합을 dst로 복사."""
    for col_letter, dim in src.column_dimensions.items():
        dst.column_dimensions[col_letter].width = dim.width
    for row_num, dim in src.row_dimensions.items():
        dst.row_dimensions[row_num].height = dim.height

    for row in src.iter_rows(min_row=1, max_row=src.max_row, max_col=src.max_column):
        for cell in row:
            nc = dst.cell(row=cell.row, column=cell.column)
            if cell.value is not None:
                nc.value = cell.value
            nc.font = copy(cell.font)
            nc.alignment = copy(cell.alignment)
            nc.border = copy(cell.border)
            nc.fill = copy(cell.fill)
            nc.number_format = cell.number_format

    for merged in src.merged_cells.ranges:
        dst.merge_cells(str(merged))


def _stretch_rows_to_page(ws, last_row: int) -> None:
    """Water-filling: 낮은 행부터 차례로 다음 높이까지 끌어올림."""
    if last_row <= 0:
        return

    total_ht = 0.0
    for r in range(1, last_row + 1):
        h = ws.row_dimensions[r].height
        total_ht += h if h is not None else _DEFAULT_ROW_HEIGHT

    extra = 900 - total_ht
    if extra <= 1:
        return

    staff_rows = []
    for r in range(4, last_row + 1):
        val = ws.cell(r, 1).value
        if val is not None and str(val) != "합계":
            staff_rows.append(r)
    if not staff_rows:
        return

    arr = []
    for r in staff_rows:
        h = ws.row_dimensions[r].height
        arr.append((r, h if h is not None else _DEFAULT_ROW_HEIGHT))
    arr.sort(key=lambda x: x[1])

    n = len(arr)
    i = 1
    while i < n and extra > 1:
        gap = arr[i][1] - arr[i - 1][1]
        if gap > 0:
            cost = gap * i
            if extra >= cost:
                for j in range(i):
                    arr[j] = (arr[j][0], arr[i][1])
                extra -= cost
            else:
                add_each = extra / i
                for j in range(i):
                    arr[j] = (arr[j][0], arr[j][1] + add_each)
                extra = 0
                break
        i += 1

    if extra > 1:
        add_each = extra / n
        for j in range(n):
            arr[j] = (arr[j][0], arr[j][1] + add_each)

    for r, h in arr:
        ws.row_dimensions[r].height = h


def _apply_page_setup(ws) -> None:
    CM2IN = 0.3 / 2.54
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.page_setup.orientation = "portrait"
    ws.page_setup.fitToPage = True
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.page_setup.scale = None
    ws.page_margins = PageMargins(
        left=CM2IN,
        right=CM2IN,
        top=CM2IN,
        bottom=CM2IN,
        header=0,
        footer=0,
    )


_FILL_DIFF = PatternFill(start_color="C5D9F1", end_color="C5D9F1", fill_type="solid")


def _fill_data(ws, table: list[list], meal: str) -> None:
    """템플릿 시트에 데이터 채움."""
    data_rows = table[1:]  # 헤더 제외

    summary_row = None
    if data_rows and data_rows[-1] and str(data_rows[-1][0]) == "합계":
        summary_row = data_rows.pop()

    ws["A2"] = f"*아동급식 {meal}*"

    data_count = len(data_rows)
    if data_count > 5:
        ws.insert_rows(9, data_count - 5)
        src_height = ws.row_dimensions[8].height
        for r in range(9, 9 + data_count - 5):
            if src_height:
                ws.row_dimensions[r].height = src_height
            for c in range(1, 10):
                src = ws.cell(8, c)
                dst = ws.cell(r, c)
                dst.font = copy(src.font)
                dst.alignment = copy(src.alignment)
                dst.border = copy(src.border)
                dst.fill = copy(src.fill)
                dst.number_format = src.number_format

    group_rows = list(range(4, 4 + data_count))
    summary_row_num = 4 + data_count

    for i, gr in enumerate(group_rows):
        row = data_rows[i]

        row_has_diff = any(has_diff(row[j + 1]) for j in range(5))

        c_a = ws.cell(gr, 1)
        c_a.value = row[0]
        if row_has_diff:
            c_a.fill = _FILL_DIFF

        for j in range(5):
            raw = row[j + 1] if j + 1 < len(row) else ""
            c = ws.cell(gr, j + 2)
            stripped = strip_diff(raw)
            c.value = int(stripped) if stripped != "" else ""
            if has_diff(row[j + 1] if j + 1 < len(row) else ""):
                c.fill = _FILL_DIFF

        raw = row[6] if len(row) > 6 else ""
        ws.cell(gr, 7).value = int(raw) if raw != "" else ""

        raw = row[7] if len(row) > 7 else ""
        c_h = ws.cell(gr, 8)

        if raw:
            # 컬럼 폭 대비 텍스트 길이에 맞춰 폰트 크기 자동 조절
            col_w = ws.column_dimensions["H"].width or 23
            col_px = int(col_w * 8) - 12  # 184px - 여유 12px
            lines = raw.split("\n")

            # 줄별 필요한 폰트 크기 계산
            font_sizes: list[int] = []
            for line in lines:
                cnt_k = sum(1 for c in line if ord(c) > 127)
                cnt_a = sum(1 for c in line if ord(c) <= 127)
                coeff = cnt_k * 1.33 + cnt_a * 0.67
                if coeff > 0:
                    ideal = col_px / coeff
                    font_sizes.append(max(6, min(11, int(ideal))))
                else:
                    font_sizes.append(_FONT_SIZE)

            c_h.value = _make_memo_rich_text(raw, font_sizes)

            # plain string이면 셀 폰트 직접 설정
            if isinstance(c_h.value, str) and "\n" not in raw:
                c_h.font = Font(name=_FONT_NAME, size=font_sizes[0])

            align = copy(c_h.alignment)
            align.wrap_text = True
            c_h.alignment = align

            line_count = len(lines)
            est_height = max(17, line_count * 17)
            if est_height > (ws.row_dimensions[gr].height or 0):
                ws.row_dimensions[gr].height = est_height
        else:
            c_h.value = ""

        ws.cell(gr, 9).value = row[8] if len(row) > 8 else ""

    if summary_row:
        for j in range(1, 10):
            val = summary_row[j] if j < len(summary_row) else ""
            if 1 <= j <= 6:
                ws.cell(summary_row_num, j + 1).value = int(val) if val != "" else ""
            else:
                ws.cell(summary_row_num, j + 1).value = val

    thin = Side(style="thin")
    medium = Side(style="medium")
    for r in range(4, 4 + data_count):
        bt = medium if r == 4 + data_count - 1 else thin
        for c in range(1, 10):
            cell = ws.cell(r, c)
            cell.border = Border(
                left=cell.border.left,
                right=cell.border.right,
                top=cell.border.top,
                bottom=bt,
            )


_FONT_NAME = "맑은 고딕"
_FONT_SIZE = 11


def _make_memo_rich_text(
    text: str,
    font_size: int | list[int] = _FONT_SIZE,
) -> str | CellRichText:
    """비고 텍스트: 줄바꿈 기준 홀수 줄 빨간색.

    font_size가 int면 모든 줄에 동일 적용, list면 줄별 크기 지정.
    """
    if "\n" not in text:
        return text
    lines = text.split("\n")
    sizes = (
        [font_size] * len(lines)
        if isinstance(font_size, int)
        else font_size + [_FONT_SIZE] * max(0, len(lines) - len(font_size))
    )
    blocks = []
    for i, line in enumerate(lines):
        sz = sizes[i] if i < len(sizes) else _FONT_SIZE
        rgb = "FFFF0000" if i % 2 == 0 else "FF000000"
        kw = {"rFont": _FONT_NAME, "sz": sz, "color": rgb, "b": True}
        t = line if i == len(lines) - 1 else line + "\n"
        blocks.append(TextBlock(InlineFont(**kw), t))
    return CellRichText(blocks)
