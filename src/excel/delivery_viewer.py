"""배송대타 Excel 출력 — openpyxl Workbook을 생성하여 임시 파일로 저장 후 실행."""

import logging
import os
import tempfile

import pandas as pd
from openpyxl import Workbook

from src.excel.page_formatting.layout import apply_page_formatting
from src.excel.sheet_writer import write_sheet
from src.excel.text_portrait import preprocess_portrait

log = logging.getLogger(__name__)


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
    """배송대타 데이터를 Excel 파일로 출력하여 엑셀로 엽니다."""
    wb = Workbook()
    ws = wb.active
    if ws is None:
        ws = wb.create_sheet()
    ws.title = title[:31]

    processed = preprocess_portrait(df)
    write_sheet(ws, processed, meal, ship, date_str)
    apply_page_formatting(
        ws,
        processed,
        mode=split,
        max_groups_limit=max_rows,
        orientation_pref=ori,
    )

    tmp = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
    tmp_path = tmp.name
    tmp.close()

    try:
        wb.save(tmp_path)
        os.startfile(tmp_path)
    except Exception:
        log.exception("Failed to save/open delivery excel file")
