""" "YY/M/D~M/D 액션" — 범위 (연도 포함) 파서."""

import re
from datetime import date

from ._patterns import _RE_YR_RANGE, _action_type, _detect_meals, _resolve_year


def _parse_yr_range(text: str, ref_year: int) -> tuple[list[dict], str]:
    r""" "YY/M/D~M/D 액션" — 범위 (연도 포함).

    예: 25/7/7~신규배송, 26/2/24~신규배송
    예: 23/11/20~석식일시중지

    종료일이 있으면 start~end, 없으면 start~ (open-ended).
    """
    results: list[dict] = []
    remaining = text

    while True:
        m = _RE_YR_RANGE.search(remaining)
        if not m:
            break
        matched = m.group(0)

        y1s, m1s, d1s = m.group(1), m.group(2), m.group(3)
        y2s, m2s, d2s = m.group(4), m.group(5), m.group(6)
        meal_str = m.group(7) or ""
        action_str = m.group(8)

        year1 = _resolve_year(int(y1s), ref_year) if y1s else ref_year
        d1 = date(year1, int(m1s), int(d1s))
        meals = _detect_meals(meal_str) if meal_str else _detect_meals(matched)
        action = _action_type(action_str)

        has_tilde = bool(re.search(r"[~∼]", matched[:20]))

        if m2s and d2s and has_tilde:
            year2 = (
                _resolve_year(int(y2s), ref_year)
                if y2s
                else (year1 if int(m2s) >= int(m1s) else year1 + 1)
            )
            d2 = date(year2, int(m2s), int(d2s))
            results.append(
                {
                    "type": action,
                    "meals": meals,
                    "start": d1,
                    "end": d2,
                    "weekdays": None,
                    "raw": matched,
                }
            )
        else:
            results.append(
                {
                    "type": action,
                    "meals": meals,
                    "start": d1,
                    "end": None,
                    "weekdays": None,
                    "raw": matched,
                }
            )
        remaining = remaining[: m.start()] + remaining[m.end() :]

    return results, remaining
