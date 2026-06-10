"""중식 명단 데이터 모델 — 방학/개학 일정 + 비고 파싱 결과.

LunchSchoolEntry: 중식 명단의 학교별 방학 정보
LunchRoster: 전체 중식 명단 데이터
"""

from dataclasses import dataclass, field
from datetime import date


@dataclass
class LunchSchoolEntry:
    """중식 명단의 학교별 방학 정보."""

    school_name: str  # "검단초" (축약형)
    school_type: str = ""  # "초", "중", "고", "특수", "기타"
    vacation_date: date | None = None  # 방학식 일자
    opening_date: date | None = None  # 개학식 일자
    vacation_meal: bool = False  # 방학식 급식 제공 여부
    opening_meal: bool = False  # 개학식 급식 제공 여부
    note_raw: str = ""  # 원본 비고

    # ── 비고 파싱 결과 ──
    excluded_grades_vacation: set[int] = field(default_factory=set)
    excluded_grades_opening: set[int] = field(default_factory=set)
    all_grades_excluded_vacation: bool = False
    all_grades_excluded_opening: bool = False
    special_schedules: list[dict] = field(default_factory=list)
    # special_schedules: [{"start": date, "end": date, "meals": bool, "note": str}, ...]


@dataclass
class LunchRoster:
    """중식 명단 전체 데이터."""

    entries: list[LunchSchoolEntry] = field(default_factory=list)
    headers: list[str] = field(default_factory=list)
    file_path: str = ""

    def find_school(self, school_name: str) -> LunchSchoolEntry | None:
        """학교명(축약 또는 전체명)으로 중식 명단 항목 찾기. 부분일치 + difflib fallback."""
        import difflib

        # 1. 축약형 직접 매칭
        for entry in self.entries:
            if entry.school_name == school_name:
                return entry

        # 2. 축약형 변환 시도 (예: "분당초등학교" → "분당초")
        short = _shorten_school_name(school_name)
        for entry in self.entries:
            if entry.school_name == short:
                return entry

        # 3. 부분일치 (한쪽이 다른쪽 포함)
        for entry in self.entries:
            if entry.school_name in school_name or school_name in entry.school_name:
                return entry

        # 4. difflib fuzzy match
        names = [e.school_name for e in self.entries]
        matches = difflib.get_close_matches(short, names, n=1, cutoff=0.6)
        if matches:
            for entry in self.entries:
                if entry.school_name == matches[0]:
                    return entry

        # 5. 원본명으로 difflib
        matches = difflib.get_close_matches(school_name, names, n=1, cutoff=0.6)
        if matches:
            for entry in self.entries:
                if entry.school_name == matches[0]:
                    return entry

        return None

    def get_unmatched_schools(self, roster_school_names: set[str]) -> set[str]:
        """명단에 있지만 중식 명단에 없는 학교명 반환."""
        unmatched: set[str] = set()
        for name in roster_school_names:
            if self.find_school(name) is None:
                unmatched.add(name)
        return unmatched

    def is_vacation_month(self, year: int, month: int) -> bool:
        """해당 월이 하나라도 학교의 방학 배송 기간에 걸치는지 확인.

        방학 배송 기간: 방학식+1일 ~ 개학식-1일.
        어느 한 학교라도 기간에 포함되면 True.
        """
        import calendar as _cal
        import logging
        from datetime import timedelta

        _log = logging.getLogger(__name__)

        last_day = _cal.monthrange(year, month)[1]
        month_start = date(year, month, 1)
        month_end = date(year, month, last_day)

        _log.debug(
            "[VacCheck] %04d-%02d 검사 (총 %d개 학교)",
            year,
            month,
            len(self.entries),
        )

        for entry in self.entries:
            if not entry.vacation_meal and not entry.opening_meal:
                _log.debug(
                    "[VacCheck] %s: meal 둘 다 False → 건너뜀", entry.school_name
                )
                continue

            eff_start: date | None = None
            eff_end: date | None = None

            if entry.vacation_meal and entry.vacation_date:
                eff_start = entry.vacation_date + timedelta(days=1)
            if entry.opening_meal and entry.opening_date:
                eff_end = entry.opening_date - timedelta(days=1)

            if eff_start is None:
                eff_start = month_start
            if eff_end is None:
                eff_end = month_end

            _log.debug(
                "[VacCheck] %s: vac_d=%s open_d=%s → range=%s~%s (month=%s~%s)",
                entry.school_name,
                entry.vacation_date,
                entry.opening_date,
                eff_start,
                eff_end,
                month_start,
                month_end,
            )

            if eff_start <= month_end and eff_end >= month_start:
                _log.info(
                    "[VacCheck] %s: 방학 기간 겹침! (%s~%s)",
                    entry.school_name,
                    eff_start,
                    eff_end,
                )
                return True

        _log.info("[VacCheck] %04d-%02d: 방학 기간 아님", year, month)
        return False


def _shorten_school_name(name: str) -> str:
    """학교명 축약: '분당초등학교' → '분당초', '구미중학교' → '구미중'."""
    import re

    suffixes = [
        "초등학교",
        "중학교",
        "고등학교",
        "학교",
        "여자중학교",
        "여자고등학교",
        "남자중학교",
        "남자고등학교",
        "공업고등학교",
        "상업고등학교",
        "과학고등학교",
        "외국어고등학교",
        "예술고등학교",
        "체육고등학교",
    ]
    for suffix in sorted(suffixes, key=len, reverse=True):
        if name.endswith(suffix) and len(name) > len(suffix):
            short = name[: -len(suffix)]
            # Determine school type from original name
            if "초" in suffix:
                short += "초"
            elif "중" in suffix:
                short += "중"
            elif "고" in suffix:
                short += "고"
            return short
    return name


def parse_lunch_boolean(val) -> bool:
    """셀 값을 bool로 변환. O, Y, 1, 예, 있음 → True."""
    if isinstance(val, (int, float)):
        return val > 0
    if isinstance(val, str):
        v = val.strip().upper()
        return v in ("O", "Y", "1", "1.0", "예", "있음", "급식", "TRUE")
    return bool(val)
