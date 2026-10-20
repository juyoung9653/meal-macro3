import re
from collections import Counter
from dataclasses import dataclass

import pandas as pd

from src.config import COL_DONG, COL_PHONE
from src.excel_io import ord_groups, txt


@dataclass(frozen=True)
class SheetGroup:
    """한 가구로 묶이는 원본 행 범위와 실제 Excel 행 범위입니다."""

    source_start: int
    source_end: int
    start_row: int
    end_row: int
    dong: str

    @property
    def meal_count(self) -> int:
        return self.source_end - self.source_start + 1


@dataclass(frozen=True)
class DongMealSummary:
    """한 동의 1식·2식·3식 가구 수와 전체 식수입니다."""

    dong: str
    household_counts: tuple[int, ...]
    total_meals: int
    row: int

    @property
    def label(self) -> str:
        return self.dong or "관할동 미지정"


@dataclass(frozen=True)
class SheetLayout:
    """배송 목록 뒤에 동별 식수 교차표가 이어지는 출력 시트 배치입니다."""

    groups: tuple[SheetGroup, ...]
    data_end_row: int
    summary_header_row: int | None
    meal_sizes: tuple[int, ...]
    dong_meal_summaries: tuple[DongMealSummary, ...]
    overall_summary: DongMealSummary | None
    used_rows: int
    used_cols: int


def get_groups(df: pd.DataFrame) -> list[tuple[int, int]]:
    """핸드폰 번호가 같은 연속 행을 한 가구로 묶습니다."""
    if df.empty or COL_PHONE not in df.columns:
        return [(i, i) for i in range(len(df))]

    keys: list[str] = []
    for row_index, value in enumerate(df[COL_PHONE]):
        if pd.isna(value):
            phone = ""
        else:
            raw_phone = str(value).strip()
            raw_phone = re.sub(r"\.0+$", "", raw_phone)
            phone = re.sub(r"\D", "", raw_phone)
        # 번호가 없는 행끼리는 같은 가구로 합치지 않습니다.
        keys.append(f"phone:{phone}" if phone else f"row:{row_index}")
    return ord_groups(keys)


def build_sheet_layout(
    df: pd.DataFrame,
    data_start_row: int = 3,
    display_col_count: int | None = None,
) -> SheetLayout:
    """데이터 사이에는 행을 넣지 않고 맨 아래 동별 식수표를 계산합니다."""
    groups = tuple(
        SheetGroup(
            source_start=source_start,
            source_end=source_end,
            start_row=source_start + data_start_row,
            end_row=source_end + data_start_row,
            dong=(
                txt(df.iloc[source_start][COL_DONG]) if COL_DONG in df.columns else ""
            ),
        )
        for source_start, source_end in get_groups(df)
    )

    data_end_row = data_start_row + len(df) - 1
    data_col_count = (
        display_col_count if display_col_count is not None else len(df.columns) + 1
    )

    can_summarize = bool(groups) and COL_DONG in df.columns
    if not can_summarize:
        return SheetLayout(
            groups=groups,
            data_end_row=data_end_row,
            summary_header_row=None,
            meal_sizes=(),
            dong_meal_summaries=(),
            overall_summary=None,
            used_rows=data_end_row,
            used_cols=data_col_count,
        )

    by_dong: dict[str, Counter[int]] = {}
    overall_counts: Counter[int] = Counter()
    for group in groups:
        by_dong.setdefault(group.dong, Counter())[group.meal_count] += 1
        overall_counts[group.meal_count] += 1

    meal_sizes = tuple(range(1, max(5, max(overall_counts)) + 1))
    summary_header_row = data_end_row + 1
    first_summary_row = summary_header_row + 1
    dong_meal_summaries = tuple(
        DongMealSummary(
            dong=dong,
            household_counts=tuple(counts[size] for size in meal_sizes),
            total_meals=sum(size * counts[size] for size in meal_sizes),
            row=first_summary_row + i,
        )
        for i, (dong, counts) in enumerate(by_dong.items())
    )
    overall_summary = DongMealSummary(
        dong="전체",
        household_counts=tuple(overall_counts[size] for size in meal_sizes),
        total_meals=sum(size * overall_counts[size] for size in meal_sizes),
        row=first_summary_row + len(dong_meal_summaries),
    )

    # 관할동 + 1식..N식 + 식수 합계
    summary_col_count = len(meal_sizes) + 2
    return SheetLayout(
        groups=groups,
        data_end_row=data_end_row,
        summary_header_row=summary_header_row,
        meal_sizes=meal_sizes,
        dong_meal_summaries=dong_meal_summaries,
        overall_summary=overall_summary,
        used_rows=overall_summary.row,
        used_cols=max(data_col_count, summary_col_count),
    )


def apply_page_breaks_by_groups(
    ws,
    groups: tuple[SheetGroup, ...],
    mode: int,
    max_groups_limit: int,
) -> None:
    """페이지 구분선을 적용하며, 한 가구가 페이지 경계에서 잘리지 않게 합니다."""
    ws.ResetAllPageBreaks()
    if mode == 0 or not groups:
        return

    curr_g = 0
    curr_dong = ""

    for group in groups:
        need_break = False

        if mode == 1:
            dong_changed = curr_dong and group.dong != curr_dong
            limit_exceeded = max_groups_limit > 0 and curr_g >= max_groups_limit
            if dong_changed or limit_exceeded:
                need_break = True
            curr_dong = group.dong

        elif mode == 2 and max_groups_limit > 0 and curr_g >= max_groups_limit:
            need_break = True

        if need_break:
            ws.HPageBreaks.Add(Before=ws.Rows(group.start_row))
            curr_g = 0

        curr_g += 1

    _prevent_group_splits(ws, groups)


def _prevent_group_splits(ws, groups: tuple[SheetGroup, ...]) -> None:
    """그룹 중간에 페이지가 잘리면 해당 그룹 시작점으로 페이지를 넘깁니다."""
    idx = 0
    while idx < len(ws.HPageBreaks):
        pb_row = ws.HPageBreaks(idx + 1).Location.Row

        for group in groups:
            if group.start_row < pb_row <= group.end_row:
                ws.HPageBreaks.Add(Before=ws.Rows(group.start_row))
                break

        idx += 1
