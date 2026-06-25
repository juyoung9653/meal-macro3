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


def _normalize_headers(raw_headers: list[str]) -> list[str]:
    """Deduplicate empty/duplicate headers.

    - Replace \n with space, strip
    - Empty headers → f"Col{idx}"
    - Duplicate headers → f"{name}_{count}"
    """
    headers: list[str] = []
    seen: dict[str, int] = {}
    for h in raw_headers:
        h = h.replace("\n", " ").strip()
        if not h:
            h = f"Col{len(headers)}"
        if h in seen:
            seen[h] += 1
            h = f"{h}_{seen[h]}"
        else:
            seen[h] = 1
        headers.append(h)
    return headers


def _rows_to_dicts(headers: list[str], rows: list[list]) -> list[dict]:
    """Convert headers + data rows to list[dict]. Skip all-empty rows."""
    data: list[dict] = []
    for row in rows:
        vals = list(row)
        entry = {
            headers[i]: _to_str(vals[i]) if i < len(vals) else ""
            for i in range(len(headers))
        }
        if all(not v or v == "" for v in entry.values()):
            continue
        data.append(entry)
    return data


def _parse_date_col(name: str) -> tuple[int, int] | None:
    """'6/15' → (6, 15). '6/15 월' 등 요일 접미사도 처리."""
    if not _is_date_col(name):
        return None
    parts = name.split("/")
    m = int(parts[0])
    d = int(parts[1].split()[0])  # '1 월' → 1
    return m, d
