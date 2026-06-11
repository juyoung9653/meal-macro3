""" "~M/D 액션" — 종료일만 지정 파서."""

from datetime import date

from ._patterns import _RE_TILDE_END, _action_type, _detect_meals, _resolve_year


def _parse_tilde_end(text: str, ref_year: int) -> tuple[list[dict], str]:
    r""" "~M/D 액션" — 종료일만 지정.

    예: ~3/16 신규

    start=None, end=d 로 결과 생성.
    """
    results: list[dict] = []
    remaining = text

    while True:
        m = _RE_TILDE_END.search(remaining)
        if not m:
            break
        matched = m.group(0)
        y_s, m_s, d_s = m.group(1), m.group(2), m.group(3)
        meal_str = m.group(4) or ""
        action_str = m.group(5)

        year = _resolve_year(int(y_s), ref_year) if y_s else ref_year
        meals = _detect_meals(meal_str) if meal_str else _detect_meals(matched)
        action = _action_type(action_str)
        d = date(year, int(m_s), int(d_s))

        results.append(
            {
                "type": action,
                "meals": meals,
                "start": None,
                "end": d,
                "weekdays": None,
                "raw": matched,
            }
        )
        remaining = remaining[: m.start()] + remaining[m.end() :]

    return results, remaining
