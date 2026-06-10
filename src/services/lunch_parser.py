"""중식 명단 비고 파서 — 학교별 방학/개학 급식 제외 학년, 특수 일정 추출.

주요 패턴:
  - 학년 제외: "6학년 방학식 미급식", "1학년 개학식 미급식", "졸업식 6학년 급식 미제공"
  - 전학년 제외: "방학식 전학년 미급식", "전학년"
  - 학년별 포함: "1~5학년 급식 실시" (→ 나머지 학년 제외)
  - 특수 일정: "중간개학 2.2.~2.5.", "2/19~2/23(3일 급식)"
  - 불확실: "개학날짜 미확정", "학운위 결정에 따라 변경"
"""

import logging
import re
from datetime import date

from .lunch_data import LunchSchoolEntry

log = logging.getLogger(__name__)

# ── 학년 추출 ──

_GRADE_RE = re.compile(r"(\d{1,2})\s*학년")


def _extract_grades(text: str) -> set[int]:
    """텍스트에서 학년 숫자 추출. '6학년' → {6}, '1~5학년' → {1,2,3,4,5}."""
    grades: set[int] = set()

    # 범위 패턴: "1~5학년", "2-5학년"
    range_m = re.search(r"(\d{1,2})\s*[~\-]\s*(\d{1,2})\s*학년", text)
    if range_m:
        start, end = int(range_m.group(1)), int(range_m.group(2))
        for g in range(start, end + 1):
            grades.add(g)
    else:
        # 개별 학년
        for m in _GRADE_RE.finditer(text):
            grades.add(int(m.group(1)))

    return grades


def _extract_before(text: str, keywords: list[str]) -> str:
    """키워드 앞부분 텍스트 추출 (학년 정보 찾기용)."""
    idx = -1
    for kw in keywords:
        i = text.find(kw)
        if i >= 0 and (idx < 0 or i < idx):
            idx = i
    if idx >= 0:
        return text[:idx]
    return text


# ── 방학식/개학식 구분 키워드 ──

_VACATION_KW = [
    "방학식",
    "방학",
    "겨울방학식",
    "겨울방학",
    "졸업식",
    "졸업",
    "종업식",
]

_OPENING_KW = [
    "개학식",
    "개학",
    "입학식",
    "입학",
    "개학일",
    "입학일",
]


def _parse_grade_exclusions(note: str, entry: LunchSchoolEntry):
    """비고에서 학년별 제외 정보 파싱하여 entry에 채움."""
    if not note or not note.strip():
        return

    segments = _split_note(note)

    for seg in segments:
        seg = seg.strip()
        if not seg:
            continue

        # ── 전학년 제외 ──
        if re.search(r"전학년\s*(?:미급식|급식\s*(?:미실시|미제공|안함))", seg):
            entry.all_grades_excluded_vacation = True
            entry.all_grades_excluded_opening = True
            continue
        if re.search(r"방학식\s*전학년\s*(?:미급식|급식\s*(?:미실시|미제공))", seg):
            entry.all_grades_excluded_vacation = True
            continue
        if re.search(r"개학식\s*전학년\s*(?:미급식|급식\s*(?:미실시|미제공))", seg):
            entry.all_grades_excluded_opening = True
            continue

        # ── 학년 제외 + 방학식/개학식 판별 ──
        grades = _extract_grades(seg)
        if not grades:
            # "졸업" 키워드로 6학년 추론
            if any(kw in seg for kw in ["졸업식", "졸업"]) and "미급식" in seg:
                grades = {6}

        if not grades:
            continue

        # 방학식 관련 제외 여부
        has_vacation_kw = any(kw in seg for kw in _VACATION_KW)
        has_opening_kw = any(kw in seg for kw in _OPENING_KW)

        # 미급식/미실시/미제공/안함 키워드
        is_excluded = any(
            kw in seg
            for kw in [
                "미급식",
                "미실시",
                "미제공",
                "안함",
                "급식 미",
                "급식을 제공하지 않",
            ]
        )

        if not is_excluded:
            continue

        if has_vacation_kw and not has_opening_kw:
            entry.excluded_grades_vacation.update(grades)
        elif has_opening_kw and not has_vacation_kw:
            entry.excluded_grades_opening.update(grades)
        elif has_vacation_kw and has_opening_kw:
            # 둘 다 언급 → 문맥 판단
            # "방학식 6학년 미급식 / 개학식 1학년 미급식"
            for kw in _VACATION_KW:
                idx = seg.find(kw)
                if idx >= 0:
                    before = seg[:idx]
                    for m in _GRADE_RE.finditer(before):
                        g = int(m.group(1))
                        entry.excluded_grades_vacation.add(g)
                    break
            for kw in _OPENING_KW:
                idx = seg.find(kw)
                if idx >= 0:
                    before = seg[:idx]
                    for m in _GRADE_RE.finditer(before):
                        g = int(m.group(1))
                        if g not in entry.excluded_grades_vacation:
                            entry.excluded_grades_opening.add(g)
                    break
            # fallback: 둘 다 적용
            if not entry.excluded_grades_vacation and not entry.excluded_grades_opening:
                entry.excluded_grades_vacation.update(grades)
                entry.excluded_grades_opening.update(grades)
        else:
            # 방학/개학 키워드 없으면 둘 다로 추정
            entry.excluded_grades_vacation.update(grades)
            entry.excluded_grades_opening.update(grades)

    # ── "X~Y학년 급식 실시" → 나머지 학년 제외 ──
    for seg in segments:
        range_m = re.search(
            r"(\d{1,2})\s*[~\-]\s*(\d{1,2})\s*학년\s*(?:전체)?\s*급식\s*실시", seg
        )
        if range_m:
            start, end = int(range_m.group(1)), int(range_m.group(2))
            included = set(range(start, end + 1))
            all_grades = set(range(1, 7))
            excluded = all_grades - included
            for kw in _VACATION_KW:
                if kw in seg:
                    entry.excluded_grades_vacation.update(excluded)
                    break
            else:
                entry.excluded_grades_vacation.update(excluded)


# ── 특수 일정 파싱 ──

_DATE_RE = re.compile(r"(\d{1,2})[./](\d{1,2})")


def _parse_special_schedules(note: str, entry: LunchSchoolEntry, ref_year: int):
    """비고에서 중간개학 등 특수 일정 파싱."""
    if not note or not note.strip():
        return

    segments = _split_note(note)

    for seg in segments:
        seg = seg.strip()
        if not seg:
            continue

        # ── 중간개학 패턴 ──
        mid_m = re.search(
            r"중간개학\s*(\d{1,2}[./]\d{1,2})\s*[~]\s*(\d{1,2}[./]\d{1,2})", seg
        )
        if mid_m:
            s_date = _parse_date_str(mid_m.group(1), ref_year)
            e_date = _parse_date_str(mid_m.group(2), ref_year)
            # 급식 여부 판단
            meals_provided = not any(
                kw in seg for kw in ["미급식", "급식 미", "미실시"]
            )
            if s_date and e_date:
                entry.special_schedules.append(
                    {
                        "start": s_date,
                        "end": e_date,
                        "meals": meals_provided,
                        "note": seg,
                        "type": "mid_opening",
                    }
                )
                continue

        # ── 일반 날짜 범위 + 급식 ──
        range_m = re.search(
            r"(\d{1,2}[./]\d{1,2})\s*[~]\s*(\d{1,2}[./]\d{1,2})"
            r"\s*(?:\([^)]*\))?\s*(?:중\s*)?"
            r"(?:기간\s*)?(급식|미급식)",
            seg,
        )
        if range_m:
            s_date = _parse_date_str(range_m.group(1), ref_year)
            e_date = _parse_date_str(range_m.group(2), ref_year)
            meals = "미급식" not in range_m.group(0)
            if s_date and e_date:
                entry.special_schedules.append(
                    {
                        "start": s_date,
                        "end": e_date,
                        "meals": meals,
                        "note": seg,
                        "type": "date_range",
                    }
                )


def _parse_date_str(s: str, ref_year: int) -> date | None:
    """'2.2', '2/2' → date(ref_year, 2, 2)."""
    m = _DATE_RE.match(s.strip())
    if not m:
        return None
    month, day = int(m.group(1)), int(m.group(2))
    # 월이 7-12월이면 이전 연도 (겨울방학), 1-6월이면 다음 연도 추정
    if month >= 7 and ref_year > 2020:
        yr = ref_year - 1
    else:
        yr = ref_year
    try:
        return date(yr, month, day)
    except ValueError:
        return None


def _split_note(note: str) -> list[str]:
    """비고를 세그먼트로 분할."""
    # 여러 구분자로 분할
    # 1. "/ " (슬래시+공백) → " / " 형태로 분할 (날짜 슬래시와 구분)
    text = note.replace(" / ", "\n").replace("/ ", "\n")

    # 2. 개행, 쌍반점
    parts = re.split(r"\n|\r\n|;\s*", text)

    # 3. ". " 분할 (문장 구분, 날짜 점과 구분)
    result = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        # "단, " 으로 시작하는 경우도 분할
        sub = re.split(r"(?:단,?\s*)", p)
        for s in sub:
            s = s.strip()
            if s:
                result.append(s)
    return result


def parse_lunch_note(entry: LunchSchoolEntry, ref_year: int = 2025):
    """중식 명단 비고 파싱 → entry의 excluded_grades, special_schedules 채움."""
    note = entry.note_raw
    if not note or not note.strip():
        return

    _parse_grade_exclusions(note, entry)
    _parse_special_schedules(note, entry, ref_year)

    if entry.note_raw.strip():
        log.debug(
            "[LunchNote] %s: excl_vac=%s excl_open=%s all_vac=%s all_open=%s special=%s",
            entry.school_name,
            entry.excluded_grades_vacation,
            entry.excluded_grades_opening,
            entry.all_grades_excluded_vacation,
            entry.all_grades_excluded_opening,
            entry.special_schedules,
        )
