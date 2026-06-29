def total_width_pt(ws, used_cols: int) -> float:
    """
    현재 시트의 1..used_cols 총 폭(포인트)을 반환합니다.

    :param ws: Excel COM Worksheet
    :param used_cols: 사용 열 수
    """
    return sum(ws.Columns(c).Width for c in range(1, used_cols + 1))


def page_chunks_from_breaks(
    ws, groups: list[tuple[int, int]], offset: int = 3
) -> list[list[tuple[int, int]]]:
    """
    ws.HPageBreaks(수동만) 기준으로 '페이지별 그룹 리스트'로 분할합니다.
    Excel 자동 추가 페이지 나누기(Extent==2)는 제외합니다.

    :param ws: Excel COM Worksheet
    :param groups: (start_idx, end_idx) 목록 (DataFrame 인덱스 기준)
    :param offset: 데이터 시작 행 오프셋(기본 3)
    """
    if not groups:
        return []

    # COM 컬렉션은 인덱스로 접근
    breaks = []
    for i in range(1, ws.HPageBreaks.Count + 1):
        pb = ws.HPageBreaks(i)
        ext = getattr(pb, "Extent", -1)
        row = pb.Location.Row
        if i == 1:
            import logging as _lg

            _lg.getLogger(__name__).info(
                "HPageBreak #%d: row=%d Extent=%s", i, row, ext
            )
        breaks.append(row)
    breaks.sort()

    chunks: list[list[tuple[int, int]]] = []
    cur: list[tuple[int, int]] = []
    bi = 0

    for g in groups:
        s0, _ = g
        start_row = s0 + offset

        while bi < len(breaks) and start_row >= breaks[bi]:
            if cur:
                chunks.append(cur)
            cur = []
            bi += 1

        cur.append(g)

    if cur:
        chunks.append(cur)

    return chunks


def measure_page_height_pt(
    ws,
    page_groups: list[tuple[int, int]],
    used_rows: int,
    is_last_page: bool,
    offset: int = 3,
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
    :param offset: 데이터 시작 행 오프셋
    """
    if not page_groups:
        return 0.0

    h_hdr = ws.Rows(1).RowHeight + ws.Rows(2).RowHeight

    start = page_groups[0][0] + offset
    end = page_groups[-1][1] + offset
    body_h = sum(ws.Rows(r).RowHeight for r in range(start, end + 1))

    h = h_hdr + body_h
    if is_last_page:
        h += ws.Rows(used_rows).RowHeight

    return h
