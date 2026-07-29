"""청구 서비스가 준비한 데이터를 기존 확인서 템플릿에 렌더링한다."""

import logging
import os
from collections.abc import Mapping, Sequence
from copy import copy

import openpyxl
from openpyxl.utils import get_column_letter

from ..core.settings import get_settings
from ..excel.excel_utils import adjust_formula, build_merged_anchor_map, copy_cell
from ..templates.template_scanner import scan_placeholders, validate_placeholders

log = logging.getLogger(__name__)


def _blank_text(value: object) -> str:
    if value is None:
        return ""
    text = str(value)
    return "" if not text.strip() else text


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


def _prepared_weekly_counts(person: Mapping[str, object]) -> dict[int, dict]:
    """서비스가 준비한 주차 집계를 검증해 주차 번호로 인덱싱한다."""
    raw_counts = person.get("weekly_meal_counts")
    if not isinstance(raw_counts, list):
        shown = person.get("ID") or person.get("성명") or "알 수 없는 대상자"
        raise TypeError(
            f"{shown}: 청구서의 weekly_meal_counts 데이터가 없습니다. "
            "청구서를 먼저 다시 생성하세요."
        )

    indexed: dict[int, dict] = {}
    for item in raw_counts:
        if not isinstance(item, dict):
            raise TypeError("weekly_meal_counts 항목은 매핑이어야 합니다.")
        week = item.get("week")
        if not isinstance(week, int) or isinstance(week, bool) or not 1 <= week <= 5:
            raise ValueError(f"유효하지 않은 확인서 주차입니다: {week!r}")
        if week in indexed:
            raise ValueError(f"확인서 주차가 중복되었습니다: {week}주")
        for meal in ("조식", "중식", "석식"):
            value = item.get(meal, 0)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{week}주 {meal} 건수는 0 이상의 정수여야 합니다.")
        _format_week_range(item)
        indexed[week] = item
    return indexed


def _format_week_range(item: dict) -> str:
    start = str(item.get("start_date", ""))
    end = str(item.get("end_date", ""))
    if not (len(start) == len(end) == 8 and start.isdigit() and end.isdigit()):
        raise ValueError("주차 시작일/종료일은 YYYYMMDD 형식이어야 합니다.")
    return f"{start[4:6]}.{start[6:8]}~{end[4:6]}.{end[6:8]}"


def _household_delivery_staff(
    members: Sequence[Mapping[str, object]],
) -> str:
    unique: list[str] = []
    for member in members:
        for value in _blank_text(member.get("배송담당", "")).split(" / "):
            staff = value.strip()
            if staff and staff not in unique:
                unique.append(staff)
    return " / ".join(unique)


def make_confirm(
    roster_data: Sequence[Mapping[str, object]],
    col_mapping: Mapping[str, str],
    output_dir: str,
    template_path: str,
    year: int,
    month: int,
    title_prefix: str = "",
    meal_type: str = "급식",
) -> list[dict]:
    """명단 데이터 → 확인서템플릿 채워서 단일 엑셀 파일 생성."""
    s = get_settings()

    _LOOKUP = ["도로명주소", "성명", "세대주\n성명", "핸드폰", "관할동"]
    addr_col, name_col, head_col, phone_col, dong_col = (
        col_mapping.get(k, k) for k in _LOOKUP
    )

    # 1. 입력을 복사하면서 주소와 blank 값을 정규화한다.
    normalized_roster: list[dict[str, object]] = []
    for source in roster_data:
        person = dict(source)
        raw = _blank_text(person.get(addr_col, ""))
        person[addr_col] = raw.replace("\n", " ").replace("\r", " ").strip()
        normalized_roster.append(person)

    # 2. 관할동별 그룹핑
    dong_groups = {}
    for p in normalized_roster:
        dong = _blank_text(p.get(dong_col, "")).strip()
        dong_groups.setdefault(dong, []).append(p)

    if not title_prefix:
        title_prefix = f"{year}년 {month}월"

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

    for dong, persons_in_dong in dong_groups.items():
        hh_groups = {}
        for p in persons_in_dong:
            addr = _blank_text(p.get(addr_col, ""))
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
                for src_cd in src.column_dimensions.values():
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
                except (AttributeError, TypeError, ValueError) as error:
                    log.debug("템플릿 기본 열 너비 복사 생략: %s", error)
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
            delivery_person = _household_delivery_staff(members)

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
            _set_ph(
                ws, ph_map, "delivery", current_row, delivery_person, tpl_anchor_map
            )
            # 새 연간 스키마에는 배송 연락처가 없으므로 플레이스홀더를 비운다.
            _set_ph(ws, ph_map, "d_phone", current_row, "", tpl_anchor_map)
            _set_ph(ws, ph_map, "addr", current_row, addr, tpl_anchor_map)

            # ── 급식 확인서: dong(관할동) 별도 플레이스홀더 ──
            if meal_type == "급식":
                _set_ph(ws, ph_map, "dong", current_row, dong, tpl_anchor_map)

            hh_notes: list[str] = []
            for p in members:
                nt = _blank_text(p.get("_note", "")).strip()
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

                name = _blank_text(person.get(name_col, ""))
                head = _blank_text(person.get(head_col, ""))
                phone = _blank_text(person.get(phone_col, ""))

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

                # 청구 서비스가 계산한 주차별 건수만 렌더링한다.
                weekly_counts = _prepared_weekly_counts(person)
                for week in range(1, 6):
                    prepared = weekly_counts.get(week)
                    _set_ph(
                        ws,
                        ph_map,
                        f"week{week}",
                        current_row,
                        _format_week_range(prepared) if prepared else "",
                        tpl_anchor_map,
                    )
                    for meal, placeholder in (
                        ("조식", "sum_b"),
                        ("석식", "sum_d"),
                        ("중식", "sum_l"),
                    ):
                        count = prepared[meal] if prepared else 0
                        _set_ph(
                            ws,
                            ph_map,
                            f"{placeholder}_{ci + 1}_{week}",
                            current_row,
                            count if count else "",
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
