""" "M/D1,D2,... 액션" — 다중 날짜 (쉼표 구분) 파서."""

import re
from datetime import date

from ._patterns import _RE_MULTI_DATE, _action_type, _detect_meals, _resolve_year


def _parse_multi_date(text: str, ref_year: int) -> tuple[list[dict], str]:
    r""" "M/D1,D2,... 액션" — 다중 날짜 (쉼표 구분).

    예: 12/2,3,5,6,7,8,9,10,...일시중지
    예: 4/9,14 석식 일시중지
    예: 5/7,8,9,10,... 배송요청

    pause/stop: 각 날짜를 개별 차감일로 등록.
    배송(신규/재배송/추가): 명시된 날짜만 배송일 whitelist.
    """
    results: list[dict] = []
    remaining = text

    while True:
        m = _RE_MULTI_DATE.search(remaining)
        if not m:
            break
        matched = m.group(0)
        y_str, mo_str, d1_str = m.group(1), m.group(2), m.group(3)
        extra_days_str = m.group(4)
        meal_str = m.group(5) or ""
        action_str = m.group(6)

        month = int(mo_str)
        year = _resolve_year(int(y_str), ref_year) if y_str else ref_year
        day_strs = [d1_str] + re.findall(r"(\d{1,2})", extra_days_str)
        meals = _detect_meals(meal_str) if meal_str else _detect_meals(matched)
        action = _action_type(action_str)

        if action in ("pause", "stop"):
            for ds in day_strs:
                try:
                    d = date(year, month, int(ds))
                except ValueError:
                    continue
                results.append(
                    {
                        "type": action,
                        "meals": meals,
                        "start": d,
                        "end": d,
                        "weekdays": None,
                        "raw": matched,
                    }
                )
        else:
            allowed: set[date] = set()
            for ds in day_strs:
                try:
                    allowed.add(date(year, month, int(ds)))
                except ValueError:
                    pass
            results.append(
                {
                    "type": "specific_dates",
                    "meals": meals,
                    "start": None,
                    "end": None,
                    "weekdays": None,
                    "allowed_dates": allowed,
                    "raw": matched,
                }
            )
        remaining = remaining[: m.start()] + remaining[m.end() :]

    return results, remaining
