from src.excel.groups import SheetGroup


def total_width_pt(ws, used_cols: int) -> float:
    """
    현재 시트의 1..used_cols 총 폭(포인트)을 반환합니다.

    :param ws: Excel COM Worksheet
    :param used_cols: 사용 열 수
    """
    return sum(ws.Columns(c).Width for c in range(1, used_cols + 1))


def page_chunks_from_breaks(
    ws, groups: tuple[SheetGroup, ...]
) -> list[list[SheetGroup]]:
    """
    ws.HPageBreaks 기준으로 '페이지별 그룹 리스트'로 분할합니다.

    :param ws: Excel COM Worksheet
    :param groups: 실제 Excel 행 위치가 계산된 배송 그룹 목록
    """
    if not groups:
        return []

    breaks = sorted(pb.Location.Row for pb in ws.HPageBreaks)

    chunks: list[list[SheetGroup]] = []
    cur: list[SheetGroup] = []
    bi = 0

    for group in groups:
        start_row = group.start_row

        while bi < len(breaks) and start_row >= breaks[bi]:
            if cur:
                chunks.append(cur)
            cur = []
            bi += 1

        cur.append(group)

    if cur:
        chunks.append(cur)

    return chunks


def measure_page_height_pt(
    ws,
    page_groups: list[SheetGroup],
    used_rows: int,
    is_last_page: bool,
) -> float:
    """
    페이지 높이(포인트)를 측정합니다.
    - 헤더: 1~2행
    - 바디: page_groups가 커버하는 데이터 영역 높이 합
    - 마지막 페이지면 footer 역할의 마지막 행(used_rows) 높이 포함

    :param ws: Excel COM Worksheet
    :param page_groups: 해당 페이지의 그룹 목록
    :param used_rows: 전체 사용 행 수
    :param is_last_page: 마지막 페이지 여부
    """
    if not page_groups:
        return 0.0

    h_hdr = ws.Rows(1).RowHeight + ws.Rows(2).RowHeight

    start = page_groups[0].start_row
    end = page_groups[-1].end_row
    body_h = sum(ws.Rows(r).RowHeight for r in range(start, end + 1))

    h = h_hdr + body_h
    if is_last_page:
        h += sum(ws.Rows(r).RowHeight for r in range(end + 1, used_rows + 1))

    return h
