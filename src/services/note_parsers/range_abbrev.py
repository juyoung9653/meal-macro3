""" "M/D~D 액션" — 같은 달 축약형 파서."""

from datetime import date

from ._patterns import _RE_RANGE_ABBREV, _action_type, _detect_meals, _resolve_year


def _parse_range_abbrev(text: str, ref_year: int) -> tuple[list[dict], str]:
    r""" "M/D~D 액션" — 같은 달 축약형.

    예: 12/5~9중지

    동일 월 내에서 start일~end일 범위를 나타냄.
    """
    results: list[dict] = []
    remaining = text

    while True:
        m = _RE_RANGE_ABBREV.search(remaining)
        if not m:
            break
        matched = m.group(0)
        y_s, m_s, d_s = m.group(1), m.group(2), m.group(3)
        end_d = int(m.group(4))
        meal_str = m.group(5) or ""
        action_str = m.group(6)

        year = _resolve_year(int(y_s), ref_year) if y_s else ref_year
        month, start_d = int(m_s), int(d_s)
        meals = _detect_meals(meal_str) if meal_str else _detect_meals(matched)
        action = _action_type(action_str)

        d1 = date(year, month, start_d)
        d2 = date(year, month, end_d)
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
        remaining = remaining[: m.start()] + remaining[m.end() :]

    return results, remaining
