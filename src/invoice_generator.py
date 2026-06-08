"""청구서 생성기 — 배송일 계산 + 템플릿 채우기"""

import calendar
import re
from datetime import date, timedelta

import openpyxl
from openpyxl.styles import Alignment, Font

from .config import (
    _get_template_data_start_row,
    _get_template_sheet_name,
    col_name_to_letter,
    read_template_columns,
)
from .holiday_fetcher import get_kr_holidays
from .settings import get_settings

# ── 요일 상수 ──
MON, TUE, WED, THU, FRI, SAT, SUN = range(7)


def _get_schedule():
    """설정에서 배송 스케줄 로드."""
    s = get_settings()
    return {
        "breakfast": set(s.get("schedule", "breakfast_days", default=[FRI])),
        "dinner": set(s.get("schedule", "dinner_days", default=[MON, TUE, WED, THU])),
        "lunch": set(
            s.get("schedule", "lunch_days", default=[MON, TUE, WED, THU, FRI])
        ),
    }


def _is_holiday(d: date) -> bool:
    return d in get_kr_holidays(d.year)


def count_weekdays(
    year: int, month: int, dow_set: set[int], excluded_dates: set[date] | None = None
) -> int:
    """해당 월에서 특정 요일(dow_set)에 속하고 공휴일 아닌 날짜 수. excluded_dates 제외"""
    total = 0
    last_day = calendar.monthrange(year, month)[1]
    for day in range(1, last_day + 1):
        d = date(year, month, day)
        if d.weekday() in dow_set and not _is_holiday(d):
            if excluded_dates and d in excluded_dates:
                continue
            total += 1
    return total


def count_weekdays_in_range(
    start: date, end: date, dow_set: set[int], excluded_dates: set[date] | None = None
) -> int:
    """기간 내 특정 요일 중 공휴일 아닌 날짜 수. excluded_dates 제외"""
    total = 0
    d = start
    while d <= end:
        if d.weekday() in dow_set and not _is_holiday(d):
            if not (excluded_dates and d in excluded_dates):
                total += 1
        d += timedelta(days=1)
    return total


# ── 비고 파서 ──────────────────────────────────────


def parse_note(note: str, ref_year: int) -> list[dict]:
    """
    비고 문자열에서 일시중지/신규 구간 파싱.
    먼저 매칭된 부분은 문자열에서 제거 → 중복매칭 방지.
    반환: [{"type":"pause"|"new", "start":date, "end":date|None}, ...]
    """
    if not note or not note.strip():
        return []
    results = []
    remaining = note.strip()

    # 1. 일시중지 (범위)
    range_pat = re.compile(
        r"(\d{1,2})[./](\d{1,2})\s*[~∼]\s*(\d{1,2})[./](\d{1,2})\s*일시중지"
    )
    while True:
        m = range_pat.search(remaining)
        if not m:
            break
        sm, sd = int(m.group(1)), int(m.group(2))
        em, ed = int(m.group(3)), int(m.group(4))
        if not (1 <= sm <= 12 and 1 <= em <= 12):
            remaining = remaining[: m.start()] + remaining[m.end() :]
            continue
        sy = ey = ref_year
        if em < sm:
            ey += 1
        results.append(
            {"type": "pause", "start": date(sy, sm, sd), "end": date(ey, em, ed)}
        )
        remaining = remaining[: m.start()] + remaining[m.end() :]

    # 2. 일시중지 (단일)
    single_pat = re.compile(r"(\d{1,2})[./](\d{1,2})\s*일시중지")
    while True:
        m = single_pat.search(remaining)
        if not m:
            break
        sm, sd = int(m.group(1)), int(m.group(2))
        if not (1 <= sm <= 12):
            remaining = remaining[: m.start()] + remaining[m.end() :]
            continue
        results.append(
            {
                "type": "pause",
                "start": date(ref_year, sm, sd),
                "end": date(ref_year, sm, sd),
            }
        )
        remaining = remaining[: m.start()] + remaining[m.end() :]

    # 3. 신규
    new_pat = re.compile(r"(\d{1,2})[./](\d{1,2})\s*(?:부터|신규)")
    while True:
        m = new_pat.search(remaining)
        if not m:
            break
        nm, nd = int(m.group(1)), int(m.group(2))
        if not (1 <= nm <= 12 and 1 <= nd <= 31):
            remaining = remaining[: m.start()] + remaining[m.end() :]
            continue
        results.append({"type": "new", "start": date(ref_year, nm, nd), "end": None})
        remaining = remaining[: m.start()] + remaining[m.end() :]

    return results


def calc_delivery_days(
    year: int,
    month: int,
    flag_breakfast: bool,
    flag_dinner: bool,
    flag_lunch: bool,
    note: str = "",
    excluded_dates: set[date] | None = None,
) -> dict:
    """한 사람의 월별 배송일 수 계산."""
    schedule = _get_schedule()
    base = {
        "breakfast": count_weekdays(year, month, schedule["breakfast"], excluded_dates)
        if flag_breakfast
        else 0,
        "dinner": count_weekdays(year, month, schedule["dinner"], excluded_dates)
        if flag_dinner
        else 0,
        "lunch": count_weekdays(year, month, schedule["lunch"], excluded_dates)
        if flag_lunch
        else 0,
    }

    pauses = parse_note(note, year)
    for p in pauses:
        if p["type"] == "pause":
            p_start = max(p["start"], date(year, month, 1))
            p_end = min(
                p["end"], date(year, month, calendar.monthrange(year, month)[1])
            )
            if p_start <= p_end:
                for meal in ["breakfast", "dinner", "lunch"]:
                    deducted = count_weekdays_in_range(
                        p_start, p_end, schedule[meal], excluded_dates
                    )
                    base[meal] = max(0, base[meal] - deducted)
        elif p["type"] == "new":
            pass

    return base


# ── 청구서 생성 ────────────────────────────────────


def _make_invoice_fonts():
    """설정에서 폰트 객체 생성."""
    s = get_settings()
    fn = s.get("display", "font_name", default="맑은 고딕")
    sz = s.get("display", "invoice_font_size", default=9)
    return Font(name=fn, size=sz), Font(name=fn, size=sz, bold=True)


def _make_confirm_fonts():
    s = get_settings()
    fn = s.get("display", "font_name", default="맑은 고딕")
    sz = s.get("display", "confirm_font_size", default=11)
    sz_s = s.get("display", "confirm_font_size_small", default=10)
    return Font(name=fn, size=sz), Font(name=fn, size=sz_s)


CENTER_ALIGN = Alignment(horizontal="center", vertical="center")


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
    provider: str = "",
    excluded_dates: set[date] | None = None,
):
    """roster_data → 청구서템플릿 채워서 output_path 에 저장."""
    template_columns = read_template_columns(template_path)
    invoice_font, invoice_font_bold = _make_invoice_fonts()

    bf_col = col_mapping.get("__calc_breakfast", "조식")
    dn_col = col_mapping.get("__calc_dinner", "석식")
    lh_col = col_mapping.get("__calc_lunch", "중식")
    total_col = col_mapping.get("__calc_total", "계")
    price_col = col_mapping.get("__calc_price", "청구금액")
    prov_col = col_mapping.get("__calc_provider", "제공기관")

    sheet_name = _get_template_sheet_name()
    wb = openpyxl.load_workbook(template_path)
    ws = wb[sheet_name]

    row_idx = _get_template_data_start_row()

    for i, person in enumerate(roster_data):
        flags = {
            "breakfast": _to_bool(person.get("_breakfast", 0)),
            "dinner": _to_bool(person.get("_dinner", 0)),
            "lunch": _to_bool(person.get("_lunch", 0)),
        }
        note_text = person.get("_note", "")
        days = calc_delivery_days(
            year,
            month,
            flags["breakfast"],
            flags["dinner"],
            flags["lunch"],
            note_text,
            excluded_dates,
        )

        breakfast_cnt = days["breakfast"] * 3
        if excluded_dates and flags["breakfast"]:
            excluded_fridays = sum(
                1
                for d in excluded_dates
                if d.weekday() == FRI and d.year == year and d.month == month
            )
            breakfast_cnt = max(0, breakfast_cnt + 2 * excluded_fridays)

        dinner_cnt = days["dinner"]
        lunch_cnt = days["lunch"]

        row_data = _map_person_to_row(person, col_mapping, i + 1, template_columns)

        row_data[bf_col] = breakfast_cnt
        row_data[dn_col] = dinner_cnt
        row_data[lh_col] = lunch_cnt

        bf_letter = col_name_to_letter(bf_col, template_columns)
        dn_letter = col_name_to_letter(dn_col, template_columns)
        lh_letter = col_name_to_letter(lh_col, template_columns)

        row_data[total_col] = (
            f"={bf_letter}{row_idx}+{dn_letter}{row_idx}+{lh_letter}{row_idx}"
        )
        row_data[price_col] = (
            f"={bf_letter}{row_idx}*{unit_price_breakfast}"
            f"+{dn_letter}{row_idx}*{unit_price_dinner}"
            f"+{lh_letter}{row_idx}*{unit_price_lunch}"
        )
        if provider:
            row_data[prov_col] = provider

        for col_name, value in row_data.items():
            col_letter = col_name_to_letter(col_name, template_columns)
            cell = ws[f"{col_letter}{row_idx}"]
            cell.value = value
            cell.font = invoice_font
            cell.alignment = CENTER_ALIGN

        row_idx += 1

    wb.save(output_path)
    return row_idx - _get_template_data_start_row()


def _to_bool(val) -> bool:
    if isinstance(val, (int, float)):
        return val > 0
    if isinstance(val, str):
        return val.strip() in ("1", "1.0", "Y", "y", "예", "있음")
    return bool(val)


def _map_person_to_row(
    person: dict, mapping: dict, seq: int, template_columns: list[str]
) -> dict:
    """person dict + mapping → {template_col_name: value}"""
    row: dict[str, str | int] = {}
    for col in template_columns:
        if col == "연번":
            row[col] = seq
        else:
            roster_key = mapping.get(col, "")
            row[col] = person.get(roster_key, "") if roster_key else ""
    return row


# ── 확인서: 주간 배송 계산 ──────────────────────────────


def _get_week_ranges(year: int, month: int) -> list[tuple[int, date, date]]:
    """설정 기반으로 해당 월을 주차별로 분할."""
    s = get_settings()
    last_day = calendar.monthrange(year, month)[1]
    week1_end = s.get("week_ranges", "week1_end_day", default=2)
    week_duration = s.get("week_ranges", "week_duration", default=7)
    start_days = s.get("week_ranges", "week_start_days", default=[3, 10, 17, 24])

    weeks = []
    end_1 = min(week1_end, last_day)
    weeks.append((1, date(year, month, 1), date(year, month, end_1)))

    for start_day in start_days:
        if start_day > last_day:
            break
        end_day = min(start_day + week_duration - 1, last_day)
        weeks.append(
            (len(weeks) + 1, date(year, month, start_day), date(year, month, end_day))
        )
    return weeks


def calc_weekly_breakdown(
    year: int,
    month: int,
    flag_breakfast: bool,
    flag_dinner: bool,
    flag_lunch: bool,
    note: str = "",
    excluded_dates: set[date] | None = None,
) -> list[dict]:
    """주차별 배송 건수 계산."""
    schedule = _get_schedule()
    weeks = _get_week_ranges(year, month)
    pauses = parse_note(note, year)

    result = []
    for wn, wstart, wend in weeks:
        b = (
            count_weekdays_in_range(wstart, wend, schedule["breakfast"], excluded_dates)
            * 3
            if flag_breakfast
            else 0
        )
        d = (
            count_weekdays_in_range(wstart, wend, schedule["dinner"], excluded_dates)
            if flag_dinner
            else 0
        )
        l = (
            count_weekdays_in_range(wstart, wend, schedule["lunch"], excluded_dates)
            if flag_lunch
            else 0
        )

        for p in pauses:
            if p["type"] == "pause":
                ps = max(p["start"], wstart)
                pe = min(p["end"], wend)
                if ps <= pe:
                    if flag_breakfast:
                        b -= (
                            count_weekdays_in_range(
                                ps, pe, schedule["breakfast"], excluded_dates
                            )
                            * 3
                        )
                    if flag_dinner:
                        d -= count_weekdays_in_range(
                            ps, pe, schedule["dinner"], excluded_dates
                        )
                    if flag_lunch:
                        l -= count_weekdays_in_range(
                            ps, pe, schedule["lunch"], excluded_dates
                        )

        result.append(
            {
                "week": wn,
                "start": wstart,
                "end": wend,
                "breakfast": max(0, b),
                "dinner": max(0, d),
                "lunch": max(0, l),
            }
        )
    return result


def _week_date_str(w_start: date, w_end: date) -> str:
    return f"{w_start.month:02d}.{w_start.day:02d}~{w_end.month:02d}.{w_end.day:02d}"


# ── 확인서 생성 ──────────────────────────────────────


def _copy_cell(src, dst):
    """셀 값 + 서식 복사"""
    from copy import copy

    dst.value = src.value
    if src.has_style:
        dst.font = copy(src.font)
        dst.border = copy(src.border)
        dst.fill = copy(src.fill)
        dst.number_format = copy(src.number_format)
        dst.protection = copy(src.protection)
        dst.alignment = copy(src.alignment)


def _adjust_formula(formula: str, row_offset: int) -> str:
    """엑셀 formula 내 행 번호를 row_offset 만큼 증가"""
    if not formula or not str(formula).startswith("="):
        return formula
    import re as _re

    def _shift_row(m):
        return f"{m.group(1)}{int(m.group(2)) + row_offset}"

    return _re.sub(r"([A-Z]+)(\d+)", _shift_row, str(formula))


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
    import os
    from collections import OrderedDict
    from copy import copy

    s = get_settings()

    _LOOKUP = ["주소", "성명", "세대주성명", "핸드폰", "학교명", "관할동"]
    addr_col, name_col, head_col, phone_col, dept_col, dong_col = (
        col_mapping.get(k, k) for k in _LOOKUP
    )

    # 1. 주소 줄바꿈 → 공백
    for p in roster_data:
        raw = str(p.get(addr_col, ""))
        p[addr_col] = raw.replace("\n", " ").replace("\r", " ").strip()

    # 2. 관할동별 그룹핑
    dong_groups = OrderedDict()
    for p in roster_data:
        dong = str(p.get(dong_col, "")).strip()
        if dong not in dong_groups:
            dong_groups[dong] = []
        dong_groups[dong].append(p)

    if not title_prefix:
        title_prefix = f"{year}년 {month}월"

    weeks = _get_week_ranges(year, month)
    created = []

    # 3. 템플릿 워크북 로드
    wb = openpyxl.load_workbook(template_path)
    data_cols = s.get("templates", "data_columns", default=11)
    confirm_font, _ = _make_confirm_fonts()

    for dong, persons_in_dong in dong_groups.items():
        # 세대(주소)별 그룹핑
        hh_groups = OrderedDict()
        for p in persons_in_dong:
            addr = p.get(addr_col, "")
            if addr not in hh_groups:
                hh_groups[addr] = []
            hh_groups[addr].append(p)

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
                    _copy_cell(
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

            # Formula 행 번호 보정
            for src_r in range(1, tpl_rows + 1):
                dst_r = current_row + src_r - 1
                for src_c in range(1, data_cols + 1):
                    cell = ws.cell(row=dst_r, column=src_c)
                    if cell.value and str(cell.value).startswith("="):
                        cell.value = _adjust_formula(cell.value, current_row - 1)

            # 공통 정보 채움
            title_suffix = s.get("confirm", "title_suffix", default="분당구 급식확인서")
            ws.cell(row=current_row, column=1).value = f"{title_prefix} {title_suffix}"
            if delivery_person:
                ws.cell(row=current_row + 2, column=11).value = delivery_person
            if delivery_phone:
                ws.cell(row=current_row + 3, column=11).value = delivery_phone
            ws.cell(row=current_row + 2, column=3).value = addr

            # 각 아동별 채움
            for ci, person in enumerate(members[:5]):
                sheet_seq += 1
                base_row = current_row + 5 + ci * 5

                name = str(person.get(name_col, ""))
                head = str(person.get(head_col, ""))
                phone = str(person.get(phone_col, ""))

                ws.cell(row=base_row, column=1).value = sheet_seq
                ws.cell(row=base_row, column=2).value = name
                ws.cell(row=base_row, column=3).value = head
                ws.cell(row=base_row, column=4).value = phone

                flags = {
                    "breakfast": _to_bool(person.get("_breakfast", 0)),
                    "dinner": _to_bool(person.get("_dinner", 0)),
                    "lunch": _to_bool(person.get("_lunch", 0)),
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

                if excluded_dates and flags["breakfast"]:
                    for wi, wdata in enumerate(weeks):
                        if wi >= len(weekly):
                            break
                        w_start, w_end = wdata[1], wdata[2]
                        excluded_fri_in_week = sum(
                            1
                            for d in excluded_dates
                            if d.weekday() == FRI and w_start <= d <= w_end
                        )
                        weekly[wi]["breakfast"] = max(
                            0, weekly[wi]["breakfast"] + 2 * excluded_fri_in_week
                        )

                for wi, wdata in enumerate(weeks):
                    row = base_row + wi
                    wd = (
                        weekly[wi]
                        if wi < len(weekly)
                        else {"breakfast": 0, "dinner": 0, "lunch": 0}
                    )
                    ws.cell(row=row, column=6).value = _week_date_str(
                        wdata[1], wdata[2]
                    )
                    ws.cell(row=row, column=7).value = (
                        wd["breakfast"] if wd["breakfast"] else ""
                    )
                    ws.cell(row=row, column=8).value = (
                        wd["dinner"] if wd["dinner"] else ""
                    )
                    ws.cell(row=row, column=9).value = (
                        wd["lunch"] if wd["lunch"] else ""
                    )

                for wi in range(len(weeks)):
                    row = base_row + wi
                    for col_idx in (1, 2, 3, 4, 6, 7, 8, 9, 10, 11):
                        cell = ws.cell(row=row, column=col_idx)
                        if cell.value is not None:
                            cell.font = confirm_font
                            cell.alignment = Alignment(
                                horizontal="center", vertical="center"
                            )

                dept = str(person.get(dept_col, ""))
                created.append({"name": name, "dept": dept, "amount": 0, "file": ""})

            current_row += tpl_rows

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
