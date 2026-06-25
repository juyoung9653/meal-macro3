"""청구서 생성기 — 템플릿 채우기 (배송일 계산은 delivery_calculator 위임).

방학 월: roster_data → 방학_방중(방중=1) / 방학_학기중(방중=0) / 방학_총괄(전체)
일반 월: roster_data → 학기_총괄(전체) / 학기_학기중(전체)
출력 시트명: prefix(방학_/학기_) 제거 → 총괄 / 학기중 / 방중

급식 유형: 시트 분할 없이 한 시트로 모든 인원 출력.
"""

import logging
from datetime import date

import openpyxl
from openpyxl.styles import Alignment

from ..core.diff_format import to_numeric
from ..excel.excel_utils import CENTER_ALIGN, adjust_formula, copy_style
from ..templates.config import (
    col_name_to_index,
    get_template_data_start_row,
    read_template_columns,
)

log = logging.getLogger(__name__)


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


_MEMO_ALIGN = Alignment(horizontal="left", vertical="top", wrap_text=True)


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
    meal_type: str = "급식",
) -> int:
    """단일 시트에 people 데이터를 채운다. 작성 건수 반환."""
    template_columns = read_template_columns(template_path, sheet_name=sheet_name)
    col_idx_map = col_name_to_index(template_columns)

    bf_col = col_mapping.get("__calc_breakfast", "조식")
    dn_col = col_mapping.get("__calc_dinner", "석식")
    lh_col = col_mapping.get("__calc_lunch", "중식")
    price_col = col_mapping.get("__calc_price", "청구금액")
    del_start_col = col_mapping.get("__delivery_start", "")
    del_end_col = col_mapping.get("__delivery_end", "")
    # 날짜 텍스트 컬럼 — to_numeric 변환 방지 + 셀 서식 텍스트
    _text_cols = {
        col_idx_map[c] for c in (del_start_col, del_end_col) if c and c in col_idx_map
    }

    # 메모 컬럼: 급식은 "평일중식중지=>초1-2,고3", 행복도시락은 "비고"
    _memo_col_name = "평일중식중지=>초1-2,고3" if meal_type == "급식" else "비고"
    _memo_col_idx = col_idx_map.get(_memo_col_name)

    ws = wb[sheet_name]
    row_idx = get_template_data_start_row()
    data_start_row = row_idx
    written_cols: set[int] = set()

    for i, person in enumerate(people):
        breakfast_cnt = person.get("_bf_cnt", 0)
        dinner_cnt = person.get("_dn_cnt", 0)
        lunch_cnt = person.get("_lh_cnt", 0)

        row_data = _map_person_to_row(person, col_mapping, i + 1, template_columns)

        # 식사 건수: 시트에 해당 컬럼이 있을 때만 기록
        if bf_col in col_idx_map:
            row_data[bf_col] = breakfast_cnt
        if dn_col in col_idx_map:
            row_data[dn_col] = dinner_cnt
        if lh_col in col_idx_map:
            row_data[lh_col] = lunch_cnt

        # 배송시작일/종료일: 시트에 컬럼이 있을 때만 기록
        if del_start_col and del_start_col in col_idx_map:
            row_data[del_start_col] = person.get("_delivery_start", "")
        if del_end_col and del_end_col in col_idx_map:
            row_data[del_end_col] = person.get("_delivery_end", "")

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

        # ── 비고/평일중식중지: 접수장 메모로 덮어쓰기 ──
        if _memo_col_idx is not None and person.get("_reception_memo"):
            row_data[_memo_col_name] = person["_reception_memo"]

        # ── Phase 1: 데이터 값 쓰기 ──
        for col_name, value in row_data.items():
            c_idx = col_idx_map.get(col_name)
            if c_idx is None:
                continue
            written_cols.add(c_idx)
            cell = ws.cell(row=row_idx, column=c_idx)

            if c_idx == _memo_col_idx:
                # 비고: 접수장 메모 (plain text)
                txt = str(value) if value else ""
                cell.value = txt
                cell.alignment = _MEMO_ALIGN
                line_count = txt.count("\n") + 1
                est_height = max(17, line_count * 17)
                if est_height > (ws.row_dimensions[row_idx].height or 0):
                    ws.row_dimensions[row_idx].height = est_height
            elif c_idx in _text_cols:
                # 날짜 텍스트 컬럼: 숫자 변환 없이 문자열 그대로
                cell.value = str(value) if value else ""
                cell.number_format = "@"
                cell.alignment = CENTER_ALIGN
            else:
                v = to_numeric(value)
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

    # Phase 2.5: 텍스트 컬럼 number_format 복구 (copy_style이 덮어씀)
    if _text_cols and data_end_row >= data_start_row:
        for r in range(data_start_row, data_end_row + 1):
            for c_idx in _text_cols:
                ws.cell(row=r, column=c_idx).number_format = "@"

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


# ── 시트 분할 계획 ────────────────────────────────


def _get_sheet_plan(
    meal_type: str,
    is_vacation_month: bool,
    roster_data: list[dict],
    template_sheet_names: list[str],
) -> tuple[list[tuple[str, list[dict]]], str | None]:
    """(sheet_plan, rename_prefix)

    sheet_plan: [(template_sheet_name, people_for_this_sheet), ...]
    rename_prefix: "방학_", "학기_", or None (급식 = no rename).

    The last entry in sheet_plan is the "total" sheet whose fill count
    becomes the overall total_count.
    """
    if meal_type == "급식":
        # 급식: 단일 시트에 모든 인원 (방중/학기중 구분 없음)
        active = [
            p
            for p in roster_data
            if p.get("_bf_cnt", 0) > 0
            or p.get("_dn_cnt", 0) > 0
            or p.get("_lh_cnt", 0) > 0
        ]
        return [(template_sheet_names[0], active)], None

    if is_vacation_month:
        # ── 방학 월 ──
        #    학기중: 조식 or 석식 실제 배송 건수 > 0 인원만
        #    방중:   중식 실제 배송 건수 > 0 인원만
        #    총괄:   학기중 ∪ 방중 (셋 다 0이면 제외)
        school_people = [
            p for p in roster_data if p.get("_bf_cnt", 0) > 0 or p.get("_dn_cnt", 0) > 0
        ]
        vac_people = [p for p in roster_data if p.get("_lh_cnt", 0) > 0]
        union_people = [
            p
            for p in roster_data
            if p.get("_bf_cnt", 0) > 0
            or p.get("_dn_cnt", 0) > 0
            or p.get("_lh_cnt", 0) > 0
        ]
        return [
            ("방학_학기중", school_people),
            ("방학_방중", vac_people),
            ("방학_총괄", union_people),
        ], "방학_"

    # ── 일반 월 (행복도시락, 방학 아님) ──
    active = [
        p
        for p in roster_data
        if p.get("_bf_cnt", 0) > 0 or p.get("_dn_cnt", 0) > 0 or p.get("_lh_cnt", 0) > 0
    ]
    return [
        ("학기_학기중", active),
        ("학기_총괄", active),
    ], "학기_"


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
    meal_type: str = "급식",
):
    """roster_data → 청구서템플릿 다중 시트 채워서 output_path 에 저장.

    meal_type="급식": 시트 분할 없이 단일 시트에 모든 인원 출력 (방중/학기중 구분 없음).
    meal_type="행복도시락":
      - 방학 O: 방학_방중(방중=1) / 방학_학기중(방중=0) / 방학_총괄(전체)
      - 방학 X: 학기_총괄(전체) / 학기_학기중(전체)
      출력 시트명은 prefix 제거 (총괄, 학기중, 방중).
    """

    wb = openpyxl.load_workbook(template_path)

    # ── 실제 배송 건수 사전 계산 (플래그만 보지 않고 비고/제외일 반영) ──
    for p in roster_data:
        log.debug(
            "[Split] %s: bf=%d dn=%d lh=%d",
            p.get("성명", "?"),
            p.get("_bf_cnt", 0),
            p.get("_dn_cnt", 0),
            p.get("_lh_cnt", 0),
        )

    # ── 시트 분할 계획에 따라 각 시트 채우기 ──
    sheet_plan, rename_prefix = _get_sheet_plan(
        meal_type, is_vacation_month, roster_data, wb.sheetnames
    )
    for sheet_name, people in sheet_plan:
        total_count = _fill_invoice_sheet(
            wb,
            sheet_name,
            people,
            col_mapping,
            template_path,
            year,
            month,
            unit_price_breakfast,
            unit_price_dinner,
            unit_price_lunch,
            excluded_dates,
            meal_type=meal_type,
        )
    if rename_prefix:
        _rename_sheets(wb, rename_prefix)

    wb.save(output_path)
    return total_count, roster_data
