import pandas as pd
import win32com.client as win32

from src.excel.groups import build_sheet_layout

# from src.excel.page_setup import apply_page_formatting
from src.excel.page_formatting.layout import apply_page_formatting
from src.excel.sheet_writer import write_sheet
from src.excel.text_portrait import preprocess_portrait


def show_excel(
    df: pd.DataFrame,
    title: str,
    meal: str,
    ship: str,
    date_str: str,
    ori: int,
    split: int,
    max_rows: int,
    summary_df: pd.DataFrame | None = None,
) -> None:
    excel = win32.DispatchEx("Excel.Application")
    excel.Visible = True
    excel.DisplayAlerts = False

    wb = excel.Workbooks.Add()
    ws = wb.Worksheets(1)
    ws.Name = title[:31]

    df = preprocess_portrait(df)
    summary_source = summary_df if summary_df is not None else df
    if len(summary_source) != len(df):
        raise ValueError("출력 데이터와 요약 데이터의 행 수가 일치하지 않습니다.")

    sheet_layout = build_sheet_layout(
        summary_source,
        display_col_count=len(df.columns) + 1,
    )
    write_sheet(
        ws,
        df,
        meal,
        ship,
        date_str,
        layout=sheet_layout,
        source_df=summary_source,
    )
    apply_page_formatting(
        ws,
        df,
        mode=split,
        max_groups_limit=max_rows,
        orientation_pref=ori,
        sheet_layout=sheet_layout,
    )
