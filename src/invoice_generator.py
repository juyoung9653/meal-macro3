"""청구서 생성기 — 템플릿 채우기 (배송일 계산은 delivery_calculator 위임)."""

from datetime import date

import openpyxl
from openpyxl.styles import Font

from .config import (
    _get_template_data_start_row,
    _get_template_sheet_name,
    col_name_to_index,
    read_template_columns,
)
from .delivery_calculator import _to_bool, count_delivery_days
from .excel_utils import CENTER_ALIGN, copy_cell
from .settings import get_settings


# ── 엑셀 컬럼 인덱스 → 문자 (A, B, ..., Z, AA, AB, ...) ──
def _col_idx_to_letter(idx: int) -> str:
    """1-based 컬럼 인덱스 → 엑셀 컬럼 문자."""
    letters = ""
    while idx > 0:
        idx, rem = divmod(idx - 1, 26)
        letters = chr(ord("A") + rem) + letters
    return letters


# ── 폰트 ──────────────────────────────────────────


def _make_invoice_fonts():
    """설정에서 폰트 객체 생성."""
    s = get_settings()
    fn = s.get("display", "font_name", default="맑은 고딕")
    sz = s.get("display", "invoice_font_size", default=9)
    return Font(name=fn, size=sz), Font(name=fn, size=sz, bold=True)


# ── 청구서 생성 ────────────────────────────────────


def _map_person_to_row(
    person: dict, mapping: dict, seq: int, template_columns: list[str]
) -> dict:
    """person dict + mapping → {template_col_name: value}"""
    row_data: dict[str, str | int] = {}
    for col in template_columns:
        roster_key = mapping.get(col, "")
        row_data[col] = (
            seq if col == "연번" else person.get(roster_key, "") if roster_key else ""
        )
    return row_data


def make_invoice(
    roster_data: list[dict],
    col_mapping: dict,
    output_path: str,
    template_path: str,
    year: int,
    month: int,
    unit_price_breakfast: int,
    unit_price_dinner: int,
    unit_price_lunch: int,
    excluded_dates: set[date] | None = None,
):
    """roster_data → 청구서템플릿 채워서 output_path 에 저장."""

    template_columns = read_template_columns(template_path)
    invoice_font, invoice_font_bold = _make_invoice_fonts()

    # ── 사전 계산: 컬럼명 → 1-based 인덱스 (list.index() 반복 호출 제거) ──
    col_idx_map = col_name_to_index(template_columns)

    bf_col = col_mapping.get("__calc_breakfast", "조식")
    dn_col = col_mapping.get("__calc_dinner", "석식")
    lh_col = col_mapping.get("__calc_lunch", "중식")
    price_col = col_mapping.get("__calc_price", "청구금액")

    # ── 수식용 컬럼 문자 사전 계산 ──
    bf_col_idx = col_idx_map.get(bf_col, 1)
    dn_col_idx = col_idx_map.get(dn_col, 1)
    lh_col_idx = col_idx_map.get(lh_col, 1)
    bf_letter = _col_idx_to_letter(bf_col_idx)
    dn_letter = _col_idx_to_letter(dn_col_idx)
    lh_letter = _col_idx_to_letter(lh_col_idx)

    sheet_name = _get_template_sheet_name()
    wb = openpyxl.load_workbook(template_path)
    ws = wb[sheet_name]

    row_idx = _get_template_data_start_row()
    data_start_row = row_idx

    # 첫 데이터 행 셀 참조·높이 저장 (값+서식+수식 복사용)
    _template_row_cells = [
        ws.cell(row=data_start_row, column=c) for c in range(1, ws.max_column + 1)
    ]
    _tmpl_row_height = ws.row_dimensions[data_start_row].height

    # ── 덮어쓸 데이터 컬럼 인덱스 집합 ──
    _data_col_idxs: set[int] = set()

    for i, person in enumerate(roster_data):
        flags = {
            k: _to_bool(person.get(f"_{k}", 0))
            for k in ("breakfast", "dinner", "lunch")
        }
        note_text = person.get("_note", "")
        days = count_delivery_days(
            year,
            month,
            flags["breakfast"],
            flags["dinner"],
            flags["lunch"],
            note_text,
            excluded_dates,
        )

        breakfast_cnt, dinner_cnt, lunch_cnt = (
            days["breakfast"],
            days["dinner"],
            days["lunch"],
        )

        row_data = _map_person_to_row(person, col_mapping, i + 1, template_columns)

        row_data[bf_col] = breakfast_cnt
        row_data[dn_col] = dinner_cnt
        row_data[lh_col] = lunch_cnt

        row_data[price_col] = (
            f"={bf_letter}{row_idx}*{unit_price_breakfast}"
            f"+{dn_letter}{row_idx}*{unit_price_dinner}"
            f"+{lh_letter}{row_idx}*{unit_price_lunch}"
        )

        # ── 템플릿 행 복사 (데이터 컬럼 제외 → 덮어쓰기 낭비 방지) ──
        if i > 0:
            for _col_idx in range(1, len(_template_row_cells) + 1):
                if _col_idx in _data_col_idxs:
                    continue
                copy_cell(
                    _template_row_cells[_col_idx - 1],
                    ws.cell(row=row_idx, column=_col_idx),
                )
            ws.row_dimensions[row_idx].height = _tmpl_row_height

        # ── 데이터 쓰기 (ws.cell() 직접 접근, string 파싱 회피) ──
        for col_name, value in row_data.items():
            c_idx = col_idx_map.get(col_name)
            if c_idx is None:
                continue
            cell = ws.cell(row=row_idx, column=c_idx)
            cell.value = value
            cell.font = invoice_font
            cell.alignment = CENTER_ALIGN
            _data_col_idxs.add(c_idx)

        row_idx += 1

    wb.save(output_path)
    return row_idx - _get_template_data_start_row()
