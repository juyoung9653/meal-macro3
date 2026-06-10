"""청구서 생성기 — 템플릿 채우기 (배송일 계산은 delivery_calculator 위임)."""

from datetime import date

import openpyxl

from ..core.meal_types import MEAL_KEYS
from ..excel.excel_utils import CENTER_ALIGN, copy_style
from ..services.delivery_calculator import count_delivery_days, to_bool
from ..templates.config import (
    col_name_to_index,
    get_template_data_start_row,
    get_template_sheet_name,
    read_template_columns,
)

# ── 숫자 변환 헬퍼 ────────────────────────────────


def _to_numeric(val):
    """str → int/float 변환 가능하면 변환, 아니면 원본 반환.

    "3" → 3, "3.14" → 3.14, "010-1234" → "010-1234" (그대로)
    """
    if isinstance(val, (int, float)):
        return val
    if not isinstance(val, str) or not val.strip():
        return val
    s = val.strip()
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        return val


# ── 엑셀 컬럼 인덱스 → 문자 (A, B, ..., Z, AA, AB, ...) ──
def _col_idx_to_letter(idx: int) -> str:
    """1-based 컬럼 인덱스 → 엑셀 컬럼 문자."""
    letters = ""
    while idx > 0:
        idx, rem = divmod(idx - 1, 26)
        letters = chr(ord("A") + rem) + letters
    return letters


# ── 청구서 생성 ────────────────────────────────────


def _map_person_to_row(
    person: dict, mapping: dict, seq: int, template_columns: list[str]
) -> dict:
    """person dict + mapping → {template_col_name: value}"""
    row_data: dict[str, str | int] = {}
    for col in template_columns:
        roster_key = mapping.get(col, "")
        if col == "연번":
            row_data[col] = seq
        elif roster_key:
            row_data[col] = person.get(roster_key, "")
        # roster_key 없으면(=선택 안함) row_data에 추가 안 함 → 쓰기 단계에서 스킵
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

    sheet_name = get_template_sheet_name()
    wb = openpyxl.load_workbook(template_path)
    ws = wb[sheet_name]

    row_idx = get_template_data_start_row()
    data_start_row = row_idx
    written_cols: set[int] = set()

    for i, person in enumerate(roster_data):
        flags = {k: to_bool(person.get(f"_{k}", 0)) for k in MEAL_KEYS}
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

        # Return computed data for result display
        person["_total"] = breakfast_cnt + dinner_cnt + lunch_cnt
        person["_amount"] = (
            f"{breakfast_cnt * unit_price_breakfast + dinner_cnt * unit_price_dinner + lunch_cnt * unit_price_lunch:,}원"
        )

        # ── 데이터 값 쓰기 (Phase 1) ──
        for col_name, value in row_data.items():
            c_idx = col_idx_map.get(col_name)
            if c_idx is None:
                continue
            written_cols.add(c_idx)
            cell = ws.cell(row=row_idx, column=c_idx)
            v = _to_numeric(value)
            cell.value = None if isinstance(v, (int, float)) and v == 0 else v
            cell.alignment = CENTER_ALIGN

        row_idx += 1

    # ── Phase 2: 첫 데이터 행 값/수식 + 서식 → 이후 모든 행 일괄 적용 ──
    data_end_row = row_idx - 1
    if data_end_row > data_start_row:
        _tmpl_cells = [
            ws.cell(row=data_start_row, column=c) for c in range(1, ws.max_column + 1)
        ]
        _tmpl_height = ws.row_dimensions[data_start_row].height

        for r in range(data_start_row + 1, data_end_row + 1):
            for c_idx in range(1, len(_tmpl_cells) + 1):
                src_cell = _tmpl_cells[c_idx - 1]
                dst_cell = ws.cell(row=r, column=c_idx)
                # 쓰지 않은 컬럼은 첫 행의 값/수식을 복사
                if c_idx not in written_cols and src_cell.value is not None:
                    dst_cell.value = src_cell.value
                copy_style(src_cell, dst_cell)
            ws.row_dimensions[r].height = _tmpl_height

    wb.save(output_path)
    return row_idx - get_template_data_start_row(), roster_data
