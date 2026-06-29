"""워크시트 기본 페이지 설정 — openpyxl page_setup."""

from openpyxl.utils import get_column_letter
from openpyxl.worksheet.page import PageMargins

from src.core.settings import get_settings

_S = get_settings()
PAGE_H = _S.get_delivery_config("page_h")
PAGE_W = _S.get_delivery_config("page_w")


def apply_base_page_setup(
    ws, used_rows: int, used_cols: int, orientation_pref: int
) -> int:
    """워크시트 기본 페이지 설정 적용.

    :param ws: openpyxl Worksheet
    :param used_rows: 사용 행 수
    :param used_cols: 사용 열 수
    :param orientation_pref: 0(자동) / 1(세로) / 2(가로)
    :return: 실제 적용된 방향(1=portrait / 2=landscape)
    """
    last_col_letter = get_column_letter(used_cols)

    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.page_setup.fitToPage = True
    ws.print_title_rows = "$1:$2"
    ws.print_area = f"A1:{last_col_letter}{used_rows}"

    # 방향
    actual = orientation_pref if orientation_pref != 0 else (2 if used_cols >= 8 else 1)
    ws.page_setup.orientation = "landscape" if actual == 2 else "portrait"

    # 여백 (0.3cm → inch)
    margin_in = round(0.3 / 2.54, 4)
    ws.page_margins = PageMargins(
        left=margin_in,
        right=margin_in,
        top=margin_in,
        bottom=margin_in,
        header=0,
        footer=0,
    )

    return actual


def page_limits_pt(actual_orientation: int) -> tuple[float, float]:
    """페이지 사용 가능 영역(포인트) 반환.

    :param actual_orientation: 1(세로) / 2(가로)
    :return: (limit_h, limit_w)
    """
    is_landscape = actual_orientation == 2
    h_buffer = 10
    w_buffer = 10
    margin_pt = round(0.3 / 2.54 * 72, 1)

    limit_h = (PAGE_W if is_landscape else PAGE_H) - (margin_pt * 2 + h_buffer)
    limit_w = (PAGE_H if is_landscape else PAGE_W) - (margin_pt * 2 + w_buffer)
    return limit_h, limit_w
