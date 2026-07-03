"""
Diff 표기 `3(+1)` 생성·파싱·판정 통합.
"""

from __future__ import annotations

import re

_RE_DIFF = re.compile(r"^(\d+)\([+-]\d+\)$")


def fmt_cell(cur: int, prv: int) -> str:
    """cur(±diff) 문자열 생성. builder._fmt_cell 대체."""
    if cur == prv:
        return str(cur) if cur else ""
    diff = cur - prv
    return f"{cur}({diff:+d})"


def has_diff(val: object) -> bool:
    """diff 표기(예: 3(+1)) 존재 여부."""
    return bool(val and _RE_DIFF.match(str(val)))


def strip_diff(val: object) -> str:
    """'3(+1)' → '3', '' → ''. exporter._strip_diff 대체."""
    s = str(val) if val is not None else ""
    m = _RE_DIFF.match(s)
    return m.group(1) if m else s


def to_numeric(val: object) -> int | float | object:
    """Convert string to int/float if possible. Return original if not.

    "3" → 3, "3.14" → 3.14, "010-1234" → "010-1234" (unchanged)
    """
    if isinstance(val, (int, float)):
        return val
    if not isinstance(val, str) or not val.strip():
        return val
    s = val.strip()
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        return val
