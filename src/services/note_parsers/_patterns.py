"""공유 상수 및 헬퍼 — 모든 날짜 패턴 파서에서 사용."""

import re

from ...core.date_utils import FRI, MON, SAT, SUN, THU, TUE, WED
from ...core.meal_types import ALL_MEALS, MealType

# ── 요일 alias ──

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


# ── 날짜 패턴 정규식 (모듈 레벨로 미리 컴파일) ──

# 4a. "M/D1,D2,... 액션" (다중 날짜, 쉼표로 구분)
_RE_MULTI_DATE = re.compile(
    r"(?:(\d{2,4})[./])?(\d{1,2})[./](\d{1,2})"
    r"((?:,\s*\d{1,2})+)"
    r"\s*(조식|석식|중식|방중)?\s*"
    r"(일시\s*중지|일시중지|중지|배송)"
)

# 4b. "YY/M/D~M/D 액션" (범위, 연도포함)
_RE_YR_RANGE = re.compile(
    r"(?:(\d{2,4})[./])?(\d{1,2})[./](\d{1,2})"
    r"\s*(?:[~∼]|부터)\s*"
    r"(?:(?:(\d{2,4})[./])?(\d{1,2})[./](\d{1,2})\s*)?"
    r"((?:조식|석식|중식|방중)(?:,\s*(?:조식|석식|중식|방중))*)?\s*"
    r"(일시\s*중지|일시중지|중지|재배송|신규\s*배송|신규|추가\s*배송|배송)"
)

# 4b2. "YY/M/D~요일1,요일2(meal) 액션" (날짜범위 + 요일필터)
_RE_RANGE_WK = re.compile(
    r"(?:(\d{2,4})[./])?(\d{1,2})[./](\d{1,2})"
    r"\s*[~∼]\s*"
    r"([월화수목금토일]+(?:,\s*[월화수목금토일]+)*)"
    r"\s*(?:요일)?\s*"
    r"(?:\(([^)]*)\))?\s*"
    r"((?:조식|석식|중식|방중)(?:,\s*(?:조식|석식|중식|방중))*)?\s*"
    r"(일시\s*중지|일시중지|중지)"
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
