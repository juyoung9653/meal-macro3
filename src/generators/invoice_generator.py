"""계산이 끝난 청구 데이터를 기존 Excel 템플릿에 렌더링한다.

방학 월: roster_data → 방학_방중(방중=1) / 방학_학기중(방중=0) / 방학_총괄(전체)
일반 월: roster_data → 학기_총괄(전체) / 학기_학기중(전체)
출력 시트명: prefix(방학_/학기_) 제거 → 총괄 / 학기중 / 방중

급식 유형: 시트 분할 없이 한 시트로 모든 인원 출력.
"""

import logging
import math
from collections.abc import Mapping
from copy import copy

import openpyxl
from openpyxl.styles import Alignment

from ..core.diff_format import to_numeric
from ..core.invoice_data import (
    MealPrices,
    MonthlyDelivery,
    MonthlyDeliveryBatch,
)
from .mapping_values import resolve_mapping_value
from ..excel.excel_utils import CENTER_ALIGN, adjust_formula, copy_style
from ..excel.publication import WorkbookPublicationResult, publish_workbook
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
    person: MonthlyDelivery,
    mapping: dict,
    template_columns: list[str],
    column_fallbacks: Mapping[str, str] | None = None,
) -> dict:
    """Typed recipient plus template mapping → {template_col_name: value}."""
    row_data: dict[str, object] = {}
    source = person.roster.extra
    calculated = {value for key, value in mapping.items() if key.startswith("__calc_")}
    for col in template_columns:
        if col == "연번":
            row_data[col] = person.delivery_sequence
            continue
        roster_key = mapping.get(col, "")
        secondary = (column_fallbacks or {}).get(col, "")
        if roster_key or secondary:
            row_data[col] = resolve_mapping_value(source, roster_key, secondary)
        elif col in mapping and col not in calculated and col != "제공기관":
            row_data[col] = ""
    return row_data


def _build_price_formula(
    col_idx_map: dict[str, int],
    bf_col: str,
    dn_col: str,
    lh_col: str,
    row_idx: int,
    prices: MealPrices,
) -> str:
    """시트에 존재하는 식사 컬럼만 포함한 청구금액 수식 생성."""
    parts: list[str] = []

    bf_idx = col_idx_map.get(bf_col)
    if bf_idx is not None:
        parts.append(f"{_col_idx_to_letter(bf_idx)}{row_idx}*{prices.breakfast}")

    dn_idx = col_idx_map.get(dn_col)
    if dn_idx is not None:
        parts.append(f"{_col_idx_to_letter(dn_idx)}{row_idx}*{prices.dinner}")

    lh_idx = col_idx_map.get(lh_col)
    if lh_idx is not None:
        parts.append(f"{_col_idx_to_letter(lh_idx)}{row_idx}*{prices.lunch}")

    if not parts:
        return "0"
    return "=" + "+".join(parts)


_MEMO_ALIGN = Alignment(horizontal="left", vertical="top", wrap_text=True)


def _fill_invoice_sheet(
    wb,
    sheet_name: str,
    people: list[MonthlyDelivery],
    col_mapping: dict,
    template_path: str,
    prices: MealPrices,
    meal_type: str = "급식",
    global_note: str | None = None,
    column_fallbacks: Mapping[str, str] | None = None,
) -> int:
    """단일 시트에 people 데이터를 채운다. 작성 건수 반환."""
    template_columns = read_template_columns(template_path, sheet_name=sheet_name)
    col_idx_map = col_name_to_index(template_columns)

    bf_col = col_mapping.get("__calc_breakfast", "조식")
    dn_col = col_mapping.get("__calc_dinner", "석식")
    lh_col = col_mapping.get("__calc_lunch", "중식")
    price_col = col_mapping.get("__calc_price", "청구금액")
    sequence_col_idx = col_idx_map.get("연번")

    # 메모 컬럼은 명단에서 정상 매핑된 값을 쓰되 좌측/줄바꿈 서식을 적용한다.
    _memo_col_name = "평일중식중지=>초1-2,고3" if meal_type == "급식" else "비고"
    _memo_col_idx = col_idx_map.get(_memo_col_name)

    ws = wb[sheet_name]
    row_idx = get_template_data_start_row()
    data_start_row = row_idx
    written_cols: set[int] = set()

    _write_global_note(
        ws,
        col_idx_map,
        subtotal_row=data_start_row - 1,
        sample_data_row=data_start_row,
        global_note=global_note,
    )

    for person in people:
        breakfast_cnt = person.breakfast_count
        dinner_cnt = person.dinner_count
        lunch_cnt = person.lunch_count

        row_data = _map_person_to_row(person, col_mapping, template_columns, column_fallbacks)
        mapped_targets = set(row_data) - {bf_col, dn_col, lh_col, price_col, col_mapping.get("__calc_total", "계")}

        # 식사 건수: 시트에 해당 컬럼이 있을 때만 기록
        if bf_col in col_idx_map:
            row_data[bf_col] = breakfast_cnt
        if dn_col in col_idx_map:
            row_data[dn_col] = dinner_cnt
        if lh_col in col_idx_map:
            row_data[lh_col] = lunch_cnt



        # 단일 단가면 기존 수식을 유지하고, 월중 단가 변경이 있으면
        # 서비스일별 계산이 끝난 정확한 금액을 숫자로 기록한다.
        if price_col in col_idx_map:
            row_data[price_col] = (
                person.billing_amount
                if person.billing_amount is not None
                else _build_price_formula(
                    col_idx_map,
                    bf_col,
                    dn_col,
                    lh_col,
                    row_idx,
                    prices,
                )
            )



        # ── Phase 1: 데이터 값 쓰기 ──
        for col_name, value in row_data.items():
            c_idx = col_idx_map.get(col_name)
            if c_idx is None:
                continue
            written_cols.add(c_idx)
            cell = ws.cell(row=row_idx, column=c_idx)

            if c_idx == _memo_col_idx:
                # 비고: 명단 원본의 plain text
                txt = str(value) if value is not None else ""
                cell.value = txt
                cell.alignment = _MEMO_ALIGN
                line_count = txt.count("\n") + 1
                est_height = max(17, line_count * 17)
                if est_height > (ws.row_dimensions[row_idx].height or 0):
                    ws.row_dimensions[row_idx].height = est_height
            elif c_idx == sequence_col_idx:
                # 직접 배송 연번은 선행 0을 포함한 원본 표시값을 보존한다.
                cell.value = "" if value is None else str(value)
                cell.number_format = "@"
                cell.alignment = CENTER_ALIGN
            else:
                v = to_numeric(value)
                cell.value = None if col_name not in mapped_targets and isinstance(v, (int, float)) and v == 0 else v
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


def _write_global_note(
    ws,
    col_idx_map: Mapping[str, int],
    *,
    subtotal_row: int,
    sample_data_row: int,
    global_note: str | None,
) -> None:
    """Write one document-wide note in the template's subtotal-row note cell."""

    if global_note is None:
        return
    note_col_idx = col_idx_map.get("비고")
    if note_col_idx is None:
        raise ValueError(f"{ws.title} 시트에 전체 비고 열이 없습니다.")

    cell = ws.cell(row=subtotal_row, column=note_col_idx)
    sample = ws.cell(row=sample_data_row, column=note_col_idx)
    cell.value = global_note
    cell.font = copy(sample.font)
    cell.alignment = _MEMO_ALIGN

    column_letter = _col_idx_to_letter(note_col_idx)
    column_width = ws.column_dimensions[column_letter].width or 13
    visual_length = sum(2 if ord(character) > 127 else 1 for character in global_note)
    estimated_lines = max(
        global_note.count("\n") + 1,
        math.ceil(visual_length / max(column_width, 1)),
    )
    current_height = ws.row_dimensions[subtotal_row].height or 15
    ws.row_dimensions[subtotal_row].height = max(current_height, estimated_lines * 15)


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
    roster_data: tuple[MonthlyDelivery, ...],
    template_sheet_names: list[str],
) -> tuple[list[tuple[str, list[MonthlyDelivery]]], str | None]:
    """(sheet_plan, rename_prefix)

    sheet_plan: [(template_sheet_name, people_for_this_sheet), ...]
    rename_prefix: "방학_", "학기_", or None (급식 = no rename).

    The last entry in sheet_plan is the "total" sheet whose fill count
    becomes the overall total_count.
    """
    if meal_type == "급식":
        # 급식: 단일 시트에 모든 인원 (방중/학기중 구분 없음)
        active = [p for p in roster_data if p.total_count > 0]
        return [(template_sheet_names[0], active)], None

    if is_vacation_month:
        # ── 방학 월 ──
        #    학기중: 조식 or 석식 실제 배송 건수 > 0 인원만
        #    방중:   중식 실제 배송 건수 > 0 인원만
        #    총괄:   학기중 ∪ 방중 (셋 다 0이면 제외)
        school_people = [
            p for p in roster_data if p.breakfast_count > 0 or p.dinner_count > 0
        ]
        vac_people = [p for p in roster_data if p.lunch_count > 0]
        union_people = [p for p in roster_data if p.total_count > 0]
        return [
            ("방학_학기중", school_people),
            ("방학_방중", vac_people),
            ("방학_총괄", union_people),
        ], "방학_"

    # ── 일반 월 (행복도시락, 방학 아님) ──
    active = [p for p in roster_data if p.total_count > 0]
    return [
        ("학기_학기중", active),
        ("학기_총괄", active),
    ], "학기_"


# ── 메인 진입점 ────────────────────────────────────


def make_invoice_publication(
    batch: MonthlyDeliveryBatch,
    col_mapping: dict,
    output_path: str,
    template_path: str,
    *,
    meal_type: str = "급식",
    global_note: str | None = None,
    output_password: str | None = None,
    column_fallbacks: Mapping[str, str] | None = None,
) -> tuple[int, WorkbookPublicationResult]:
    """월별 배송 배치 → 청구서템플릿 다중 시트 채워서 output_path 에 저장.

    meal_type="급식": 시트 분할 없이 단일 시트에 모든 인원 출력 (방중/학기중 구분 없음).
    meal_type="행복도시락":
      - 방학 O: 방학_방중(방중=1) / 방학_학기중(방중=0) / 방학_총괄(전체)
      - 방학 X: 학기_총괄(전체) / 학기_학기중(전체)
      출력 시트명은 prefix 제거 (총괄, 학기중, 방중).
    """

    if global_note is not None:
        if not isinstance(global_note, str):
            raise TypeError("전체 비고는 문자열이어야 합니다.")
        global_note = global_note.strip() or None

    wb = openpyxl.load_workbook(template_path)
    publication_started = False
    try:
        for p in batch.deliveries:
            log.debug(
                "[Split] %s: bf=%d dn=%d lh=%d",
                p.roster.display_name,
                p.breakfast_count,
                p.dinner_count,
                p.lunch_count,
            )

        # ── 시트 분할 계획에 따라 각 시트 채우기 ──
        sheet_plan, rename_prefix = _get_sheet_plan(
            meal_type, batch.is_vacation_month, batch.deliveries, wb.sheetnames
        )
        total_count = 0
        for sheet_name, people in sheet_plan:
            total_count = _fill_invoice_sheet(
                wb,
                sheet_name,
                people,
                col_mapping,
                template_path,
                batch.prices,
                meal_type=meal_type,
                global_note=global_note,
                column_fallbacks=column_fallbacks,
            )
        if rename_prefix:
            _rename_sheets(wb, rename_prefix)

        publication_started = True
        publication = publish_workbook(wb, output_path, password=output_password)
        return total_count, publication
    finally:
        if not publication_started:
            wb.close()
