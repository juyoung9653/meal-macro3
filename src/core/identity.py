"""사람 식별에 사용하는 이름·생년월일 정규화."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any


def normalize_identity_name(value: Any) -> str:
    """공백과 대소문자 차이를 제거한 이름을 반환한다."""

    return re.sub(r"\s+", "", str(value or "")).casefold()


def _format_date(year: int, month: int, day: int) -> str:
    try:
        return date(year, month, day).strftime("%Y%m%d")
    except ValueError:
        return ""


def _expand_short_year(year: int) -> int:
    """두 자리 연도를 네 자리로 확장한다.

    급식 대상자 명단의 최근 출생연도는 2000년대로, 그 밖의 값은
    1900년대로 해석한다. 경계값은 실행 연도와 무관하도록 고정한다.
    """

    return 2000 + year if year <= 49 else 1900 + year


def _resident_birthdate(digits: str) -> str:
    """주민등록번호 앞 7자리 이상에서 생년월일을 복원한다."""

    if len(digits) < 7:
        return ""
    century_by_code = {
        "0": 1800,
        "9": 1800,
        "1": 1900,
        "2": 1900,
        "5": 1900,
        "6": 1900,
        "3": 2000,
        "4": 2000,
        "7": 2000,
        "8": 2000,
    }
    century = century_by_code.get(digits[6])
    if century is None:
        return ""
    return _format_date(
        century + int(digits[:2]),
        int(digits[2:4]),
        int(digits[4:6]),
    )


def normalize_birthdate(value: Any) -> str:
    """일반적인 생년월일 표기를 ``YYYYMMDD``로 통일한다.

    날짜 객체, 엑셀 날짜 일련번호, 8/6자리 숫자, 구분자가 있는 날짜,
    주민등록번호 형식을 지원한다. 해석할 수 없는 값은 기존처럼 숫자만
    남겨 서로 다른 원본을 함부로 같은 사람으로 합치지 않는다.
    """

    if value is None or value == "":
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y%m%d")
    if isinstance(value, date):
        return value.strftime("%Y%m%d")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if number.is_integer() and 40000 <= number <= 80000:
            try:
                excel_date = date(1899, 12, 30) + timedelta(days=int(number))
                return excel_date.strftime("%Y%m%d")
            except (OverflowError, ValueError):
                pass

    raw = str(value).strip()
    if not raw:
        return ""

    separated = re.match(
        r"^(\d{2}|\d{4})\D+(\d{1,2})\D+(\d{1,2})(?:\D.*)?$",
        raw,
    )
    if separated:
        raw_year, raw_month, raw_day = separated.groups()
        year = int(raw_year)
        if len(raw_year) == 2:
            year = _expand_short_year(year)
        normalized = _format_date(year, int(raw_month), int(raw_day))
        if normalized:
            return normalized

    digits = re.sub(r"\D", "", raw)
    if len(digits) in (7, 13):
        normalized = _resident_birthdate(digits)
        if normalized:
            return normalized
    if len(digits) >= 8:
        normalized = _format_date(
            int(digits[:4]),
            int(digits[4:6]),
            int(digits[6:8]),
        )
        if normalized:
            return normalized
    if len(digits) == 6:
        normalized = _format_date(
            _expand_short_year(int(digits[:2])),
            int(digits[2:4]),
            int(digits[4:6]),
        )
        if normalized:
            return normalized

    return digits or normalize_identity_name(raw)
