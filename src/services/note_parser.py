"""비고(Note) 파서 — 명단 비고 문자열에서 배송 일정 변경사항 추출.

Public API:
  - parse_note(note, ref_year) -> list[dict]
    각 dict: {type, meals, start, end, weekdays, raw}

  - expand_note_dates(note, ref_year, month) -> list[tuple[date, set[str]]]
    비고 → 개별 날짜로 확장 → 해당 월만 필터링 (Pipeline Steps 1-2)
    각 tuple: (date, {"breakfast"|"dinner"|"lunch", ...})

  - apply_note_to_month(note, year, month, flags, excluded_dates) -> dict
    비고 → 배송 차감 건수 계산 (Pipeline Steps 1-4)
    Returns: {"breakfast": int, "dinner": int, "lunch": int}
"""

import logging
import re
from datetime import date, timedelta
from typing import Callable

from ..core.date_utils import FRI, MON, SAT, SUN, THU, TUE, WED, date_range
from ..core.meal_types import ALL_MEALS, MealType
from ..core.settings import get_settings
from .holiday_fetcher import get_kr_holidays

log = logging.getLogger(__name__)

# ── 요일 alias (하위 호환) ──

_KOREAN_WEEKDAY: dict[str, int] = {
    "월": MON,
    "화": TUE,
    "수": WED,
    "목": THU,
    "금": FRI,
    "토": SAT,
    "일": SUN,
}


# ── 날짜 유틸 ──


def _resolve_year(yy: int, ref_year: int) -> int:
    return ref_year if yy == 0 else 2000 + yy if yy < 100 else yy


def _detect_meals(text: str) -> set[str]:
    """텍스트에서 급식 종류 탐지. 없으면 전체 반환."""
    found: set[str] = set()
    if "조식" in text:
        found.add(MealType.BREAKFAST)
    if "석식" in text:
        found.add(MealType.DINNER)
    if "중식" in text or "방중" in text:
        found.add(MealType.LUNCH)
    return found if found else ALL_MEALS


# ── 무시할 내용 필터 ──


def _is_irrelevant(text: str) -> bool:
    """급식 갯수와 무관한 내용 (주소변경, 개명, 우유 등)."""
    irr_kw = ["개명", "흰우유", "별도메뉴"]
    for kw in irr_kw:
        if kw in text:
            return True
    # 주소 관련 내용은 항상 무시 (급식 갯수와 무관)
    if any(kw in text for kw in ["주소", "신주소", "변동주소"]):
        return True
    return False


# ── 세그먼트 분할 (개행/쌍반점/마침표 기준, 쉼표는 유지) ──


def _split_segments(text: str) -> list[str]:
    """개행, 쌍반점, 마침표+공백 기준 분할. 쉼표는 날짜 구분자로 유지."""
    # 쌍따옴표 블록 보호
    protected: dict[str, str] = {}

    def _protect(m):
        tok = f"__Q{len(protected)}__"
        protected[tok] = m.group(0)
        return tok

    text = re.sub(r'"[^"]*"', _protect, text)

    # 콤마+개행 연결: 날짜 목록이 여러 줄에 걸친 경우 병합
    # 예: "5/7,8,9,10,\n31 배송요청" → "5/7,8,9,10,31 배송요청"
    text = re.sub(r",\s*\n\s*(?=\d)", ",", text)

    # 분할
    parts = re.split(r"\n|\r\n|;\s*", text)

    result = []
    for p in parts:
        p = p.strip()
        # 보호 복원
        for tok, orig in protected.items():
            p = p.replace(tok, orig)
        if p and p not in ("0", "", '"'):
            result.append(p)
    return result


# ── 액션 타입 ──


def _action_type(text: str) -> str:
    if "일시중지" in text or "일시 중지" in text:
        return "pause"
    if "중지" in text:
        return "stop"
    if "재배송" in text:
        return "resume"
    if "추가" in text:
        return "add"
    if "신규" in text:
        return "new"
    if "배송" in text:
        return "new"
    return "pause"


# ══════════════════════════════════════════════════════
#  메인 파서
# ══════════════════════════════════════════════════════


def parse_note(note: str, ref_year: int) -> list[dict]:
    """비고 문자열에서 배송 일정 변경사항 파싱.

    반환:
        [{"type":str, "meals":set[str], "start":date|None, "end":date|None,
          "weekdays":set[int]|None, "raw":str}, ...]

    type: "pause"|"stop"|"new"|"resume"|"add"|"weekday_only"
    """
    if not note or not note.strip():
        return []

    segments = _split_segments(note.strip())
    all_results: list[dict] = []

    for seg in segments:
        seg = seg.strip()
        if not seg:
            continue
        # 먼저 파싱 후, 각 결과의 raw가 무시할 내용이면 제외
        results = _parse_segment(seg, ref_year)
        for r in results:
            if not _is_irrelevant(r.get("raw", "")):
                all_results.append(r)

    return all_results


# ══════════════════════════════════════════════════════
#  날짜 확장 — Pipeline Steps 1-2
# ══════════════════════════════════════════════════════


def expand_note_dates(
    note: str, ref_year: int, month: int
) -> list[tuple[date, set[str]]]:
    """비고 파싱 → 개별 날짜 확장 → 해당 월 필터링.

    Pipeline Steps 1-2:
      1. parse_note 로 비고의 모든 adjustment 파싱
      2. 각 adjustment 의 날짜범위/요일 을 개별 date 로 확장
      3. 해당 월에 속하는 날짜만 유지

    Returns:
        list of (date, meal_types) pairs.
        meal_types = {"breakfast", "dinner", "lunch"} 중 하나 이상.
        중복 (date, meal_type) 제거됨.
    """
    import calendar

    adjustments = parse_note(note, ref_year)
    if not adjustments:
        return []

    log.info("[Memo] raw=%r", note.strip())

    month_start = date(ref_year, month, 1)
    month_end = date(ref_year, month, calendar.monthrange(ref_year, month)[1])

    seen: set[tuple[date, str]] = set()
    result: list[tuple[date, set[str]]] = []

    for adj in adjustments:
        adj_type = adj["type"]
        meals = adj["meals"]
        start = adj.get("start")
        end = adj.get("end")
        weekdays = adj.get("weekdays")
        allowed_dates = adj.get("allowed_dates")  # specific_dates whitelist
        raw = adj.get("raw", "")

        log.debug(
            "[Memo] adj type=%s meals=%s start=%s end=%s wd=%s raw=%r",
            adj_type,
            meals,
            start,
            end,
            weekdays,
            raw,
        )

        # standalone "배송" (날짜 없음, 신규/재배송/추가/식사종류 없음) = 단순 메모, 스킵
        # 날짜(start) 가 있으면 의미 있는 배송 일정이므로 처리
        if adj_type in ("new", "resume", "add"):
            has_qualifier = any(
                kw in raw
                for kw in ["신규", "재배송", "추가", "조식", "석식", "중식", "방중"]
            )
            if not has_qualifier and start is None:
                continue

        # ── 날짜 범위 결정 ──
        r_start: date | None = None
        r_end: date | None = None

        if adj_type in ("pause", "stop"):
            if start is not None and end is not None:
                r_start = max(start, month_start)
                r_end = min(end, month_end)
            elif start is not None:
                r_start = max(start, month_start)
                r_end = month_end
            elif weekdays is not None:
                r_start = month_start
                r_end = month_end
            elif adj_type == "stop":
                r_start = month_start
                r_end = month_end

        elif adj_type in ("new", "resume", "add"):
            effective_start = start if start else month_start
            if effective_start > month_start:
                r_start = month_start
                r_end = effective_start - timedelta(days=1)

        elif adj_type == "weekday_only":
            r_start = month_start
            r_end = month_end

        elif adj_type == "specific_dates":
            # 명시된 날짜들만 배송, 나머지 전부 차감 (whitelist)
            r_start = month_start
            r_end = month_end

        if r_start is None or r_end is None or r_start > r_end:
            continue

        # ── 날짜 확장 ──
        for d in date_range(r_start, r_end):
            # weekday_only: 지정 요일만 허용 → 나머지는 차감 대상
            if adj_type == "weekday_only" and weekdays:
                if d.weekday() in weekdays:
                    continue
            # specific_dates: 명시된 날짜만 허용 → 나머지는 차감 대상
            elif adj_type == "specific_dates" and allowed_dates:
                if d in allowed_dates:
                    continue
            # pause/stop with weekdays filter: 지정 요일만 차감
            elif weekdays is not None and d.weekday() not in weekdays:
                continue

            for meal in meals:
                key = (d, meal)
                if key not in seen:
                    seen.add(key)
                    result.append((d, {meal}))

    log.info(
        "[Memo] expand: %d adjustments -> %d (date,meal) pairs in %04d-%02d",
        len(adjustments),
        len(result),
        ref_year,
        month,
    )
    return result


def _parse_segment(seg: str, ref_year: int) -> list[dict]:
    """단일 세그먼트 처리."""
    results: list[dict] = []
    remaining = seg

    # 0. "방학중식 중지" 특수 패턴
    m = re.search(r"방학중식\s*중지", remaining)
    if m:
        remaining = remaining[: m.start()] + remaining[m.end() :]
        results.append(
            {
                "type": "stop",
                "meals": {"lunch"},
                "start": None,
                "end": None,
                "weekdays": None,
                "raw": m.group(0),
            }
        )

    # 1. 매주X요일 패턴 (예: 매주월,목일시중지(석식만))
    #    (조식/석식/중식은 요일 뒤 또는 괄호 안에 올 수 있음)
    m = re.search(
        r"매주\s*"
        r"([월화수목금토일]+(?:,\s*[월화수목금토일]+)*)\s*"
        r"(?:요일)?\s*"
        r"((?:조식|석식|중식|방중)(?:,\s*(?:조식|석식|중식|방중))*)?\s*"
        r"(일시중지|일시\s*중지|중지|배송)"
        r"\s*(?:\(([^)]*)\))?",
        remaining,
    )
    if m:
        remaining = remaining[: m.start()] + remaining[m.end() :]
        wk_str = m.group(1).replace(" ", "")
        meal_between = (m.group(2) or "").replace(" ", "")
        action_raw = m.group(3)
        paren = m.group(4) or ""

        weekdays = {_KOREAN_WEEKDAY[ch] for ch in wk_str if ch in _KOREAN_WEEKDAY}
        action = _action_type(action_raw)
        # meal_between이 있으면 우선 사용, 없으면 paren + remaining에서 감지
        if meal_between:
            meals = _detect_meals(meal_between)
        else:
            meals = _detect_meals(paren + remaining[: m.start()] + remaining[m.end() :])

        for wd in weekdays:
            results.append(
                {
                    "type": action,
                    "meals": meals,
                    "start": None,
                    "end": None,
                    "weekdays": {wd},
                    "raw": m.group(0),
                }
            )

    # 2. 요일 지정 배송 패턴 (예: 화,목(석식)만 배송희망, 월,수 배송(석식))
    m = re.search(
        r"([월화수목금토일]+(?:,\s*[월화수목금토일]+)*)\s*"
        r"(?:요일)?\s*"
        r"(?:\(([^)]*)\))?\s*"
        r"(?:만\s*)?배송",
        remaining,
    )
    if m:
        remaining = remaining[: m.start()] + remaining[m.end() :]
        wk_str = m.group(1).replace(" ", "")
        paren = m.group(2) or ""

        weekdays = {_KOREAN_WEEKDAY[ch] for ch in wk_str if ch in _KOREAN_WEEKDAY}
        meals = _detect_meals(paren + remaining)

        results.append(
            {
                "type": "weekday_only",
                "meals": meals,
                "start": None,
                "end": None,
                "weekdays": weekdays,
                "raw": m.group(0),
            }
        )

    # 3. 날짜 기반 패턴들
    results.extend(_parse_date_patterns(remaining, ref_year))

    return results


# ── 날짜 패턴 정규식 (모듈 레벨로 미리 컴파일) ──

# 4a. "M/D1,D2,... 액션" (다중 날짜, 쉼표로 구분)
_RE_MULTI_DATE = re.compile(
    r"(?:(\d{2,4})[./])?(\d{1,2})[./](\d{1,2})"
    r"((?:,\s*\d{1,2})+)"  # ,D ,D ...
    r"\s*(조식|석식|중식|방중)?\s*"
    r"(일시\s*중지|일시중지|중지|배송)"
)

# 4b. "YY/M/D~M/D 액션" (범위, 연도포함)
_RE_YR_RANGE = re.compile(
    r"(?:(\d{2,4})[./])?(\d{1,2})[./](\d{1,2})"  # start date
    r"\s*(?:[~∼]|부터)\s*"
    r"(?:(?:(\d{2,4})[./])?(\d{1,2})[./](\d{1,2})\s*)?"  # optional end date
    r"((?:조식|석식|중식|방중)(?:,\s*(?:조식|석식|중식|방중))*)?\s*"
    r"(일시\s*중지|일시중지|중지|재배송|신규\s*배송|신규|추가\s*배송|배송)"
)

# 4b2. "YY/M/D~요일1,요일2(meal) 액션" (날짜범위 + 요일필터)
_RE_RANGE_WK = re.compile(
    r"(?:(\d{2,4})[./])?(\d{1,2})[./](\d{1,2})"  # start date
    r"\s*[~∼]\s*"
    r"([월화수목금토일]+(?:,\s*[월화수목금토일]+)*)"  # weekdays
    r"\s*(?:요일)?\s*"
    r"(?:\(([^)]*)\))?\s*"  # optional meal in parens
    r"((?:조식|석식|중식|방중)(?:,\s*(?:조식|석식|중식|방중))*)?\s*"  # optional inline meal
    r"(일시\s*중지|일시중지|중지)"  # action
)

# 4c. "M/D~D 액션" (같은 달 축약형)
_RE_RANGE_ABBREV = re.compile(
    r"(?:(\d{2,4})[./])?(\d{1,2})[./](\d{1,2})"
    r"\s*[~∼]\s*(\d{1,2})"
    r"\s*((?:조식|석식|중식|방중)(?:,\s*(?:조식|석식|중식|방중))*)?\s*"
    r"(일시\s*중지|일시중지|중지|재배송)"
)

# 4d. "~M/D 액션" (종료일만 지정, 시작일 없음)
_RE_TILDE_END = re.compile(
    r"~\s*(?:(\d{2,4})[./])?(\d{1,2})[./](\d{1,2})"
    r"\s*((?:조식|석식|중식|방중)(?:,\s*(?:조식|석식|중식|방중))*)?\s*"
    r"(일시\s*중지|일시중지|중지|재배송|신규\s*배송|신규|추가\s*배송|배송)"
)

# 4e. 단일 날짜 + 액션 (가장 일반적)
_RE_SINGLE_DATE = re.compile(
    r"(?:(\d{2,4})[./])?(\d{1,2})[./](\d{1,2})"
    r"\s*(?:[~∼]|부터)?\s*"
    r"((?:조식|석식|중식|방중)(?:,\s*(?:조식|석식|중식|방중))*)?\s*"
    r"(?:\(([^)]*)\))?\s*"
    r"(?:만\s*)?(일시\s*중지|일시중지|중지(?!\s*배송)|재배송|신규\s*배송|신규|추가\s*배송|배송)"
)


# ── 개별 날짜 패턴 파서 함수들 ──
# 각 함수: (text, ref_year) → (results, remaining_text)
#   - results: 해당 패턴으로 추출한 결과 dict 목록
#   - remaining_text: 매치된 부분이 제거된 나머지 텍스트


def _parse_multi_date(text: str, ref_year: int) -> tuple[list[dict], str]:
    r""" "M/D1,D2,... 액션" — 다중 날짜 (쉼표 구분).

    예: 12/2,3,5,6,7,8,9,10,...일시중지
    예: 4/9,14 석식 일시중지
    예: 5/7,8,9,10,... 배송요청

    pause/stop: 각 날짜를 개별 차감일로 등록.
    배송(신규/재배송/추가): 명시된 날짜만 배송일 whitelist.
    """
    results: list[dict] = []
    remaining = text

    while True:
        m = _RE_MULTI_DATE.search(remaining)
        if not m:
            break
        matched = m.group(0)
        y_str, mo_str, d1_str = m.group(1), m.group(2), m.group(3)
        extra_days_str = m.group(4)
        meal_str = m.group(5) or ""
        action_str = m.group(6)

        month = int(mo_str)
        year = _resolve_year(int(y_str), ref_year) if y_str else ref_year
        day_strs = [d1_str] + re.findall(r"(\d{1,2})", extra_days_str)
        meals = _detect_meals(meal_str) if meal_str else _detect_meals(matched)
        action = _action_type(action_str)

        if action in ("pause", "stop"):
            for ds in day_strs:
                try:
                    d = date(year, month, int(ds))
                except ValueError:
                    continue
                results.append(
                    {
                        "type": action,
                        "meals": meals,
                        "start": d,
                        "end": d,
                        "weekdays": None,
                        "raw": matched,
                    }
                )
        else:
            allowed: set[date] = set()
            for ds in day_strs:
                try:
                    allowed.add(date(year, month, int(ds)))
                except ValueError:
                    pass
            results.append(
                {
                    "type": "specific_dates",
                    "meals": meals,
                    "start": None,
                    "end": None,
                    "weekdays": None,
                    "allowed_dates": allowed,
                    "raw": matched,
                }
            )
        remaining = remaining[: m.start()] + remaining[m.end() :]

    return results, remaining


def _parse_yr_range(text: str, ref_year: int) -> tuple[list[dict], str]:
    r""" "YY/M/D~M/D 액션" — 범위 (연도 포함).

    예: 25/7/7~신규배송, 26/2/24~신규배송
    예: 23/11/20~석식일시중지

    종료일이 있으면 start~end, 없으면 start~ (open-ended).
    """
    results: list[dict] = []
    remaining = text

    while True:
        m = _RE_YR_RANGE.search(remaining)
        if not m:
            break
        matched = m.group(0)

        y1s, m1s, d1s = m.group(1), m.group(2), m.group(3)
        y2s, m2s, d2s = m.group(4), m.group(5), m.group(6)
        meal_str = m.group(7) or ""
        action_str = m.group(8)

        year1 = _resolve_year(int(y1s), ref_year) if y1s else ref_year
        d1 = date(year1, int(m1s), int(d1s))
        meals = _detect_meals(meal_str) if meal_str else _detect_meals(matched)
        action = _action_type(action_str)

        has_tilde = bool(re.search(r"[~∼]", matched[:20]))

        if m2s and d2s and has_tilde:
            year2 = (
                _resolve_year(int(y2s), ref_year)
                if y2s
                else (year1 if int(m2s) >= int(m1s) else year1 + 1)
            )
            d2 = date(year2, int(m2s), int(d2s))
            results.append(
                {
                    "type": action,
                    "meals": meals,
                    "start": d1,
                    "end": d2,
                    "weekdays": None,
                    "raw": matched,
                }
            )
        else:
            results.append(
                {
                    "type": action,
                    "meals": meals,
                    "start": d1,
                    "end": None,
                    "weekdays": None,
                    "raw": matched,
                }
            )
        remaining = remaining[: m.start()] + remaining[m.end() :]

    return results, remaining


def _parse_range_wk(text: str, ref_year: int) -> tuple[list[dict], str]:
    r""" "YY/M/D~요일1,요일2(meal) 액션" — 날짜범위 + 요일필터.

    예: 26/3/12~화,목(석식) 중지

    요일 파싱 실패 시 매치는 제거되지만 결과는 생성하지 않음.
    """
    results: list[dict] = []
    remaining = text

    while True:
        m = _RE_RANGE_WK.search(remaining)
        if not m:
            break
        matched = m.group(0)
        y_s, m_s, d_s = m.group(1), m.group(2), m.group(3)
        wk_str = m.group(4).replace(" ", "")
        paren = m.group(5) or ""
        inline_meal = m.group(6) or ""
        action_str = m.group(7)

        year = _resolve_year(int(y_s), ref_year) if y_s else ref_year
        month, start_d = int(m_s), int(d_s)

        weekdays = {_KOREAN_WEEKDAY[ch] for ch in wk_str if ch in _KOREAN_WEEKDAY}
        if not weekdays:
            remaining = remaining[: m.start()] + remaining[m.end() :]
            continue

        meal_source = paren or inline_meal
        meals = _detect_meals(meal_source) if meal_source else _detect_meals(matched)
        action = _action_type(action_str)

        d1 = date(year, month, start_d)
        results.append(
            {
                "type": action,
                "meals": meals,
                "start": d1,
                "end": None,
                "weekdays": weekdays,
                "raw": matched,
            }
        )
        remaining = remaining[: m.start()] + remaining[m.end() :]

    return results, remaining


def _parse_range_abbrev(text: str, ref_year: int) -> tuple[list[dict], str]:
    r""" "M/D~D 액션" — 같은 달 축약형.

    예: 12/5~9중지

    동일 월 내에서 start일~end일 범위를 나타냄.
    """
    results: list[dict] = []
    remaining = text

    while True:
        m = _RE_RANGE_ABBREV.search(remaining)
        if not m:
            break
        matched = m.group(0)
        y_s, m_s, d_s = m.group(1), m.group(2), m.group(3)
        end_d = int(m.group(4))
        meal_str = m.group(5) or ""
        action_str = m.group(6)

        year = _resolve_year(int(y_s), ref_year) if y_s else ref_year
        month, start_d = int(m_s), int(d_s)
        meals = _detect_meals(meal_str) if meal_str else _detect_meals(matched)
        action = _action_type(action_str)

        d1 = date(year, month, start_d)
        d2 = date(year, month, end_d)
        results.append(
            {
                "type": action,
                "meals": meals,
                "start": d1,
                "end": d2,
                "weekdays": None,
                "raw": matched,
            }
        )
        remaining = remaining[: m.start()] + remaining[m.end() :]

    return results, remaining


def _parse_tilde_end(text: str, ref_year: int) -> tuple[list[dict], str]:
    r""" "~M/D 액션" — 종료일만 지정.

    예: ~3/16 신규

    start=None, end=d 로 결과 생성.
    """
    results: list[dict] = []
    remaining = text

    while True:
        m = _RE_TILDE_END.search(remaining)
        if not m:
            break
        matched = m.group(0)
        y_s, m_s, d_s = m.group(1), m.group(2), m.group(3)
        meal_str = m.group(4) or ""
        action_str = m.group(5)

        year = _resolve_year(int(y_s), ref_year) if y_s else ref_year
        meals = _detect_meals(meal_str) if meal_str else _detect_meals(matched)
        action = _action_type(action_str)
        d = date(year, int(m_s), int(d_s))

        results.append(
            {
                "type": action,
                "meals": meals,
                "start": None,
                "end": d,
                "weekdays": None,
                "raw": matched,
            }
        )
        remaining = remaining[: m.start()] + remaining[m.end() :]

    return results, remaining


def _parse_single_date(text: str, ref_year: int) -> tuple[list[dict], str]:
    r"""단일 날짜 + 액션 (가장 일반적).

    예: 4/2석식일시중지, 4/14석식중지, 3/5~재배송
    예: 8/1부터 조식만배송희망

    ~ 또는 부터 가 있으면 open-ended (start only).
    """
    results: list[dict] = []
    remaining = text

    while True:
        m = _RE_SINGLE_DATE.search(remaining)
        if not m:
            break
        matched = m.group(0)
        y_s, m_s, d_s = m.group(1), m.group(2), m.group(3)
        meal_str = m.group(4) or ""
        paren_str = m.group(5) or ""
        action_str = m.group(6)

        if paren_str:
            meal_str = meal_str + paren_str

        year = _resolve_year(int(y_s), ref_year) if y_s else ref_year
        month, day = int(m_s), int(d_s)
        meals = _detect_meals(meal_str) if meal_str else _detect_meals(matched)
        action = _action_type(action_str)

        has_open = bool(re.search(r"[~∼]|부터", matched[:15]))

        if has_open:
            results.append(
                {
                    "type": action,
                    "meals": meals,
                    "start": date(year, month, day),
                    "end": None,
                    "weekdays": None,
                    "raw": matched,
                }
            )
        else:
            results.append(
                {
                    "type": action,
                    "meals": meals,
                    "start": date(year, month, day),
                    "end": date(year, month, day),
                    "weekdays": None,
                    "raw": matched,
                }
            )
        remaining = remaining[: m.start()] + remaining[m.end() :]

    return results, remaining


def _parse_irrelevant(results: list[dict]) -> list[dict]:
    """`_is_irrelevant` 필터: raw 텍스트가 무시할 내용인 결과 제거.

    개명, 흰우유, 별도메뉴, 주소 변경 등 급식 갯수와 무관한 항목 필터링.
    """
    return [r for r in results if not _is_irrelevant(r.get("raw", ""))]


# ── 패턴 디스크립터 테이블 (우선순위 순) ──

_DATE_PATTERNS: list[tuple[str, Callable]] = [
    ("multi_date", _parse_multi_date),
    ("yr_range", _parse_yr_range),
    ("range_wk", _parse_range_wk),
    ("range_abbrev", _parse_range_abbrev),
    ("tilde_end", _parse_tilde_end),
    ("single_date", _parse_single_date),
]


def _parse_date_patterns(text: str, ref_year: int) -> list[dict]:
    """날짜 기반 액션 파싱 (오케스트레이터).

    우선순위 순서로 각 패턴 파서를 순차 실행.
    각 파서는 처리한 부분을 remaining에서 제거하므로
    중복 매칭 없이 각 패턴이 독립적으로 동작.

    각 매치의 급식 종류는 matched text에서만 감지.
    명시된 급식이 없으면 전체(all meals)로 간주.
    """
    results: list[dict] = []
    remaining = text
    for _name, parse_fn in _DATE_PATTERNS:
        sub_results, remaining = parse_fn(remaining, ref_year)
        results.extend(sub_results)
    return results


# ══════════════════════════════════════════════════════
#  차감 건수 계산 — Pipeline Steps 1-4
# ══════════════════════════════════════════════════════


def apply_note_to_month(
    note: str,
    ref_year: int,
    month: int,
    flag_breakfast: bool,
    flag_dinner: bool,
    flag_lunch: bool,
    excluded_dates: set[date] | None = None,
) -> dict:
    """비고 → 급식종류별 배송 차감 건수 계산.

    Pipeline:
      1. parse_note → expand_note_dates: 비고 날짜를 개별 date 로 확장
      2. 해당 월 날짜만 유지 (expand_note_dates 가 처리)
      3. 공휴일·배송예외일 제거
      4. 배송 스케줄 기반으로 급식종류별 차감일 카운트
         (해당 요일에 그 급식종류가 배송되는 날만 차감)

    Returns:
        {"breakfast": int, "dinner": int, "lunch": int}
        각 급식종류별 비고로 인한 배송 차감일 수 (raw days).
        실제 배송일 = 기본배송일 - 이 값.
        조식: 주말(토·일)은 공휴일이어도 차감 대상에서 제외하지 않음.
    """
    # ── Steps 1-2: expand note dates ──
    date_meals = expand_note_dates(note, ref_year, month)

    # ── Step 3: remove holidays + custom excluded dates ──
    holidays = get_kr_holidays(ref_year)
    blocked = set(holidays) | (excluded_dates or set())
    if blocked:
        month_blocked = sorted(d for d in blocked if d.month == month)
        if month_blocked:
            log.debug(
                "[Memo] blocked dates in %04d-%02d: %s",
                ref_year,
                month,
                [str(d) for d in month_blocked],
            )

    # ── Step 4: count by schedule ──
    s = get_settings()
    schedule = {
        MealType.BREAKFAST: set(s.get("schedule", "breakfast_days", default=[FRI])),
        MealType.DINNER: set(
            s.get("schedule", "dinner_days", default=[MON, TUE, WED, THU])
        ),
        MealType.LUNCH: set(
            s.get("schedule", "lunch_days", default=[MON, TUE, WED, THU, FRI])
        ),
    }

    flags = {
        MealType.BREAKFAST: flag_breakfast,
        MealType.DINNER: flag_dinner,
        MealType.LUNCH: flag_lunch,
    }
    counts: dict[str, int] = {
        MealType.BREAKFAST: 0,
        MealType.DINNER: 0,
        MealType.LUNCH: 0,
    }

    for d, meals in date_meals:
        if d in blocked:
            # 조식 + 주말(토·일)이면 공휴일이어도 차감 대상
            if not (MealType.BREAKFAST in meals and d.weekday() in {SAT, SUN}):
                log.debug("[Memo] skip blocked date %s", d)
                continue
        for meal_type in meals:
            if not flags.get(meal_type):
                continue
            if d.weekday() in schedule[meal_type]:
                counts[meal_type] += 1
                log.debug("[Memo] %s: %s -%s", d, meal_type, meal_type)

    if date_meals:
        log.info(
            "[Memo] reduction: bf=%d dn=%d lh=%d (flags: bf=%s dn=%s lh=%s)",
            counts[MealType.BREAKFAST],
            counts[MealType.DINNER],
            counts[MealType.LUNCH],
            flag_breakfast,
            flag_dinner,
            flag_lunch,
        )
    return counts
