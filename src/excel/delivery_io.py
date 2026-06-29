from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd
import win32com.client as win32

from src.core.settings import get_settings
from src.excel._parsing import _to_str, to_num, truthy
from src.excel.groups import ord_groups

_S = get_settings()
COL_MEAL = _S.get_delivery_config("col_meal")
COL_ORD = _S.get_delivery_config("col_ord")
COL_SHIP = _S.get_delivery_config("col_ship")
DATA_ROW = _S.get_delivery_config("data_row")
HEADER_ROW = _S.get_delivery_config("header_row")


@dataclass(frozen=True)
class Choice:
    meal: str
    ship: str
    flag_col: str
    cols: list[str]


def txt(v: Any) -> str:
    return _to_str(v)


def load_excel(path: Path, sheet: str, pw: str = "") -> pd.DataFrame:
    """Excel 파일에서 데이터를 읽어 DataFrame으로 반환.

    내부적으로 win32com을 사용하며, 함수 종료 시 새로 연 엑셀/워크북은 정리함.
    read_df 실행 중 예외 발생 시에도 finally에서 자원 정리 보장.

    Raises:
        FileNotFoundError: 파일이 존재하지 않을 때
        KeyError: 시트를 찾을 수 없을 때 (가능한 시트명 포함)
        pywintypes.com_error: 엑셀 COM 오류 (비밀번호 오류 등)
    """
    target_path = str(path.resolve()).lower()
    excel = None
    wb = None
    is_new_app = False
    is_new_wb = False

    try:
        # 1. 엑셀 인스턴스 확보
        try:
            excel = win32.GetActiveObject("Excel.Application")
        except Exception:
            excel = win32.Dispatch("Excel.Application")
            excel.Visible = False
            is_new_app = True

        excel.DisplayAlerts = False  # COM 오류 시 다이얼로그 차단

        # 2. 이미 열려 있는 워크북 확인
        for w in excel.Workbooks:
            if w.FullName.lower() == target_path:
                wb = w
                break

        # 3. 제한된 보기(Protected View) 확인
        if not wb:
            for pv in excel.ProtectedViewWindows:
                if pv.SourceFullName.lower() == target_path:
                    wb = pv.Edit()
                    break

        # 4. 새로 열기
        if not wb:
            wb = excel.Workbooks.Open(target_path, Password=pw, ReadOnly=True)
            is_new_wb = True

        # 5. 시트 존재 확인 (명확한 오류 메시지)
        try:
            ws = wb.Worksheets(sheet)
        except Exception:
            available = [ws_i.Name for ws_i in wb.Worksheets]
            raise KeyError(
                f"시트 '{sheet}'를 찾을 수 없습니다.\n"
                f"파일: {path.name}\n"
                f"가능한 시트: {available}"
            )

        # 6. 데이터 읽기 (finally에서 자원 정리됨)
        return read_df(ws)

    finally:
        if wb and is_new_wb:
            try:
                wb.Close(SaveChanges=False)
            except Exception:
                pass
        if excel and is_new_app:
            try:
                excel.Quit()
            except Exception:
                pass


def read_df(ws) -> pd.DataFrame:
    used = ws.UsedRange
    last_r = max(used.Row + used.Rows.Count - 1, HEADER_ROW)
    last_c = used.Column + used.Columns.Count - 1

    headers = [
        txt(x) for x in (read_range(ws, HEADER_ROW, 1, HEADER_ROW, last_c)[0] or [])
    ]
    body = read_range(ws, DATA_ROW, 1, last_r, last_c)

    while headers and headers[-1] == "":
        headers.pop()
        for r in body:
            if r:
                r.pop()

    return pd.DataFrame(body, columns=headers)


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


def make_result(df: pd.DataFrame, ch: Choice) -> pd.DataFrame:
    f = txt(ch.flag_col)
    if f not in df.columns:
        raise KeyError(f"'{ch.flag_col}' 헤더를 찾지 못했습니다. (정규화: '{f}')")

    x = df
    if ch.meal and COL_MEAL in x.columns:
        x = x[x[COL_MEAL].map(txt) == txt(ch.meal)]
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
