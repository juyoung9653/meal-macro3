"""Diff 표기 `3(+1)` 생성·파싱·판정 통합.

builder._fmt_cell / exporter._has_diff·_strip_diff / main._has_diff_cell
세 곳에 산재된 동일 로직을 여기로 모음.
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
