import pandas as pd
import win32com.client as win32

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
) -> None:
    excel = win32.DispatchEx("Excel.Application")
    excel.Visible = True
    excel.DisplayAlerts = False

    wb = excel.Workbooks.Add()
    ws = wb.Worksheets(1)
    ws.Name = title[:31]

    df = preprocess_portrait(df)
    write_sheet(ws, df, meal, ship, date_str)
    apply_page_formatting(
        ws,
        df,
        mode=split,
        max_groups_limit=max_rows,
        orientation_pref=ori,
    )
