"""새 연간 배송 형식과 Excel 출력에서 공유하는 값 파싱 함수."""

from __future__ import annotations

import math
import re
from datetime import date, datetime
from typing import Any


def parse_annual_date_header(value: Any) -> date | None:
    """``YYYYMMDD`` 또는 Excel 날짜 값을 실제 날짜로 변환한다.

    레거시 ``M/D`` 형식과 Excel serial number는 의도적으로 지원하지 않는다.
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, bool):
        return None

    text: str
    if isinstance(value, str):
        text = value.strip()
    elif isinstance(value, (int, float)):
        if isinstance(value, float) and (
            not math.isfinite(value) or not value.is_integer()
        ):
            return None
        text = str(int(value))
    else:
        return None

    if not re.fullmatch(r"\d{8}", text):
        return None
    try:
        return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
    except ValueError:
        return None


def _to_str(value: Any) -> str:
    """Excel/데이터프레임 값을 표시용 문자열로 정규화한다."""
    if value is None:
        return ""
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return ""
    text = str(value).strip()
    if not text or text.lower() in {
        "nan",
        "inf",
        "infinity",
        "+nan",
        "-nan",
        "+inf",
        "-inf",
    }:
        return ""
    if re.fullmatch(r"-?\d+(\.0+)?", text):
        try:
            number = float(text)
            return "" if not math.isfinite(number) else str(int(number))
        except ValueError:
            return text
    return text
