from src.config import PAGE_H, PAGE_W


def apply_base_page_setup(
    ws, used_rows: int, used_cols: int, orientation_pref: int
) -> int:
    """
    워크시트 기본 페이지 설정을 적용하고 실제 방향값을 반환합니다.

    :param ws: Excel COM Worksheet
    :param used_rows: 사용 행 수
    :param used_cols: 사용 열 수
    :param orientation_pref: 0(자동) / 1(세로) / 2(가로)
    :return: 실제 적용된 방향(1/2)
    """
    ps, excel = ws.PageSetup, ws.Application

    ps.PrintArea = ws.Range(ws.Cells(1, 1), ws.Cells(used_rows, used_cols)).Address
    ps.PaperSize = 9  # A4
    ps.Zoom = False
    ps.FitToPagesWide = 1
    ps.FitToPagesTall = False
    ps.PrintTitleRows = "$1:$2"

    m = excel.CentimetersToPoints(0.3)
    ps.LeftMargin = m
    ps.RightMargin = m
    ps.TopMargin = m
    ps.BottomMargin = m
    ps.HeaderMargin = 0
    ps.FooterMargin = 0
    ps.CenterHorizontally = True

    actual = orientation_pref if orientation_pref != 0 else (2 if used_cols >= 8 else 1)
    ps.Orientation = actual
    return actual


def page_limits_pt(ws, actual_orientation: int) -> tuple[float, float]:
    """
    페이지 사용 가능 영역(포인트)을 반환합니다.

    :param ws: Excel COM Worksheet
    :param actual_orientation: 1(세로) / 2(가로)
    :return: (limit_h, limit_w)
    """
    ps = ws.PageSetup
    is_landscape = actual_orientation == 2

    h_buffer = 30 if is_landscape else 20
    w_buffer = 20

    limit_h = (PAGE_W if is_landscape else PAGE_H) - (
        ps.TopMargin + ps.BottomMargin + h_buffer
    )
    limit_w = (PAGE_H if is_landscape else PAGE_W) - (
        ps.LeftMargin + ps.RightMargin + w_buffer
    )
    return limit_h, limit_w
