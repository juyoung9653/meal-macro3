"""배송 그룹 분석 및 인쇄 페이지 계획.

페이지 소속과 후속 페이지의 시작 행은 :class:`PagePlan`이 단일 기준으로 관리한다.
레거시 tuple/list API는 기존 호출자를 위한 어댑터다.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import IntEnum

import pandas as pd

from src.core.settings import get_settings
from src.excel._parsing import _to_str as txt

log = logging.getLogger(__name__)

_S = get_settings()
COL_ORD = _S.get_delivery_config("col_ord")


class SplitMode(IntEnum):
    """배송 그룹을 인쇄 페이지에 배치하는 방식."""

    NONE = 0
    BY_JURISDICTION = 1
    EVENLY = 2


@dataclass(frozen=True)
class DeliveryGroup:
    """DataFrame의 연속된 배송 그룹 범위 (0-based, inclusive)."""

    start_index: int
    end_index: int

    def __post_init__(self) -> None:
        if self.start_index < 0:
            raise ValueError("start_index must not be negative")
        if self.end_index < self.start_index:
            raise ValueError("end_index must be greater than or equal to start_index")

    @property
    def start(self) -> int:
        """레거시 범위 표기와 대응하는 시작 인덱스."""
        return self.start_index

    @property
    def end(self) -> int:
        """레거시 범위 표기와 대응하는 끝 인덱스."""
        return self.end_index

    @property
    def row_count(self) -> int:
        return self.end_index - self.start_index + 1

    def sheet_start_row(self, offset: int = 3) -> int:
        """그룹 첫 데이터 행의 1-based worksheet row를 반환한다."""
        return self.start_index + offset


@dataclass(frozen=True)
class PagePlan:
    """인쇄 페이지의 그룹 청크 및 후속 페이지 시작 행 계획.

    ``next_page_start_rows``는 데이터 행 기준의 1-based worksheet row다. OpenXML
    break coordinate 변환은 이 순수 계획 객체가 아닌 layout 계층에서만 수행한다.
    """

    split_mode: SplitMode
    page_chunks: tuple[tuple[DeliveryGroup, ...], ...]
    next_page_start_rows: tuple[int, ...]

    @classmethod
    def from_groups(
        cls,
        groups: Sequence[DeliveryGroup],
        mode: object,
        max_groups_limit: int,
        df: pd.DataFrame | None = None,
        offset: int = 3,
    ) -> PagePlan:
        """그룹, 분할 방식, 제한으로 페이지 계획을 생성한다."""
        split_mode = _coerce_split_mode(mode)
        _validate_max_groups_limit(max_groups_limit)
        typed_groups = tuple(groups)
        if not typed_groups:
            return cls(split_mode, (), ())

        chunks = _compute_page_chunks(typed_groups, split_mode, max_groups_limit, df)
        next_page_start_rows = tuple(
            chunk[0].sheet_start_row(offset) for chunk in chunks[1:] if chunk
        )
        return cls(split_mode, chunks, next_page_start_rows)


def _coerce_split_mode(mode: object) -> SplitMode:
    try:
        return SplitMode(mode)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"unknown split mode: {mode!r}") from exc


def _validate_max_groups_limit(max_groups_limit: int) -> None:
    if max_groups_limit <= 0:
        raise ValueError("max_groups_limit must be greater than 0")


def get_delivery_groups(df: pd.DataFrame) -> list[DeliveryGroup]:
    """DataFrame에서 연속된 배송 그룹을 typed 범위로 추출한다."""
    if df.empty or COL_ORD not in df.columns:
        return [DeliveryGroup(i, i) for i in range(len(df))]
    return _groups_from_values(df[COL_ORD].map(txt).tolist())


def get_groups(df: pd.DataFrame) -> list[tuple[int, int]]:
    """레거시 API: 배송 그룹 범위를 tuple 목록으로 반환한다."""
    return [(group.start_index, group.end_index) for group in get_delivery_groups(df)]


def build_page_plan(
    df: pd.DataFrame,
    mode: object,
    max_groups_limit: int,
    offset: int = 3,
) -> PagePlan:
    """DataFrame 전체를 기준으로 권위 있는 typed 페이지 계획을 생성한다."""
    return PagePlan.from_groups(
        get_delivery_groups(df), mode, max_groups_limit, df=df, offset=offset
    )


def apply_page_breaks_by_groups(
    df: pd.DataFrame,
    groups: list[tuple[int, int]],
    mode: object,
    max_groups_limit: int,
    offset: int = 3,
) -> list[int]:
    """레거시 API: ``PagePlan``의 후속 페이지 시작 행을 list로 반환한다."""
    plan = PagePlan.from_groups(
        _as_delivery_groups(groups), mode, max_groups_limit, df=df, offset=offset
    )
    log.info(
        "page_breaks: mode=%s max_limit=%d total_groups=%d total_breaks=%d",
        plan.split_mode.name,
        max_groups_limit,
        len(groups),
        len(plan.next_page_start_rows),
    )
    return list(plan.next_page_start_rows)


def _groups_from_values(vals: Iterable[str]) -> list[DeliveryGroup]:
    values = list(vals)
    groups: list[DeliveryGroup] = []
    i, n = 0, len(values)
    while i < n:
        j = i
        while j + 1 < n and values[j + 1] == values[i]:
            j += 1
        groups.append(DeliveryGroup(i, j))
        i = j + 1
    return groups


def _as_delivery_groups(
    groups: Sequence[DeliveryGroup | tuple[int, int]],
) -> list[DeliveryGroup]:
    return [
        group if isinstance(group, DeliveryGroup) else DeliveryGroup(*group)
        for group in groups
    ]


def compute_page_chunks(
    groups: list[tuple[int, int]],
    mode: object,
    max_groups_limit: int,
    df: pd.DataFrame | None = None,
) -> list[list[tuple[int, int]]]:
    """레거시 API: ``PagePlan`` 청크를 tuple 목록으로 반환한다."""
    plan = PagePlan.from_groups(
        _as_delivery_groups(groups), mode, max_groups_limit, df=df
    )
    return [
        [(group.start_index, group.end_index) for group in chunk]
        for chunk in plan.page_chunks
    ]


def _compute_page_chunks(
    groups: tuple[DeliveryGroup, ...],
    mode: SplitMode,
    max_groups_limit: int,
    df: pd.DataFrame | None,
) -> tuple[tuple[DeliveryGroup, ...], ...]:
    if mode is SplitMode.NONE:
        return (groups,)

    total = len(groups)
    if mode is SplitMode.BY_JURISDICTION and df is not None:
        chunks: list[tuple[DeliveryGroup, ...]] = []
        current: list[DeliveryGroup] = []
        current_count = 0
        current_dong = ""
        for group in groups:
            dong = str(df.iloc[group.start_index].get("관할동", ""))
            dong_changed = bool(current_dong) and dong != current_dong
            limit_exceeded = current_count >= max_groups_limit
            if (dong_changed or limit_exceeded) and current:
                chunks.append(tuple(current))
                current = []
                current_count = 0
            current.append(group)
            current_count += 1
            current_dong = dong
        if current:
            chunks.append(tuple(current))
        return tuple(chunks)

    # EVENLY, and the legacy BY_JURISDICTION-without-dataframe behavior.
    num_pages = max(1, (total + max_groups_limit - 1) // max_groups_limit)
    base, remainder = divmod(total, num_pages)
    chunks = []
    index = 0
    for page_index in range(num_pages):
        size = base + (1 if page_index < remainder else 0)
        chunks.append(groups[index : index + size])
        index += size
    return tuple(chunks)
