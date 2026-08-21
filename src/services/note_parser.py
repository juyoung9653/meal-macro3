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

from ..core.date_utils import SAT, SUN, date_range
from ..core.meal_types import MealType
from ..core.settings import get_settings
from .holiday_fetcher import get_kr_holidays
from .note_parsers._patterns import (
    _KOREAN_WEEKDAY,
    _action_type,
    _detect_meals,
    _resolve_year,
)
from .note_parsers.date_patterns import _parse_date_patterns

log = logging.getLogger(__name__)


_PAUSE_THROUGH_DELIVERY_RE = re.compile(
    r"(?:(?P<start_year>\d{2,4})\s*[./년]\s*)?"
    r"(?:(?P<start_month>\d{1,2})\s*[./월]\s*)?"
    r"(?P<start_day>\d{1,2})\s*일?\s*"
    r"(?P<middle>(?:(?!\d{1,2}\s*(?:일|[./월])).){0,30}?)"
    r"(?:일시\s*)?중지\s*"
    r"(?:(?P<end_year>\d{2,4})\s*[./년]\s*)?"
    r"(?:(?P<end_month>\d{1,2})\s*[./월]\s*)?"
    r"(?P<end_day>\d{1,2})\s*일?\s*"
    r"(?P<suffix>.{0,20}?)"
    r"(?:부터\s*)?(?:재)?배송"
)


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
    """개행, 쌍반점 기준 분할. 쉼표는 날짜 구분자로 유지.
    ``//`` 포함된 줄은 주석으로 간주하여 무시."""
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
        # ``//`` 이후는 주석 처리 (인라인)
        if "//" in p:
            p = p.split("//", 1)[0].strip()
        if p and p not in ("0", "", '"'):
            result.append(p)
    return result


# ══════════════════════════════════════════════════════
#  메인 파서
# ══════════════════════════════════════════════════════


def parse_note(note: str, ref_year: int, ref_month: int | None = None) -> list[dict]:
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
        results = _parse_segment(seg, ref_year, ref_month)
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

    adjustments = parse_note(note, ref_year, month)
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


# ══════════════════════════════════════════════════════
#  세그먼트 파싱 — 오케스트레이터
# ══════════════════════════════════════════════════════


def _parse_segment(
    seg: str, ref_year: int, ref_month: int | None = None
) -> list[dict]:
    """단일 세그먼트 처리."""
    results: list[dict] = []
    remaining = seg

    # 0. "17일 중지 19일 배송"은 배송 재개 전날인 18일까지 중지한다.
    # 월이 생략되면 expand_note_dates()가 전달한 조회 월을 사용한다.
    while True:
        m = _PAUSE_THROUGH_DELIVERY_RE.search(remaining)
        if not m:
            break
        start_month_text = m.group("start_month")
        end_month_text = m.group("end_month")
        if not start_month_text and ref_month is None:
            break

        start_year = (
            _resolve_year(int(m.group("start_year")), ref_year)
            if m.group("start_year")
            else ref_year
        )
        start_month = int(start_month_text or ref_month)
        end_month = int(end_month_text or start_month)
        if m.group("end_year"):
            end_year = _resolve_year(int(m.group("end_year")), ref_year)
        elif end_month < start_month:
            end_year = start_year + 1
        else:
            end_year = start_year

        try:
            start = date(start_year, start_month, int(m.group("start_day")))
            resume_date = date(end_year, end_month, int(m.group("end_day")))
        except ValueError:
            remaining = remaining[: m.start()] + remaining[m.end() :]
            continue

        matched = m.group(0)
        end = resume_date - timedelta(days=1)
        if start <= end:
            results.append(
                {
                    "type": "pause",
                    "meals": _detect_meals(matched),
                    "start": start,
                    "end": end,
                    "weekdays": None,
                    "raw": matched,
                }
            )
        remaining = remaining[: m.start()] + remaining[m.end() :]

    # 1. "방학중식 중지" 특수 패턴
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

    # 2. 매주X요일 패턴 (예: 매주월,목일시중지(석식만))
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

    # 3. 요일 지정 배송 패턴 (예: 화,목(석식)만 배송희망, 월,수 배송(석식))
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

    # 4. 날짜 기반 패턴들
    results.extend(_parse_date_patterns(remaining, ref_year))

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
        MealType.BREAKFAST: s.get_schedule_days("breakfast"),
        MealType.DINNER: s.get_schedule_days("dinner"),
        MealType.LUNCH: s.get_schedule_days("lunch"),
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
