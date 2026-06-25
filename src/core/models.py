"""Enriched person dict builder — replaces old RosterPerson/EnrichedPerson ghost types.

Usage::
    from src.core.models import build_enriched_person_dict
    d = build_enriched_person_dict(raw={"성명": "홍길동"}, flag_breakfast=True)
"""


def build_enriched_person_dict(
    raw: dict,
    name: str = "",
    dept: str = "",
    note: str = "",
    flag_breakfast: bool = False,
    flag_dinner: bool = False,
    flag_lunch: bool = False,
    _total: int = 0,
    _amount: str = "",
) -> dict:
    """Build enriched person dict in one step (replaces RosterPerson + EnrichedPerson + person_to_dict).

    Parameters
    ----------
    raw : dict
        Original row dict (e.g. from roster data).
    name : str
        Person name (convenience, not added to output — already in raw).
    dept : str
        Department/school name (convenience, not added to output — already in raw).
    note : str
        Free-text note.
    flag_breakfast, flag_dinner, flag_lunch : bool
        Meal-day flags.
    _total : int
        Computed total count.
    _amount : str
        Computed amount string.

    Returns
    -------
    dict
        Flat dict combining raw fields with enriched ``_``-prefixed fields.
    """
    d = dict(raw)
    d.update(
        {
            "_breakfast": 1 if flag_breakfast else 0,
            "_dinner": 1 if flag_dinner else 0,
            "_lunch": 1 if flag_lunch else 0,
            "_note": note,
            "_total": _total,
            "_amount": _amount,
        }
    )
    return d
