"""청구서 생성기 — 템플릿 채우기 (배송일 계산은 delivery_calculator 위임).

방학 월: roster_data → 방학_방중(방중=1) / 방학_학기중(방중=0) / 방학_총괄(전체)
일반 월: roster_data → 학기_총괄(전체) / 학기_학기중(전체)
출력 시트명: prefix(방학_/학기_) 제거 → 총괄 / 학기중 / 방중
"""

import logging
from datetime import date

import openpyxl

from ..core.meal_types import MEAL_KEYS
from ..excel.excel_utils import CENTER_ALIGN, adjust_formula, copy_style
from ..services.delivery_calculator import count_delivery_days, to_bool
from ..templates.config import (
    col_name_to_index,
    get_template_data_start_row,
    read_template_columns,
)

log = logging.getLogger(__name__)

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
    return row_data


def _build_price_formula(
    col_idx_map: dict[str, int],
    bf_col: str,
    dn_col: str,
    lh_col: str,
    row_idx: int,
    unit_price_breakfast: int,
    unit_price_dinner: int,
    unit_price_lunch: int,
) -> str:
    """시트에 존재하는 식사 컬럼만 포함한 청구금액 수식 생성."""
    parts: list[str] = []

    bf_idx = col_idx_map.get(bf_col)
    if bf_idx is not None:
        parts.append(f"{_col_idx_to_letter(bf_idx)}{row_idx}*{unit_price_breakfast}")

    dn_idx = col_idx_map.get(dn_col)
    if dn_idx is not None:
        parts.append(f"{_col_idx_to_letter(dn_idx)}{row_idx}*{unit_price_dinner}")

    lh_idx = col_idx_map.get(lh_col)
    if lh_idx is not None:
        parts.append(f"{_col_idx_to_letter(lh_idx)}{row_idx}*{unit_price_lunch}")

    if not parts:
        return "0"
    return "=" + "+".join(parts)


def _fill_invoice_sheet(
    wb,
    sheet_name: str,
    people: list[dict],
    col_mapping: dict,
    template_path: str,
    year: int,
    month: int,
    unit_price_breakfast: int,
    unit_price_dinner: int,
    unit_price_lunch: int,
    excluded_dates: set[date] | None = None,
) -> int:
    """단일 시트에 people 데이터를 채운다. 작성 건수 반환."""
    template_columns = read_template_columns(template_path, sheet_name=sheet_name)
    col_idx_map = col_name_to_index(template_columns)

    bf_col = col_mapping.get("__calc_breakfast", "조식")
    dn_col = col_mapping.get("__calc_dinner", "석식")
    lh_col = col_mapping.get("__calc_lunch", "중식")
    price_col = col_mapping.get("__calc_price", "청구금액")

    ws = wb[sheet_name]
    row_idx = get_template_data_start_row()
    data_start_row = row_idx
    written_cols: set[int] = set()

    for i, person in enumerate(people):
        flags = {k: to_bool(person.get(f"_{k}", 0)) for k in MEAL_KEYS}
        note_text = person.get("_note", "")

        # ── 중식 배송일: _lunch_days override 우선 ──
        lunch_days_override = person.get("_lunch_days", None)
        lunch_flag_for_calc = flags["lunch"] and lunch_days_override is None

        days = count_delivery_days(
            year,
            month,
            flags["breakfast"],
            flags["dinner"],
            lunch_flag_for_calc,
            note_text,
            excluded_dates,
        )

        breakfast_cnt, dinner_cnt, lunch_cnt = (
            days["breakfast"],
            days["dinner"],
            days["lunch"] if lunch_days_override is None else lunch_days_override,
        )

        row_data = _map_person_to_row(person, col_mapping, i + 1, template_columns)

        # 식사 건수: 시트에 해당 컬럼이 있을 때만 기록
        if bf_col in col_idx_map:
            row_data[bf_col] = breakfast_cnt
        if dn_col in col_idx_map:
            row_data[dn_col] = dinner_cnt
        if lh_col in col_idx_map:
            row_data[lh_col] = lunch_cnt

        # 청구금액 수식: 시트에 존재하는 컬럼만 포함
        if price_col in col_idx_map:
            row_data[price_col] = _build_price_formula(
                col_idx_map,
                bf_col,
                dn_col,
                lh_col,
                row_idx,
                unit_price_breakfast,
                unit_price_dinner,
                unit_price_lunch,
            )

        # 결과 표시용 (첫 번째 시트에서만 누적 — 호출부에서 total 관리)
        person["_total"] = breakfast_cnt + dinner_cnt + lunch_cnt
        person["_amount"] = (
            f"{breakfast_cnt * unit_price_breakfast + dinner_cnt * unit_price_dinner + lunch_cnt * unit_price_lunch:,}원"
        )

        # ── Phase 1: 데이터 값 쓰기 ──
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

    # ── Phase 2: 첫 데이터 행 서식/수식 → 이후 행 복사 ──
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
                if c_idx not in written_cols and src_cell.value is not None:
                    val = src_cell.value
                    if isinstance(val, str) and str(val).startswith("="):
                        val = adjust_formula(str(val), r - data_start_row)
                    dst_cell.value = val
                copy_style(src_cell, dst_cell)
            ws.row_dimensions[r].height = _tmpl_height

    return data_end_row - data_start_row + 1


# ── 시트명 prefix 제거 + 미사용 prefix 시트 삭제 ──

_PREFIX_PAIRS: dict[str, str] = {
    "학기_총괄": "총괄",
    "학기_학기중": "학기중",
    "방학_총괄": "총괄",
    "방학_학기중": "학기중",
    "방학_방중": "방중",
}


def _rename_sheets(wb, keep_prefix: str):
    """keep_prefix 시트만 남기고 prefix 제거된 이름으로 변경.
    keep_prefix: "학기_" 또는 "방학_"
    """
    # 1) 이름 변경 (keep_prefix 시트만)
    for old, new in _PREFIX_PAIRS.items():
        if old.startswith(keep_prefix) and old in wb.sheetnames:
            try:
                wb[old].title = new
            except ValueError:
                # 이름 충돌 시 _2, _3 ...
                for n in range(2, 100):
                    cand = f"{new}_{n}"
                    if cand not in wb.sheetnames:
                        wb[old].title = cand
                        break

    # 2) 다른 prefix 시트 삭제
    other_prefix = "방학_" if keep_prefix == "학기_" else "학기_"
    for sn in list(wb.sheetnames):
        if sn.startswith(other_prefix):
            del wb[sn]


# ── 메인 진입점 ────────────────────────────────────


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
    is_vacation_month: bool = False,
):
    """roster_data → 청구서템플릿 다중 시트 채워서 output_path 에 저장.

    방학 여부에 따라:
      - 방학 O: 방학_방중(방중=1) / 방학_학기중(방중=0) / 방학_총괄(전체)
      - 방학 X: 학기_총괄(전체) / 학기_학기중(전체)

    출력 시트명은 prefix 제거 (총괄, 학기중, 방중).
    """

    # ── 방학 여부: 중식명단 is_vacation_month() 결과 사용 ──
    has_any_vacation = is_vacation_month

    wb = openpyxl.load_workbook(template_path)

    # ── 실제 배송 건수 사전 계산 (플래그만 보지 않고 비고/제외일 반영) ──
    n = len(roster_data)
    bf_cnt = [0] * n
    dn_cnt = [0] * n
    lh_cnt = [0] * n
    for i, p in enumerate(roster_data):
        flags = {k: to_bool(p.get(f"_{k}", 0)) for k in MEAL_KEYS}
        note_text = p.get("_note", "")

        lunch_days_override = p.get("_lunch_days", None)
        lunch_flag_for_calc = flags["lunch"] and lunch_days_override is None

        days = count_delivery_days(
            year,
            month,
            flags["breakfast"],
            flags["dinner"],
            lunch_flag_for_calc,
            note_text,
            excluded_dates,
        )
        bf_cnt[i] = days["breakfast"]
        dn_cnt[i] = days["dinner"]
        lh_cnt[i] = (
            days["lunch"] if lunch_days_override is None else lunch_days_override
        )
        log.debug(
            "[Split] %s: bf=%d dn=%d lh=%d",
            p.get("성명", "?"),
            bf_cnt[i],
            dn_cnt[i],
            lh_cnt[i],
        )

    if has_any_vacation:
        # ── 방학 월 ──
        #    학기중: 조식 or 석식 실제 배송 건수 > 0 인원만
        #    방중:   중식 실제 배송 건수 > 0 인원만
        #    총괄:   학기중 ∪ 방중 (합집합, 자동으로 셋 다 0이면 제외)
        school_people = [
            roster_data[i] for i in range(n) if bf_cnt[i] > 0 or dn_cnt[i] > 0
        ]
        vac_people = [roster_data[i] for i in range(n) if lh_cnt[i] > 0]
        union_people = [
            roster_data[i]
            for i in range(n)
            if bf_cnt[i] > 0 or dn_cnt[i] > 0 or lh_cnt[i] > 0
        ]

        _fill_invoice_sheet(
            wb,
            "방학_학기중",
            school_people,
            col_mapping,
            template_path,
            year,
            month,
            unit_price_breakfast,
            unit_price_dinner,
            unit_price_lunch,
            excluded_dates,
        )
        _fill_invoice_sheet(
            wb,
            "방학_방중",
            vac_people,
            col_mapping,
            template_path,
            year,
            month,
            unit_price_breakfast,
            unit_price_dinner,
            unit_price_lunch,
            excluded_dates,
        )
        total_count = _fill_invoice_sheet(
            wb,
            "방학_총괄",
            union_people,
            col_mapping,
            template_path,
            year,
            month,
            unit_price_breakfast,
            unit_price_dinner,
            unit_price_lunch,
            excluded_dates,
        )

        _rename_sheets(wb, "방학_")
    else:
        # ── 일반 월: 아무 식사라도 실제 배송 건수 > 0 인원만 ──
        active_people = [
            roster_data[i]
            for i in range(n)
            if bf_cnt[i] > 0 or dn_cnt[i] > 0 or lh_cnt[i] > 0
        ]
        _fill_invoice_sheet(
            wb,
            "학기_학기중",
            active_people,
            col_mapping,
            template_path,
            year,
            month,
            unit_price_breakfast,
            unit_price_dinner,
            unit_price_lunch,
            excluded_dates,
        )
        total_count = _fill_invoice_sheet(
            wb,
            "학기_총괄",
            active_people,
            col_mapping,
            template_path,
            year,
            month,
            unit_price_breakfast,
            unit_price_dinner,
            unit_price_lunch,
            excluded_dates,
        )

        _rename_sheets(wb, "학기_")

    wb.save(output_path)
    return total_count, roster_data
