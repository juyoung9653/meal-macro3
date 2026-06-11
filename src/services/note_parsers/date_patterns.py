"""날짜 기반 액션 파싱 오케스트레이터.

우선순위 순서로 각 패턴 파서를 순차 실행.
"""

from typing import Callable

from .multi_date import _parse_multi_date
from .range_abbrev import _parse_range_abbrev
from .range_wk import _parse_range_wk
from .single_date import _parse_single_date
from .tilde_end import _parse_tilde_end
from .yr_range import _parse_yr_range

# ── 패턴 디스크립터 테이블 (우선순위 순) ──

_DATE_PATTERNS: list[tuple[str, Callable]] = [
    ("multi_date", _parse_multi_date),
    ("yr_range", _parse_yr_range),
    ("range_wk", _parse_range_wk),
    ("range_abbrev", _parse_range_abbrev),
    ("tilde_end", _parse_tilde_end),
    ("single_date", _parse_single_date),
]


def _parse_date_patterns(text: str, ref_year: int) -> list[dict]:
    """날짜 기반 액션 파싱 (오케스트레이터).

    우선순위 순서로 각 패턴 파서를 순차 실행.
    각 파서는 처리한 부분을 remaining에서 제거하므로
    중복 매칭 없이 각 패턴이 독립적으로 동작.

    각 매치의 급식 종류는 matched text에서만 감지.
    명시된 급식이 없으면 전체(all meals)로 간주.
    """
    results: list[dict] = []
    remaining = text
    for _name, parse_fn in _DATE_PATTERNS:
        sub_results, remaining = parse_fn(remaining, ref_year)
        results.extend(sub_results)
    return results
