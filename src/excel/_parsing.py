"""Excel 파싱 헬퍼 — 순수 함수. DbReader, DataSource와 무관하게 사용 가능."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any


def _serial_to_date(serial: int | float) -> date | None:
    if not isinstance(serial, (int, float)) or serial < 1:
        return None
    try:
        return date(1899, 12, 30) + timedelta(days=int(serial))
    except (ValueError, OverflowError):
        return None


def _to_date(val: Any) -> date | None:
    if val is None:
        return None
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, date):
        return val
    if isinstance(val, (int, float)):
        return _serial_to_date(val)
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return None
        for sep in ("-", ".", "/"):
            parts = s.split(sep)
            if len(parts) == 3:
                try:
                    y, m, d = int(parts[0]), int(parts[1]), int(parts[2])
                    if 2000 <= y <= 2100 and 1 <= m <= 12 and 1 <= d <= 31:
                        return date(y, m, d)
                except (ValueError, OverflowError):
                    pass
    return None


def _to_str(val: Any) -> str:
    if val is None:
        return ""
    return str(val).strip()


def _is_date_col(name: str) -> bool:
    """M/D 형식 컬럼명 확인. '6/1 월' 같이 요일이 붙어도 허용."""
    if not name or "/" not in name:
        return False
    # '6/1 월' → ['6', '1 월'] → 첫 번째 토큰만 정수 파싱
    parts = name.split("/")
    if len(parts) < 2:
        return False
    try:
        m = int(parts[0])
        # 두 번째 부분에서 숫자만 추출 (ex: '1 월' → 1, '15' → 15)
        d_str = parts[1].split()[0] if parts[1].strip() else ""
        d = int(d_str)
        return 1 <= m <= 12 and 1 <= d <= 31
    except (ValueError, TypeError):
        return False


def _parse_date_col(name: str) -> tuple[int, int] | None:
    """'6/15' → (6, 15). '6/15 월' 등 요일 접미사도 처리."""
    if not _is_date_col(name):
        return None
    parts = name.split("/")
    m = int(parts[0])
    d = int(parts[1].split()[0])  # '1 월' → 1
    return m, d
