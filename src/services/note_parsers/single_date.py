"""단일 날짜 + 액션 (가장 일반적) 파서."""

import re
from datetime import date

from ._patterns import _RE_SINGLE_DATE, _action_type, _detect_meals, _resolve_year


def _parse_single_date(text: str, ref_year: int) -> tuple[list[dict], str]:
    r"""단일 날짜 + 액션 (가장 일반적).

    예: 4/2석식일시중지, 4/14석식중지, 3/5~재배송
    예: 8/1부터 조식만배송희망

    ~ 또는 부터 가 있으면 open-ended (start only).
    """
    results: list[dict] = []
    remaining = text

    while True:
        m = _RE_SINGLE_DATE.search(remaining)
        if not m:
            break
        matched = m.group(0)
        y_s, m_s, d_s = m.group(1), m.group(2), m.group(3)
        meal_str = m.group(4) or ""
        paren_str = m.group(5) or ""
        action_str = m.group(6)

        if paren_str:
            meal_str = meal_str + paren_str

        year = _resolve_year(int(y_s), ref_year) if y_s else ref_year
        month, day = int(m_s), int(d_s)
        meals = _detect_meals(meal_str) if meal_str else _detect_meals(matched)
        action = _action_type(action_str)

        has_open = bool(re.search(r"[~∼]|부터", matched[:15]))

        if has_open:
            results.append(
                {
                    "type": action,
                    "meals": meals,
                    "start": date(year, month, day),
                    "end": None,
                    "weekdays": None,
                    "raw": matched,
                }
            )
        else:
            results.append(
                {
                    "type": action,
                    "meals": meals,
                    "start": date(year, month, day),
                    "end": date(year, month, day),
                    "weekdays": None,
                    "raw": matched,
                }
            )
        remaining = remaining[: m.start()] + remaining[m.end() :]

    return results, remaining
