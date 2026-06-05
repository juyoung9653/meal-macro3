"""청구서 생성기 — 배송일 계산 + 템플릿 채우기"""

import calendar
import re
from datetime import date, timedelta

import holidays
import openpyxl
from openpyxl.styles import Alignment, Font

from .config import (
    DEFAULT_TEMPLATE_COLUMNS,
    TEMPLATE_DATA_START_ROW,
    TEMPLATE_SHEET_NAME,
)

# ── 요일 상수 ──
MON, TUE, WED, THU, FRI, SAT, SUN = range(7)


# ── 공휴일 캐시 (매번 생성 비용 大 → 모듈 로딩 시 1회) ──
_KR_HOLIDAYS = holidays.country_holidays("KR")


def _is_holiday(d: date) -> bool:
    return d in _KR_HOLIDAYS


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
            if excluded_dates and d in excluded_dates:
                pass  # skip
            else:
                total += 1
        d += timedelta(days=1)
    return total


# ── 배송 스케줄 (요일 집합) ──
SCHEDULE = {
    "breakfast": {FRI},  # 조식: 금
    "dinner": {MON, TUE, WED, THU},  # 석식: 월~목
    "lunch": {MON, TUE, WED, THU, FRI},  # 중식(방학): 월~금
}


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

    # 1. 일시중지 (범위): "4/27~5/4 일시중지" / "4.27∼5.4 일시중지"
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

    # 2. 일시중지 (단일): "4/27 일시중지"
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

    # 3. 신규: "6/1 신규" / "신규 6/1부터" / "6.1부터"
    # NOTE: month=1..12 보장, 실제 일시중지 제거 후 남은 텍스트에서만 동작
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
    """
    한 사람의 월별 배송일 수 계산.
    flag_*: 명단에서 해당 급식 이용 여부 (1.0=True)
    excluded_dates: 배송 제외일 집합
    """
    base = {
        "breakfast": count_weekdays(year, month, SCHEDULE["breakfast"], excluded_dates)
        if flag_breakfast
        else 0,
        "dinner": count_weekdays(year, month, SCHEDULE["dinner"], excluded_dates)
        if flag_dinner
        else 0,
        "lunch": count_weekdays(year, month, SCHEDULE["lunch"], excluded_dates)
        if flag_lunch
        else 0,
    }

    # 비고 조정
    pauses = parse_note(note, year)
    for p in pauses:
        if p["type"] == "pause":
            # pause 구간과 겹치는 배송일 차감
            p_start = max(p["start"], date(year, month, 1))
            p_end = min(
                p["end"], date(year, month, calendar.monthrange(year, month)[1])
            )
            if p_start <= p_end:
                for meal in ["breakfast", "dinner", "lunch"]:
                    deducted = count_weekdays_in_range(
                        p_start, p_end, SCHEDULE[meal], excluded_dates
                    )
                    base[meal] = max(0, base[meal] - deducted)

        elif p["type"] == "new":
            # 해당 월 이전에 시작했으면 영향 없음, 이후면 0
            # 신규 시작일 전날까지는 배송 안 한 걸로
            # 이미 flag_*로 걸러졌으므로 추가 처리 생략 가능
            # 실제로는 신규일 이전 날짜를 제외해야 하지만
            # flag가 True면 해당월 전체 배송으로 계산된 상태
            # TODO: 세밀한 조정 필요시 구현
            pass

    return base


# ── 청구서 생성 ────────────────────────────────────

INVOICE_FONT = Font(name="맑은 고딕", size=9)
INVOICE_FONT_BOLD = Font(name="맑은 고딕", size=9, bold=True)
CENTER_ALIGN = Alignment(horizontal="center", vertical="center")


def make_invoice(
    roster_data: list[dict],
    col_mapping: dict,
    output_path: str,
    template_path: str,
    year: int = 2026,
    month: int = 6,
    unit_price_breakfast: int = 4000,
    unit_price_dinner: int = 2000,
    unit_price_lunch: int = 4000,
    provider: str = "",
    excluded_dates: set[date] | None = None,
):
    """
    roaster_data → 청구서템플릿 채워서 output_path 에 저장.

    col_mapping 은 roster_data dict key → template 컬럼명(0-based index) 대응.
    config.DEFAULT_TEMPLATE_COLUMNS 기준.
    """
    wb = openpyxl.load_workbook(template_path)
    ws = wb[TEMPLATE_SHEET_NAME]

    row_idx = TEMPLATE_DATA_START_ROW

    for i, person in enumerate(roster_data):
        # 배송일 계산
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
        dinner_cnt = days["dinner"]
        lunch_cnt = days["lunch"]

        # 템플릿 매핑
        row_data = _map_person_to_row(person, col_mapping, i + 1)
        row_data["조식"] = breakfast_cnt
        row_data["석식"] = dinner_cnt
        row_data["중식"] = lunch_cnt
        # 계, 청구금액은 엑셀 수식
        row_data["계"] = f"=L{row_idx}+M{row_idx}+N{row_idx}"
        row_data["청구금액"] = (
            f"=L{row_idx}*{unit_price_breakfast}"
            f"+M{row_idx}*{unit_price_dinner}"
            f"+N{row_idx}*{unit_price_lunch}"
        )
        if provider:
            row_data["제공기관"] = provider

        # 엑셀 쓰기
        for col_name, value in row_data.items():
            col_letter = _col_name_to_letter(col_name)
            cell = ws[f"{col_letter}{row_idx}"]
            cell.value = value
            cell.font = INVOICE_FONT
            cell.alignment = CENTER_ALIGN

        row_idx += 1

    wb.save(output_path)
    return row_idx - TEMPLATE_DATA_START_ROW  # 생성 건수


def _to_bool(val) -> bool:
    if isinstance(val, (int, float)):
        return val > 0
    if isinstance(val, str):
        return val.strip() in ("1", "1.0", "Y", "y", "예", "있음")
    return bool(val)


def _map_person_to_row(person: dict, mapping: dict, seq: int) -> dict:
    """person dict + mapping → {template_col_name: value}"""
    row = {"연번": seq}
    tmpl_cols = {
        "구분": mapping.get("roster_category_col", ""),
        "학교명": mapping.get("roster_dept_col", ""),
        "학년": mapping.get("roster_grade_col", ""),
        "성명": mapping.get("roster_name_col", ""),
        "생년월일": mapping.get("roster_birth_col", ""),
        "세대주성명": mapping.get("roster_head_col", ""),
        "아동과의관계": mapping.get("roster_relation_col", ""),
        "관할동": mapping.get("roster_dong_col", ""),
        "주소": mapping.get("roster_addr_col", ""),
        "핸드폰": mapping.get("roster_phone_col", ""),
        "비고": mapping.get("roster_note_col", ""),
    }
    for tmpl_col, roster_key in tmpl_cols.items():
        if roster_key:
            row[tmpl_col] = person.get(roster_key, "")
        else:
            row[tmpl_col] = ""
    return row


def _col_name_to_letter(name: str) -> str:
    """DEFAULT_TEMPLATE_COLUMNS 인덱스 → 엑셀 컬럼 문자"""
    idx_map = {
        "연번": "A",
        "구분": "B",
        "학교명": "C",
        "학년": "D",
        "성명": "E",
        "생년월일": "F",
        "세대주성명": "G",
        "아동과의관계": "H",
        "관할동": "I",
        "주소": "J",
        "핸드폰": "K",
        "조식": "L",
        "석식": "M",
        "중식": "N",
        "계": "O",
        "청구금액": "P",
        "제공기관": "Q",
        "비고": "R",
    }
    return idx_map.get(name, "A")


# ── 확인서: 주간 배송 계산 ──────────────────────────────

CONFIRM_FONT = Font(name="맑은 고딕", size=11)
CONFIRM_FONT_SMALL = Font(name="맑은 고딕", size=10)


def _get_week_ranges(year: int, month: int) -> list[tuple[int, date, date]]:
    """해당 월을 주차별로 분할. [(week_num, start, end), ...]"""
    import calendar

    last_day = calendar.monthrange(year, month)[1]
    weeks = []
    for start_day in (1, 8, 15, 22, 29):
        if start_day > last_day:
            break
        end_day = min(start_day + 6, last_day)
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
    """
    주차별 배송 건수 계산.
    반환: [{"week":1, "start":date, "end":date, "breakfast":N, "dinner":N, "lunch":N}, ...]
    """
    weeks = _get_week_ranges(year, month)
    pauses = parse_note(note, year)

    result = []
    for wn, wstart, wend in weeks:
        b = (
            count_weekdays_in_range(wstart, wend, SCHEDULE["breakfast"], excluded_dates)
            * 3
            if flag_breakfast
            else 0
        )
        d = (
            count_weekdays_in_range(wstart, wend, SCHEDULE["dinner"], excluded_dates)
            if flag_dinner
            else 0
        )
        l = (
            count_weekdays_in_range(wstart, wend, SCHEDULE["lunch"], excluded_dates)
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
                                ps, pe, SCHEDULE["breakfast"], excluded_dates
                            )
                            * 3
                        )
                    if flag_dinner:
                        d -= count_weekdays_in_range(
                            ps, pe, SCHEDULE["dinner"], excluded_dates
                        )
                    if flag_lunch:
                        l -= count_weekdays_in_range(
                            ps, pe, SCHEDULE["lunch"], excluded_dates
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
    """주차 날짜 범위 문자열 "05.01~05.02"""
    return f"{w_start.month:02d}.{w_start.day:02d}~{w_end.month:02d}.{w_end.day:02d}"


# ── 확인서 생성 ──────────────────────────────────────


def make_confirm(
    roster_data: list[dict],
    col_mapping: dict,
    output_dir: str,
    template_path: str = "확인서템플릿.xlsx",
    year: int = 2026,
    month: int = 6,
    title_prefix: str = "",
    delivery_person: str = "",
    delivery_phone: str = "",
    excluded_dates: set[date] | None = None,
) -> list[dict]:
    """
    명단 데이터 → 확인서템플릿 채워서 개별 확인서 생성.
    동일 주소로 세대별 그룹핑 → 한 시트에 같이 출력.
    반환: [{"name":..., "dept":..., "amount":..., "file":...}, ...]
    """
    import os
    from collections import OrderedDict

    addr_col = col_mapping.get("roster_addr_col", "주소")
    name_col = col_mapping.get("roster_name_col", "성명")
    head_col = col_mapping.get("roster_head_col", "세대주성명")
    phone_col = col_mapping.get("roster_phone_col", "핸드폰")
    dept_col = col_mapping.get("roster_dept_col", "학교명")

    # 세대별 그룹핑 (주소 기준, 순서 유지)
    groups = OrderedDict()
    for p in roster_data:
        addr = str(p.get(addr_col, "")).strip()
        if addr not in groups:
            groups[addr] = []
        groups[addr].append(p)

    if not title_prefix:
        title_prefix = f"{year}년 {month}월"

    created = []
    seq_total = 0

    for addr, members in groups.items():
        n = len(members)
        # 몇 명짜리 시트 쓸지 결정
        sheet_key = f"{min(n, 5)}명"

        wb = openpyxl.load_workbook(template_path)
        # 필요한 시트만 남기고 삭제
        for sn in list(wb.sheetnames):
            if sn != sheet_key:
                del wb[sn]
        ws = wb[sheet_key]

        # 공통 채움
        ws["A1"] = f"{title_prefix} 분당구 급식확인서"
        if delivery_person:
            ws["K3"] = delivery_person
        if delivery_phone:
            ws["K4"] = delivery_phone
        # 주소
        ws["C3"] = addr

        # 주간 범위
        weeks = _get_week_ranges(year, month)

        # 각 아동별 채움
        for ci, person in enumerate(members):
            seq_total += 1
            child_idx = ci + 1
            base_row = 6 + ci * 5  # 1명=6, 2명=11, 3명=16, ...

            name = str(person.get(name_col, ""))
            head = str(person.get(head_col, ""))
            phone = str(person.get(phone_col, ""))

            ws[f"A{base_row}"] = child_idx
            ws[f"B{base_row}"] = name
            ws[f"C{base_row}"] = head
            ws[f"D{base_row}"] = phone

            # 주간 배송 계산
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

            for wi, wdata in enumerate(weeks):
                row = base_row + wi
                wd = (
                    weekly[wi]
                    if wi < len(weekly)
                    else {"breakfast": 0, "dinner": 0, "lunch": 0}
                )
                ws[f"F{row}"] = _week_date_str(wdata[1], wdata[2])
                ws[f"G{row}"] = wd["breakfast"]
                ws[f"H{row}"] = wd["dinner"]
                ws[f"I{row}"] = wd["lunch"]

            # 폰트 적용
            for wi in range(len(weeks)):
                row = base_row + wi
                for col in ("A", "B", "C", "D", "F", "G", "H", "I", "J", "K"):
                    cell = ws[f"{col}{row}"]
                    if cell.value is not None:
                        cell.font = CONFIRM_FONT
                        cell.alignment = Alignment(
                            horizontal="center", vertical="center"
                        )

            # 결과 저장용
            dept = str(person.get(dept_col, ""))
            created.append(
                {
                    "name": name,
                    "dept": dept,
                    "amount": 0,  # 금액은 청구서 참조
                    "file": f"{addr[:15]}.xlsx",
                }
            )

        # 저장
        safe_addr = addr.replace(" ", "_").replace(",", "").replace(".", "")[:20]
        fname = f"확인서_{year:04d}{month:02d}_{safe_addr}.xlsx"
        out_path = os.path.join(output_dir, fname)
        wb.save(out_path)

        # file명 업데이트
        for item in created[-n:]:
            item["file"] = fname

    return created
