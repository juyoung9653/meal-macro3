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


def _is_holiday(d: date) -> bool:
    kr = holidays.country_holidays("KR")
    return d in kr


def count_weekdays(year: int, month: int, dow_set: set[int]) -> int:
    """해당 월에서 특정 요일(dow_set)에 속하고 공휴일이 아닌 날짜 수"""
    total = 0
    last_day = calendar.monthrange(year, month)[1]
    for day in range(1, last_day + 1):
        d = date(year, month, day)
        if d.weekday() in dow_set and not _is_holiday(d):
            total += 1
    return total


def count_weekdays_in_range(start: date, end: date, dow_set: set[int]) -> int:
    """기간 내 특정 요일 중 공휴일 아닌 날짜 수"""
    total = 0
    d = start
    while d <= end:
        if d.weekday() in dow_set and not _is_holiday(d):
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
) -> dict:
    """
    한 사람의 월별 배송일 수 계산.
    flag_*: 명단에서 해당 급식 이용 여부 (1.0=True)
    """
    base = {
        "breakfast": count_weekdays(year, month, SCHEDULE["breakfast"])
        if flag_breakfast
        else 0,
        "dinner": count_weekdays(year, month, SCHEDULE["dinner"]) if flag_dinner else 0,
        "lunch": count_weekdays(year, month, SCHEDULE["lunch"]) if flag_lunch else 0,
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
                    deducted = count_weekdays_in_range(p_start, p_end, SCHEDULE[meal])
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
    template_path: str = "청구서템플릿.xlsx",
    year: int = 2026,
    month: int = 6,
    unit_price_breakfast: int = 4000,
    unit_price_dinner: int = 2000,
    unit_price_lunch: int = 4000,
    provider: str = "",
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
            year, month, flags["breakfast"], flags["dinner"], flags["lunch"], note_text
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


def make_confirm(
    invoice_path: str,
    output_path: str,
    year: int = 2026,
    month: int = 6,
) -> int:
    """
    청구서 기반 확인서 생성 (TODO: 추후 구현).
    현재는 청구서 복사본.
    """
    import shutil

    shutil.copy2(invoice_path, output_path)
    return 0
