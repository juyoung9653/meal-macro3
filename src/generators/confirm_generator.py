"""확인서 생성기 — 템플릿 채우기 (배송일 계산은 delivery_calculator 위임)."""

import os
from copy import copy
from datetime import date

import openpyxl

from ..core.meal_types import MealType
from ..core.settings import get_settings
from ..excel.excel_utils import (
    adjust_formula,
    build_merged_anchor_map,
    copy_cell,
)
from ..services.delivery_calculator import (
    calc_weekly_breakdown,
    get_week_ranges,
    to_bool,
    week_date_str,
)
from ..templates.template_scanner import scan_placeholders, validate_placeholders


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


def make_confirm(
    roster_data: list[dict],
    col_mapping: dict,
    output_dir: str,
    template_path: str,
    year: int,
    month: int,
    title_prefix: str = "",
    delivery_person: str = "",
    delivery_phone: str = "",
    excluded_dates: set[date] | None = None,
) -> list[dict]:
    """명단 데이터 → 확인서템플릿 채워서 단일 엑셀 파일 생성."""
    s = get_settings()

    _LOOKUP = ["주소", "성명", "세대주성명", "핸드폰", "관할동"]
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
    created = []

    # 3. 템플릿 워크북 로드
    wb = openpyxl.load_workbook(template_path)
    data_cols = s.get("templates", "data_columns", default=11)

    # 플레이스홀더 위치 스캔 (mtime + 파일 캐시)
    tpl_sheet_names = s.get(
        "templates",
        "confirm_sheet_names",
        default=["1명", "2명", "3명", "4명", "5명"],
    )
    all_placeholders = scan_placeholders(
        template_path,
        tpl_sheet_names,
        cache_path=s.get("cache", "placeholder_cache", default="") or None,
    )

    # ── 템플릿 시트별 MergedCell 앵커맵 사전 빌드 ──
    tpl_anchor_maps = _pre_build_anchor_maps(wb, tpl_sheet_names)  # keep private

    ph_warnings = validate_placeholders(all_placeholders)
    if ph_warnings:
        msg = "템플릿에 필수 플레이스홀더가 누락되었습니다:\n\n" + "\n".join(
            ph_warnings
        )
        msg += "\n\n확인서템플릿.xlsx 파일을 확인 후 다시 실행하세요."
        raise ValueError(msg)

    for dong, persons_in_dong in dong_groups.items():
        # 세대(주소)별 그룹핑
        hh_groups = {}
        for p in persons_in_dong:
            addr = p.get(addr_col, "")
            hh_groups.setdefault(addr, []).append(p)

        # 가구원 수 통계
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

        # 시트 생성
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

        # 페이지 설정
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
        col_widths_set = False

        for addr, members in hh_groups.items():
            n = len(members)
            tpl_key = f"{min(n, 5)}명"
            src = wb[tpl_key]
            tpl_rows = src.max_row

            # 컬럼 너비 복사 + 설정된 값 적용
            if not col_widths_set:
                for col_letter in src.column_dimensions:
                    if src.column_dimensions[col_letter].width:
                        ws.column_dimensions[col_letter].width = copy(
                            src.column_dimensions[col_letter].width
                        )
                col_w_map = {
                    "G": s.get("confirm", "col_width_g", default=12.5),
                    "H": s.get("confirm", "col_width_h", default=12.5),
                    "I": s.get("confirm", "col_width_i", default=12.5),
                    "J": s.get("confirm", "col_width_j", default=12.5),
                }
                for cl, w_val in col_w_map.items():
                    ws.column_dimensions[cl].width = w_val
                col_widths_set = True

            # 템플릿 행 복사
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

            # 병합 셀 복사
            for mr in src.merged_cells.ranges:
                ws.merge_cells(
                    start_row=current_row + mr.min_row - 1,
                    start_column=mr.min_col,
                    end_row=current_row + mr.max_row - 1,
                    end_column=mr.max_col,
                )

            # Formula 행 번호 보정 (tpl_anchor_map으로 O(1) MergedCell 회피)
            tpl_anchor_map = tpl_anchor_maps.get(tpl_key, {})
            for src_r in range(1, tpl_rows + 1):
                dst_r = current_row + src_r - 1
                for src_c in range(1, data_cols + 1):
                    if (src_r, src_c) in tpl_anchor_map:
                        continue  # 비앵커 MergedCell → 건너뜀
                    cell = ws.cell(row=dst_r, column=src_c)
                    if cell.value and str(cell.value).startswith("="):
                        cell.value = adjust_formula(str(cell.value), current_row - 1)

            # 현재 시트의 플레이스홀더 위치 맵
            ph_map = all_placeholders.get(tpl_key, {})

            # 공통 정보 채움 (플레이스홀더 기반, tpl_anchor_map으로 O(1))
            title_suffix = s.get("confirm", "title_suffix", default="분당구 급식확인서")
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
            if delivery_phone:
                _set_ph(
                    ws, ph_map, "d_phone", current_row, delivery_phone, tpl_anchor_map
                )
            _set_ph(ws, ph_map, "addr", current_row, addr, tpl_anchor_map)

            # 각 아동별 채움 (플레이스홀더 기반)
            for ci, person in enumerate(members[:5]):
                sheet_seq += 1

                name = str(person.get(name_col, ""))
                head = str(person.get(head_col, ""))
                phone = str(person.get(phone_col, ""))

                # 개인정보
                _set_ph(
                    ws, ph_map, f"num{ci + 1}", current_row, sheet_seq, tpl_anchor_map
                )
                _set_ph(ws, ph_map, f"child{ci + 1}", current_row, name, tpl_anchor_map)
                _set_ph(ws, ph_map, f"carer{ci + 1}", current_row, head, tpl_anchor_map)
                _set_ph(
                    ws, ph_map, f"phone{ci + 1}", current_row, phone, tpl_anchor_map
                )

                flags = {
                    MealType.BREAKFAST: to_bool(person.get("_breakfast", 0)),
                    MealType.DINNER: to_bool(person.get("_dinner", 0)),
                    MealType.LUNCH: to_bool(person.get("_lunch", 0)),
                }
                note_text = str(person.get("_note", ""))
                weekly = calc_weekly_breakdown(
                    year,
                    month,
                    flags["breakfast"],
                    flags["dinner"],
                    flags["lunch"],
                    note_text,
                    excluded_dates,
                )

                for wi, wdata in enumerate(weeks):
                    wd = (
                        weekly[wi]
                        if wi < len(weekly)
                        else {
                            MealType.BREAKFAST: 0,
                            MealType.DINNER: 0,
                            MealType.LUNCH: 0,
                        }
                    )
                    # 주차 날짜 (예: {week1})
                    _set_ph(
                        ws,
                        ph_map,
                        f"week{wi + 1}",
                        current_row,
                        week_date_str(wdata[1], wdata[2]),
                        tpl_anchor_map,
                    )
                    # 조식 건수
                    _set_ph(
                        ws,
                        ph_map,
                        f"sum_b_{ci + 1}_{wi + 1}",
                        current_row,
                        wd[MealType.BREAKFAST] if wd[MealType.BREAKFAST] else "",
                        tpl_anchor_map,
                    )
                    # 석식 건수
                    _set_ph(
                        ws,
                        ph_map,
                        f"sum_d_{ci + 1}_{wi + 1}",
                        current_row,
                        wd[MealType.DINNER] if wd[MealType.DINNER] else "",
                        tpl_anchor_map,
                    )
                    # 중식 건수
                    _set_ph(
                        ws,
                        ph_map,
                        f"sum_l_{ci + 1}_{wi + 1}",
                        current_row,
                        wd[MealType.LUNCH] if wd[MealType.LUNCH] else "",
                        tpl_anchor_map,
                    )

            current_row += tpl_rows

        # 시트별 통계 추가
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

    # 4. 원본 템플릿 시트 제거
    tpl_sheets = set(
        s.get(
            "templates",
            "confirm_sheet_names",
            default=["1명", "2명", "3명", "4명", "5명"],
        )
    )
    for sn in list(wb.sheetnames):
        if sn in tpl_sheets:
            del wb[sn]

    # 5. 단일 파일 저장
    fname_fmt = s.get(
        "confirm", "output_filename_format", default="확인서_{year:04d}{month:02d}.xlsx"
    )
    fname = fname_fmt.format(year=year, month=month)
    out_path = os.path.join(output_dir, fname)
    wb.save(out_path)

    for item in created:
        item["file"] = fname

    return created
