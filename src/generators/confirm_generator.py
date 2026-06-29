"""확인서 생성기 — 템플릿 채우기 (db.xlsx 기반 배송 데이터 활용)."""

import logging
import os
from copy import copy
from datetime import date, timedelta

import openpyxl
from openpyxl.utils import get_column_letter

from ..core.date_utils import get_week_ranges, to_bool, week_date_str
from ..core.meal_types import MealType
from ..core.settings import get_settings
from ..excel._parsing import _is_date_col
from ..excel.db_reader import get_db_reader
from ..excel.excel_utils import adjust_formula, build_merged_anchor_map, copy_cell
from ..templates.template_scanner import scan_placeholders, validate_placeholders

log = logging.getLogger(__name__)


def _set_ph(ws, ph_map, key, current_row, value, tpl_anchor_map=None):
    """ph_map[key] 위치들에 value 쓰기. tpl_anchor_map 있으면 O(1) 병합셀 처리."""
    for ph_row, ph_col in ph_map.get(key, []):
        out_row = current_row + ph_row - 1
        out_col = ph_col
        if tpl_anchor_map and (ph_row, ph_col) in tpl_anchor_map:
            ar, ac = tpl_anchor_map[(ph_row, ph_col)]
            ws.cell(row=current_row + ar - 1, column=ac).value = value
        else:
            ws.cell(row=out_row, column=out_col).value = value


def _pre_build_anchor_maps(wb, tpl_sheet_names: list[str]) -> dict[str, dict]:
    """템플릿 시트별 MergedCell → 앵커 매핑 사전 빌드."""
    maps = {}
    for sn in tpl_sheet_names:
        if sn in wb.sheetnames:
            maps[sn] = build_merged_anchor_map(wb[sn])
    return maps


def _calc_weekly_breakdown_from_daily(
    year: int,
    month: int,
    person: dict,
    weeks: list,
    week_date_map: dict[date, int] | None = None,
) -> list[dict]:
    """db.xlsx 배송전체의 멀티 행(식종별) 데이터를 주차별로 집계.

    person dict에 포함된 멀티 행 raw 데이터(_delivery_rows_raw 필드)를 기준으로
    각 주차별 조식/석식/중식 건수를 계산.
    식종 정보는 각 배송 행의 '식종' 컬럼에서 가져온다.
    """
    bf_flag = to_bool(person.get("_breakfast", 0))
    dn_flag = to_bool(person.get("_dinner", 0))
    lh_flag = to_bool(person.get("_lunch", 0))

    # 활성화된 식종 플래그가 없으면 전부 0
    if not (bf_flag or dn_flag or lh_flag):
        return [
            {MealType.BREAKFAST: 0, MealType.DINNER: 0, MealType.LUNCH: 0}
            for _ in weeks
        ]

    # 주차별 카운트 초기화
    week_counts = []
    for wn, wstart, wend in weeks:
        week_counts.append(
            {MealType.BREAKFAST: 0, MealType.DINNER: 0, MealType.LUNCH: 0}
        )

    # ── 멀티 행 raw 데이터 처리 (식종별로 여러 행이 있을 수 있음) ──
    delivery_rows = person.get("_delivery_rows_raw", [])
    if not delivery_rows:
        # fallback: 기존 _daily 필드가 있으면 단일 행처럼 처리
        daily = person.get("_daily", {})
        if not daily:
            log.warning(
                "[_calc_weekly] NO _delivery_rows_raw AND NO _daily → all zeros"
            )
            return week_counts
        delivery_rows = [{"_daily": daily, "식종": person.get("식종", "")}]
        log.warning(
            "[_calc_weekly] fallback: using _daily=%d items, 식종=%r",
            len(daily),
            person.get("식종", ""),
        )

    for row_data in delivery_rows:
        row_meal_type = str(row_data.get("식종", "")).strip()

        # 식종 → MealType 매핑
        target = MealType.from_name(row_meal_type)
        if target is None:
            # 식종 미상 → 플래그 보고 유추
            if bf_flag:
                target = MealType.BREAKFAST
            elif dn_flag:
                target = MealType.DINNER
            elif lh_flag:
                target = MealType.LUNCH
            else:
                continue

        # 해당 식종이 비활성이면 건너뜀
        if target == MealType.BREAKFAST and not bf_flag:
            continue
        if target == MealType.DINNER and not dn_flag:
            continue
        if target == MealType.LUNCH and not lh_flag:
            continue

        # daily 데이터 (날짜 컬럼 → "O"/"X")
        daily = row_data.get("_daily", {})
        if not daily:
            continue

        for date_key, val in daily.items():
            if not to_bool(val):
                continue

            if not _is_date_col(date_key):
                continue
            parsed = date_key.split("/")
            try:
                m_from_key = int(parsed[0])
                d_from_key = int(parsed[1].split()[0])
            except (ValueError, IndexError):
                log.warning("[_calc_weekly] Failed to parse date_key=%r", date_key)
                continue

            if m_from_key != month:
                continue

            try:
                d = date(year, month, d_from_key)
            except ValueError:
                continue

            # 해당 날짜가 속한 주차 찾기 (O(1) lookup)
            wi = week_date_map.get(d) if week_date_map else None
            if wi is not None:
                week_counts[wi][target] += 1
            else:
                # fallback: 선형 탐색
                for wi, (wn, wstart, wend) in enumerate(weeks):
                    if wstart <= d <= wend:
                        week_counts[wi][target] += 1
                        break

    return week_counts


def make_confirm(
    roster_data: list[dict],
    col_mapping: dict,
    output_dir: str,
    template_path: str,
    year: int,
    month: int,
    title_prefix: str = "",
    excluded_dates: set[date] | None = None,
    meal_type: str = "급식",
) -> list[dict]:
    """명단 데이터 → 확인서템플릿 채워서 단일 엑셀 파일 생성."""
    s = get_settings()

    _LOOKUP = ["도로명주소", "성명", "세대주\n성명", "핸드폰", "관할동"]
    addr_col, name_col, head_col, phone_col, dong_col = (
        col_mapping.get(k, k) for k in _LOOKUP
    )

    # 1. 주소 줄바꿈 → 공백
    for p in roster_data:
        raw = str(p.get(addr_col, ""))
        p[addr_col] = raw.replace("\n", " ").replace("\r", " ").strip()

    # 2. 관할동별 그룹핑
    dong_groups = {}
    for p in roster_data:
        dong = str(p.get(dong_col, "")).strip()
        dong_groups.setdefault(dong, []).append(p)

    if not title_prefix:
        title_prefix = f"{year}년 {month}월"

    weeks = get_week_ranges(year, month)

    # ── 주차별 날짜 맵 precompute (O(1) lookup) ──
    week_date_map: dict[date, int] = {}
    for wi, (_wn, wstart, wend) in enumerate(weeks):
        d = wstart
        while d <= wend:
            week_date_map[d] = wi
            d += timedelta(days=1)

    created = []

    # 3. 템플릿 워크북 로드
    wb = openpyxl.load_workbook(template_path)
    data_cols = s.get("templates", "data_columns")

    tpl_sheet_names = s.get("templates", "confirm_sheet_names")
    all_placeholders = scan_placeholders(
        template_path,
        tpl_sheet_names,
    )

    tpl_anchor_maps = _pre_build_anchor_maps(wb, tpl_sheet_names)

    ph_warnings = validate_placeholders(all_placeholders, meal_type=meal_type)
    if ph_warnings:
        msg = "템플릿에 필수 플레이스홀더가 누락되었습니다:\n\n" + "\n".join(
            ph_warnings
        )
        msg += "\n\n확인서템플릿.xlsx 파일을 확인 후 다시 실행하세요."
        raise ValueError(msg)

    # ── DbReader에서 배송 데이터 로드 ──
    # 모든 person이 _delivery_rows_raw를 가지고 있으면 delivery_matrix는 skip (최적화)
    need_matrix = any(
        "_delivery_rows_raw" not in p or not p["_delivery_rows_raw"]
        for p in roster_data
    )

    delivery_by_id: dict[str, list[dict]] = {}

    try:
        reader = get_db_reader()
        if need_matrix:
            delivery_matrix = reader.read_delivery_matrix()
        else:
            delivery_matrix = []
    except Exception:
        delivery_matrix = []

    if need_matrix:
        # ── person_id → [rows] 멀티 행 맵 (식종별 여러 행 허용, _daily 미리 계산) ──
        for row in delivery_matrix:
            pid = str(row.get("ID", "")).strip()
            if pid:
                enriched = dict(row)
                enriched["_daily"] = {c: v for c, v in row.items() if _is_date_col(c)}
                delivery_by_id.setdefault(pid, []).append(enriched)

    # 배송연락처: 배송정보 테이블 헤더에서 연락처/배송담당 컬럼 읽기
    delivery_person = ""
    delivery_phone = ""
    try:
        for row in reader.read_delivery_order():
            p = row.get("배송담당", "").strip()
            ph = row.get("연락처", "").strip()
            if p or ph:
                delivery_person = p
                delivery_phone = ph
                break
    except Exception:
        pass

    for dong, persons_in_dong in dong_groups.items():
        hh_groups = {}
        for p in persons_in_dong:
            addr = p.get(addr_col, "")
            hh_groups.setdefault(addr, []).append(p)

        hh_1 = hh_2 = hh_3 = hh_4 = hh_5 = 0
        for addr, members in hh_groups.items():
            n = len(members)
            if n == 1:
                hh_1 += 1
            elif n == 2:
                hh_2 += 1
            elif n == 3:
                hh_3 += 1
            elif n == 4:
                hh_4 += 1
            else:
                hh_5 += 1

        import re as _re

        safe_dong = _re.sub(r"[\\/?*\[\]:]", "", dong.replace(" ", "_"))[:20]
        if not safe_dong:
            safe_dong = "기타"
        sheet_name = safe_dong
        if sheet_name in wb.sheetnames:
            for i in range(2, 100):
                cand = f"{safe_dong}_{i}"
                if cand not in wb.sheetnames:
                    sheet_name = cand
                    break

        ws = wb.create_sheet(title=sheet_name)

        ws.page_setup.orientation = "landscape"
        ws.page_setup.fitToWidth = 1
        ws.page_setup.fitToHeight = 0
        ws.page_setup.fitToPage = True
        ws.page_setup.paperSize = 9
        for side in ("left", "right", "top", "bottom"):
            setattr(ws.page_margins, side, 0.3 / 2.54)
        ws.page_margins.header = 0
        ws.page_margins.footer = 0

        current_row = 1
        sheet_seq = 0
        col_widths_copied = False

        for addr, members in hh_groups.items():
            n = len(members)
            tpl_key = f"{min(n, 5)}명"
            src = wb[tpl_key]
            tpl_rows = src.max_row

            if not col_widths_copied:
                # ── 데이터 있는 열(data_cols)까지만 컬럼 너비 복사 ──
                for _key, src_cd in src.column_dimensions.items():
                    if not src_cd.width:
                        continue
                    c_min = src_cd.min or 1
                    c_max = min(src_cd.max or c_min, data_cols)
                    for ci in range(c_min, c_max + 1):
                        letter = get_column_letter(ci)
                        ws.column_dimensions[letter].width = src_cd.width
                        ws.column_dimensions[letter].hidden = src_cd.hidden
                # 시트 수준 기본 너비도 복사
                try:
                    if src.sheet_format.defaultColWidth:
                        ws.sheet_format.defaultColWidth = (
                            src.sheet_format.defaultColWidth
                        )
                except Exception:
                    pass
                col_widths_copied = True

            # 셀 복사
            for src_r in range(1, tpl_rows + 1):
                dst_r = current_row + src_r - 1
                for src_c in range(1, data_cols + 1):
                    copy_cell(
                        src.cell(row=src_r, column=src_c),
                        ws.cell(row=dst_r, column=src_c),
                    )
                if src_r in src.row_dimensions:
                    ws.row_dimensions[dst_r].height = copy(
                        src.row_dimensions[src_r].height
                    )

            for mr in src.merged_cells.ranges:
                ws.merge_cells(
                    start_row=current_row + mr.min_row - 1,
                    start_column=mr.min_col,
                    end_row=current_row + mr.max_row - 1,
                    end_column=mr.max_col,
                )
            tpl_anchor_map = tpl_anchor_maps.get(tpl_key, {})
            for src_r in range(1, tpl_rows + 1):
                dst_r = current_row + src_r - 1
                for src_c in range(1, data_cols + 1):
                    if (src_r, src_c) in tpl_anchor_map:
                        continue
                    cell = ws.cell(row=dst_r, column=src_c)
                    if cell.value and str(cell.value).startswith("="):
                        cell.value = adjust_formula(str(cell.value), current_row - 1)

            ph_map = all_placeholders.get(tpl_key, {})

            title_suffix = s.get("confirm", "title_suffix")
            if meal_type == "급식":
                title_suffix = title_suffix.replace("분당구", "수정구")
            _set_ph(
                ws,
                ph_map,
                "title",
                current_row,
                f"{title_prefix} {title_suffix}",
                tpl_anchor_map,
            )
            if delivery_person:
                _set_ph(
                    ws, ph_map, "delivery", current_row, delivery_person, tpl_anchor_map
                )
            if meal_type != "급식" and delivery_phone:
                _set_ph(
                    ws, ph_map, "d_phone", current_row, delivery_phone, tpl_anchor_map
                )
            _set_ph(ws, ph_map, "addr", current_row, addr, tpl_anchor_map)

            # ── 급식 확인서: dong(관할동) 별도 플레이스홀더 ──
            if meal_type == "급식":
                _set_ph(ws, ph_map, "dong", current_row, dong, tpl_anchor_map)

            hh_notes: list[str] = []
            for p in members:
                nt = str(p.get("_note", "")).strip()
                if nt and nt not in hh_notes:
                    hh_notes.append(nt)
            combined_note = "\n".join(hh_notes)
            _set_ph(ws, ph_map, "memo", current_row, combined_note, tpl_anchor_map)

            # ── 번호: 급식은 가구단위 1개(num1), 행복도시락은 사람별(num1~numN) ──
            if meal_type == "급식":
                sheet_seq += 1

            for ci, person in enumerate(members[:5]):
                if meal_type != "급식":
                    sheet_seq += 1

                name = str(person.get(name_col, ""))
                head = str(person.get(head_col, ""))
                phone = str(person.get(phone_col, ""))

                if meal_type == "급식" and ci == 0:
                    _set_ph(ws, ph_map, "num1", current_row, sheet_seq, tpl_anchor_map)
                elif meal_type != "급식":
                    _set_ph(
                        ws,
                        ph_map,
                        f"num{ci + 1}",
                        current_row,
                        sheet_seq,
                        tpl_anchor_map,
                    )
                _set_ph(ws, ph_map, f"child{ci + 1}", current_row, name, tpl_anchor_map)
                _set_ph(ws, ph_map, f"carer{ci + 1}", current_row, head, tpl_anchor_map)
                _set_ph(
                    ws, ph_map, f"phone{ci + 1}", current_row, phone, tpl_anchor_map
                )

                # ── 배송 데이터: 멀티 행(식종별) 주차별 집계 ──
                person_id = str(person.get("ID", "")) or str(person.get("id", ""))

                # delivery_by_id에서 이 사람의 모든 행(식종별) 가져오기 (_daily는 이미 포함)
                # 이미 InvoiceData를 통해 _delivery_rows_raw가 있다면 overwrite하지 않음
                if (
                    "_delivery_rows_raw" not in person
                    or not person["_delivery_rows_raw"]
                ):
                    person["_delivery_rows_raw"] = (
                        delivery_by_id.get(person_id, []) if person_id else []
                    )

                weekly_counts = _calc_weekly_breakdown_from_daily(
                    year, month, person, weeks, week_date_map
                )

                for wi, wdata in enumerate(weeks):
                    wd = (
                        weekly_counts[wi]
                        if wi < len(weekly_counts)
                        else {
                            MealType.BREAKFAST: 0,
                            MealType.DINNER: 0,
                            MealType.LUNCH: 0,
                        }
                    )
                    _set_ph(
                        ws,
                        ph_map,
                        f"week{wi + 1}",
                        current_row,
                        week_date_str(wdata[1], wdata[2]),
                        tpl_anchor_map,
                    )
                    _set_ph(
                        ws,
                        ph_map,
                        f"sum_b_{ci + 1}_{wi + 1}",
                        current_row,
                        wd[MealType.BREAKFAST] if wd[MealType.BREAKFAST] else "",
                        tpl_anchor_map,
                    )
                    _set_ph(
                        ws,
                        ph_map,
                        f"sum_d_{ci + 1}_{wi + 1}",
                        current_row,
                        wd[MealType.DINNER] if wd[MealType.DINNER] else "",
                        tpl_anchor_map,
                    )
                    _set_ph(
                        ws,
                        ph_map,
                        f"sum_l_{ci + 1}_{wi + 1}",
                        current_row,
                        wd[MealType.LUNCH] if wd[MealType.LUNCH] else "",
                        tpl_anchor_map,
                    )

            current_row += tpl_rows

        created.append(
            {
                "sheet_name": sheet_name,
                "total_hh": hh_1 + hh_2 + hh_3 + hh_4 + hh_5,
                "count_1": hh_1,
                "count_2": hh_2,
                "count_3": hh_3,
                "count_4": hh_4,
                "count_5": hh_5,
                "file": "",
            }
        )

    tpl_sheets = set(s.get("templates", "confirm_sheet_names"))
    for sn in list(wb.sheetnames):
        if sn in tpl_sheets:
            del wb[sn]

    fname_fmt = s.get("confirm", "output_filename_format")
    fname = fname_fmt.format(year=year, month=month)
    out_path = os.path.join(output_dir, fname)
    wb.save(out_path)

    for item in created:
        item["file"] = fname

    return created
