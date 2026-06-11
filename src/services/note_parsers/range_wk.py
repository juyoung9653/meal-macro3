""" "YY/M/D~요일1,요일2(meal) 액션" — 날짜범위 + 요일필터 파서."""

from datetime import date

from ._patterns import (
    _KOREAN_WEEKDAY,
    _RE_RANGE_WK,
    _action_type,
    _detect_meals,
    _resolve_year,
)


def _parse_range_wk(text: str, ref_year: int) -> tuple[list[dict], str]:
    r""" "YY/M/D~요일1,요일2(meal) 액션" — 날짜범위 + 요일필터.

    예: 26/3/12~화,목(석식) 중지

    요일 파싱 실패 시 매치는 제거되지만 결과는 생성하지 않음.
    """
    results: list[dict] = []
    remaining = text

    while True:
        m = _RE_RANGE_WK.search(remaining)
        if not m:
            break
        matched = m.group(0)
        y_s, m_s, d_s = m.group(1), m.group(2), m.group(3)
        wk_str = m.group(4).replace(" ", "")
        paren = m.group(5) or ""
        inline_meal = m.group(6) or ""
        action_str = m.group(7)

        year = _resolve_year(int(y_s), ref_year) if y_s else ref_year
        month, start_d = int(m_s), int(d_s)

        weekdays = {_KOREAN_WEEKDAY[ch] for ch in wk_str if ch in _KOREAN_WEEKDAY}
        if not weekdays:
            remaining = remaining[: m.start()] + remaining[m.end() :]
            continue

        meal_source = paren or inline_meal
        meals = _detect_meals(meal_source) if meal_source else _detect_meals(matched)
        action = _action_type(action_str)

        d1 = date(year, month, start_d)
        results.append(
            {
                "type": action,
                "meals": meals,
                "start": d1,
                "end": None,
                "weekdays": weekdays,
                "raw": matched,
            }
        )
        remaining = remaining[: m.start()] + remaining[m.end() :]

    return results, remaining
