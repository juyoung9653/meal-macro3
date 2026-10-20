import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd
import win32com.client as win32

from src.config import BOX_SUFFIX, COL_MEAL, COL_ORD, COL_SHIP


@dataclass(frozen=True)
class Choice:
    meal: str
    ship: str
    flag_col: str
    cols: list[str]


def to_num(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    t = re.sub(r"[^\d.\-]", "", txt(v))
    try:
        return float(t) if t else None
    except ValueError:
        return None


def truthy(v: Any) -> bool:
    if v is None:
        return False
    if isinstance(v, float) and math.isnan(v):
        return False
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v == 1
    return txt(v).lower() in {"1", "y", "true"}


def txt(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return ""
    t = str(v).strip()
    if not t or t.lower() in {"nan", "inf", "infinity", "+nan", "-nan", "+inf", "-inf"}:
        return ""
    if re.fullmatch(r"-?\d+(\.0+)?", t):
        try:
            f = float(t)
            return "" if (math.isnan(f) or math.isinf(f)) else str(int(f))
        except ValueError:
            return t
    return t


def load_excel(path: Path, sheet: str, pw: str = "") -> pd.DataFrame:
    target_path = str(path.resolve()).lower()  # 경로 표준화 및 소문자 변환
    excel = None
    wb = None
    is_new_app = False
    is_new_wb = False

    try:
        # 1. 엑셀 인스턴스 가져오기 (이미 있으면 활용, 없으면 새로 생성)
        try:
            excel = win32.GetActiveObject("Excel.Application")
        except Exception:
            excel = win32.Dispatch("Excel.Application")
            excel.Visible = False
            is_new_app = True

        excel.DisplayAlerts = True  # 비번 입력창 등을 위해 활성화

        # 2. 이미 열려 있는지 확인 (일반 모드 & 제한된 보기 통합 체크)
        # 일반 워크북 확인
        for w in excel.Workbooks:
            if w.FullName.lower() == target_path:
                wb = w
                break

        # 제한된 보기(Protected View) 확인 및 일반 모드로 전환
        if not wb:
            for pv in excel.ProtectedViewWindows:
                # SourceName이 아닌 FullName(또는 SourceFullName) 비교 권장
                if pv.SourceFullName.lower() == target_path:
                    wb = pv.Edit()
                    break

        # 3. 어디에도 없다면 새로 열기
        if not wb:
            wb = excel.Workbooks.Open(target_path, Password=pw, ReadOnly=True)
            is_new_wb = True

        return read_df(wb.Worksheets(sheet))

    finally:
        # 4. 자원 정리 (우리가 새로 만든 것만 닫음)
        if wb and is_new_wb:
            wb.Close(SaveChanges=False)
        if excel and is_new_app:
            excel.Quit()
        excel = None


def read_df(ws) -> pd.DataFrame:
    table = next((t for t in ws.ListObjects if t.Name.lower() == "db"), None)
    if table is None:
        raise ValueError(f"'{ws.Name}' 시트에서 'db' 테이블을 찾지 못했습니다.")

    headers = [txt(x) for x in range_rows(table.HeaderRowRange)[0]]
    data = table.DataBodyRange
    body = [] if data is None else range_rows(data)
    return pd.DataFrame(body, columns=headers)


def range_rows(excel_range) -> list[list[Any]]:
    value = excel_range.Value
    if not isinstance(value, tuple):
        return [[value]]
    return [list(row) if isinstance(row, tuple) else [row] for row in value]


def read_range(ws, r1: int, c1: int, r2: int, c2: int) -> list[list[Any]]:
    v = ws.Range(f"{a1(c1)}{r1}", f"{a1(c2)}{r2}").Value
    if v is None:
        return []
    if not isinstance(v, tuple):
        return [[v]]
    return [list(row) if isinstance(row, tuple) else [row] for row in v]


def a1(n: int) -> str:
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def uniq(df: pd.DataFrame, col: str) -> list[str]:
    if col not in df.columns:
        return []
    s1 = df[col].map(txt)
    return s1.loc[s1 != ""].drop_duplicates().sort_values(kind="mergesort").tolist()


def active_combinations(df: pd.DataFrame, flag_col: str) -> list[tuple[str, str]]:
    """선택 날짜에 배송 표시된 (기본 식종, 배송담당) 조합을 반환합니다."""
    f = txt(flag_col)
    if f not in df.columns or COL_MEAL not in df.columns or COL_SHIP not in df.columns:
        return []

    active = df[df[f].map(truthy)]
    combinations: set[tuple[str, str]] = set()
    for meal_value, ship_value in zip(active[COL_MEAL], active[COL_SHIP]):
        meal = txt(meal_value)
        ship = txt(ship_value)
        meal = meal.removesuffix(BOX_SUFFIX)
        if meal and ship:
            combinations.add((meal, ship))
    return sorted(combinations)


def make_result(df: pd.DataFrame, ch: Choice) -> pd.DataFrame:
    f = txt(ch.flag_col)
    if f not in df.columns:
        raise KeyError(f"'{ch.flag_col}' 헤더를 찾지 못했습니다. (정규화: '{f}')")

    x = df
    if ch.meal and COL_MEAL in x.columns:
        selected_meal = txt(ch.meal)
        meal_values = {selected_meal}
        if not selected_meal.endswith(BOX_SUFFIX):
            meal_values.add(f"{selected_meal}{BOX_SUFFIX}")
        x = x[x[COL_MEAL].map(txt).isin(meal_values)]
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


def ord_groups(vals: list[str]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    i, n = 0, len(vals)
    while i < n:
        j = i
        while j + 1 < n and vals[j + 1] == vals[i]:
            j += 1
        out.append((i, j))
        i = j + 1
    return out
