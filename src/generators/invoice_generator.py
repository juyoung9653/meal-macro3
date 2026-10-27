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
from ..domain.document_column_sources import (
    AUTO_SEQUENCE, AUTO_BREAKFAST, AUTO_LUNCH, AUTO_DINNER, AUTO_TOTAL,
    AUTO_PRICE, AUTO_HISTORY, TEMPLATE_VALUE, invoice_output_defaults,
)
from ..excel.excel_utils import CENTER_ALIGN, adjust_formula, copy_style
from ..excel.publication import WorkbookPublicationResult, publish_workbook
from ..templates.config import (
    read_template_column_indexes,
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
    *, calculated_values: Mapping[str, object] | None = None,
) -> dict:
    """Typed recipient plus template mapping → {template_col_name: value}."""
    row_data: dict[str, object] = {}
    source = {**person.roster.extra, **(calculated_values or {})}
    for col in template_columns:
        roster_key = mapping.get(col, "")
        secondary = (column_fallbacks or {}).get(col, "")
        if roster_key or secondary:
            row_data[col] = resolve_mapping_value(source, roster_key, secondary)
        elif col in mapping:
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
    history_note_column: str | None = None,
) -> int:
    """단일 시트에 people 데이터를 채운다. 작성 건수 반환."""
    template_columns = read_template_columns(template_path, sheet_name=sheet_name)
    col_idx_map = read_template_column_indexes(template_path, sheet_name=sheet_name)

    _memo_col_name = ("평일중식중지=>초1-2,고3" if meal_type == "급식" else "비고") if history_note_column is None else history_note_column
    col_mapping = invoice_output_defaults(col_mapping, template_columns, _memo_col_name)
    count_columns = {
        source: next((col for col in template_columns if col_mapping.get(col) == source), "")
        for source in (AUTO_BREAKFAST, AUTO_DINNER, AUTO_LUNCH)
    }
    preserve_template = object()

    ws = wb[sheet_name]
    row_idx = get_template_data_start_row()
    data_start_row = row_idx
    written_cols: dict[int, set[int]] = {}
    template_values = [ws.cell(data_start_row, c).value for c in range(1, ws.max_column + 1)]

    _write_global_note(
        ws,
        col_idx_map,
        subtotal_row=data_start_row - 1,
        sample_data_row=data_start_row,
        global_note=global_note,
    )

    for ordinal, person in enumerate(people, start=1):
        # References follow calculated meal columns, never user-replaced values.
        # When a count column is absent, use the original calculated facts.
        has_counts = all(count_columns.values())
        amount = person.amount(prices)
        total = person.total_count
        if has_counts:
            references = [f"{_col_idx_to_letter(col_idx_map[col])}{row_idx}" for col in count_columns.values()]
            total = "=" + "+".join(references)
            if person.billing_amount is None:
                amount = _build_price_formula(col_idx_map, count_columns[AUTO_BREAKFAST],
                    count_columns[AUTO_DINNER], count_columns[AUTO_LUNCH], row_idx, prices)
        calculated = {
            AUTO_SEQUENCE: ordinal, AUTO_BREAKFAST: person.breakfast_count,
            AUTO_DINNER: person.dinner_count, AUTO_LUNCH: person.lunch_count,
            AUTO_TOTAL: total, AUTO_PRICE: amount, AUTO_HISTORY: person.note,
            TEMPLATE_VALUE: preserve_template,
        }
        source_values = {**person.roster.extra, **calculated}
        selected_sources = {
            target: (primary if resolve_mapping_value(source_values, primary) != ""
                     else (column_fallbacks or {}).get(target, ""))
            for target, primary in col_mapping.items() if target in col_idx_map
        }
        row_data = _map_person_to_row(person, col_mapping, template_columns,
            column_fallbacks, calculated_values=calculated)
        row_data = {target: value for target, value in row_data.items() if value is not preserve_template}
        numeric_sources = {AUTO_BREAKFAST, AUTO_DINNER, AUTO_LUNCH, AUTO_TOTAL, AUTO_PRICE}
        # ── Phase 1: 데이터 값 쓰기 ──
        for col_name, value in row_data.items():
            c_idx = col_idx_map.get(col_name)
            if c_idx is None:
                continue
            written_cols.setdefault(row_idx, set()).add(c_idx)
            cell = ws.cell(row=row_idx, column=c_idx)

            selected = selected_sources.get(col_name)
            if selected in (AUTO_HISTORY, "비고") or (
                col_name in ("비고", "평일중식중지=>초1-2,고3")
                and selected not in numeric_sources | {AUTO_SEQUENCE}
            ):
                # 비고: 명단 원본의 plain text
                txt = str(value) if value is not None else ""
                cell.value = txt
                cell.alignment = _MEMO_ALIGN
                line_count = txt.count("\n") + 1
                est_height = max(17, line_count * 17)
                if est_height > (ws.row_dimensions[row_idx].height or 0):
                    ws.row_dimensions[row_idx].height = est_height
            elif selected_sources.get(col_name) == AUTO_SEQUENCE:
                # 연번은 이 시트에 실제 출력한 행 순서다.
                cell.value = value
                cell.number_format = "0"
                cell.alignment = CENTER_ALIGN
            else:
                v = to_numeric(value)
                cell.value = None if selected_sources.get(col_name) in numeric_sources and isinstance(v, (int, float)) and v == 0 else v
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
                if c_idx not in written_cols.get(r, set()) and template_values[c_idx - 1] is not None:
                    val = template_values[c_idx - 1]
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
    overwrite: bool = True,
    column_fallbacks: Mapping[str, str] | None = None,
    history_note_column: str | None = None,
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
                history_note_column=history_note_column,
            )
        if rename_prefix:
            _rename_sheets(wb, rename_prefix)

        publication_started = True
        publication = publish_workbook(wb, output_path, password=output_password, overwrite=overwrite)
        return total_count, publication
    finally:
        if not publication_started:
            wb.close()
