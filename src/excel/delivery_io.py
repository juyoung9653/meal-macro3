from dataclasses import dataclass
from typing import Any

import pandas as pd

from src.core.settings import get_settings
from src.excel._parsing import _to_str, to_num, truthy

_S = get_settings()
COL_MEAL = _S.get_delivery_config("col_meal")
COL_ORD = _S.get_delivery_config("col_ord")
COL_SHIP = _S.get_delivery_config("col_ship")


@dataclass(frozen=True)
class Choice:
    meal: str
    ship: str
    flag_col: str
    cols: list[str]


def txt(v: Any) -> str:
    return _to_str(v)


def uniq(df: pd.DataFrame, col: str) -> list[str]:
    if col not in df.columns:
        return []
    s1 = df[col].map(txt)
    return s1.loc[s1 != ""].drop_duplicates().sort_values(kind="mergesort").tolist()


def make_result(df: pd.DataFrame, ch: Choice) -> pd.DataFrame:
    f = txt(ch.flag_col)
    if f not in df.columns:
        raise KeyError(f"'{ch.flag_col}' 헤더를 찾지 못했습니다. (정규화: '{f}')")

    x = df
    if ch.meal and COL_MEAL in x.columns:
        meal_str = txt(ch.meal)
        x = x[x[COL_MEAL].map(txt).str.startswith(meal_str)]
    if ch.ship and COL_SHIP in x.columns:
        x = x[x[COL_SHIP].map(txt) == txt(ch.ship)]

    x = x[x[f].map(truthy)].copy()

    if COL_ORD in x.columns:
        x = (
            x.assign(_o=x[COL_ORD].map(to_num))
            .sort_values("_o", kind="mergesort")
            .drop(columns="_o")
        )

    cols = [c for c in ch.cols if c in x.columns]
    return x.loc[:, cols] if cols else x
