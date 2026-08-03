"""배송대타 Excel 출력 — openpyxl Workbook을 생성하여 임시 파일로 저장 후 실행."""

import logging
import os
import tempfile

import pandas as pd

from src.excel.delivery_sheet_renderer import (
    DeliveryPrintPolicy,
    DeliveryRenderRequest,
    DeliverySheetRenderer,
)

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
    request = DeliveryRenderRequest(
        title=title,
        meal=meal,
        staff=ship,
        date=date_str,
        print_policy=DeliveryPrintPolicy(
            orientation=ori,
            split_mode=split,
            max_rows=max_rows,
        ),
    )
    wb = DeliverySheetRenderer().render(df, request)

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
