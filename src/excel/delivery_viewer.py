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
) -> str:
    """배송대타 Excel을 임시 파일로 저장해 열고 생성 경로를 반환한다."""
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

    descriptor, tmp_path = tempfile.mkstemp(suffix=".xlsx")
    os.close(descriptor)

    try:
        wb.save(tmp_path)
        os.startfile(tmp_path)
    except Exception:
        log.exception("Failed to save/open delivery excel file")
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    finally:
        wb.close()

    return tmp_path
