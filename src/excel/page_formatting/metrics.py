"""워크시트 레이아웃 측정 — openpyxl Worksheet 기준."""


def total_width_pt(col_widths: list[float]) -> float:
    """컬럼 폭 리스트의 총합(포인트)."""
    return sum(col_widths)


def page_chunks_from_breaks(
    groups: list[tuple[int, int]],
    page_breaks: list[int],
    offset: int = 3,
) -> list[list[tuple[int, int]]]:
    """페이지 나누기 위치를 기준으로 그룹 리스트를 페이지 청크로 분할.

    :param groups: (start_idx, end_idx) 목록 (DataFrame 인덱스 기준)
    :param page_breaks: 페이지 나누기 행 인덱스 리스트 (from apply_page_breaks_by_groups)
    :param offset: 데이터 시작 행 오프셋
    :return: 페이지별 그룹 리스트
    """
    if not groups:
        return []

    breaks = sorted(page_breaks)
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
