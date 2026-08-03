"""배송 그룹 페이지 분할의 입력값 검증 테스트."""

from __future__ import annotations

import pandas as pd
import pytest
from openpyxl import Workbook

from src.excel.groups import (
    DeliveryGroup,
    PagePlan,
    SplitMode,
    apply_page_breaks_by_groups,
    build_page_plan,
    compute_page_chunks,
)
from src.excel.page_formatting import layout

_GROUPS = [(0, 0), (1, 1), (2, 2)]


@pytest.mark.parametrize("mode", [1, 2])
@pytest.mark.parametrize("max_groups_limit", [0, -1])
def test_compute_page_chunks_rejects_non_positive_group_limit(
    mode: int, max_groups_limit: int
):
    with pytest.raises(ValueError, match="max_groups_limit"):
        compute_page_chunks(
            _GROUPS,
            mode=mode,
            max_groups_limit=max_groups_limit,
        )


@pytest.mark.parametrize("max_groups_limit", [0, -1])
def test_apply_page_breaks_rejects_non_positive_group_limit(
    max_groups_limit: int,
):
    frame = pd.DataFrame(
        {
            "배송순서": ["001", "002", "003"],
            "관할동": ["정자동", "정자동", "수내동"],
        }
    )

    with pytest.raises(ValueError, match="max_groups_limit"):
        apply_page_breaks_by_groups(
            frame,
            _GROUPS,
            mode=2,
            max_groups_limit=max_groups_limit,
        )


def test_compute_page_chunks_still_balances_with_positive_limit():
    chunks = compute_page_chunks(_GROUPS, mode=2, max_groups_limit=2)

    assert chunks == [[(0, 0), (1, 1)], [(2, 2)]]


def test_page_plan_owns_typed_chunks_and_one_based_next_page_rows():
    frame = pd.DataFrame({"관할동": ["A", "A", "B"]})

    plan = build_page_plan(frame, SplitMode.EVENLY, max_groups_limit=2)

    assert plan.split_mode is SplitMode.EVENLY
    assert plan.page_chunks == (
        (DeliveryGroup(0, 0), DeliveryGroup(1, 1)),
        (DeliveryGroup(2, 2),),
    )
    assert plan.next_page_start_rows == (5,)


@pytest.mark.parametrize("mode", [-1, 3, "evenly"])
def test_unknown_split_mode_is_rejected(mode: object):
    with pytest.raises(ValueError, match="unknown split mode"):
        build_page_plan(pd.DataFrame({"관할동": ["A"]}), mode, 1)


def test_mode_one_breaks_are_derived_from_the_same_chunks():
    groups = [(0, 0), (1, 1), (2, 2), (3, 3), (4, 4)]
    frame = pd.DataFrame({"관할동": ["A", "A", "B", "B", "B"]})

    chunks = compute_page_chunks(groups, mode=1, max_groups_limit=2, df=frame)
    breaks = apply_page_breaks_by_groups(
        frame, groups, mode=1, max_groups_limit=2, offset=3
    )

    assert [len(chunk) for chunk in chunks] == [2, 2, 1]
    assert breaks == [5, 7]


def test_page_break_uses_zero_based_openxml_coordinate(monkeypatch):
    frame = pd.DataFrame({"관할동": ["A"]})
    workbook = Workbook()
    worksheet = workbook.active
    assert worksheet is not None

    monkeypatch.setattr(layout, "apply_base_page_setup", lambda *_args: 0)
    page_plan = PagePlan(
        SplitMode.EVENLY,
        ((DeliveryGroup(0, 0),), (DeliveryGroup(2, 2),)),
        (5,),
    )
    monkeypatch.setattr(layout, "build_page_plan", lambda *_args: page_plan)
    monkeypatch.setattr(layout, "_clean_address_newlines", lambda *_args: None)
    monkeypatch.setattr(layout, "calc_optimal_font_size", lambda *_args: 10.0)
    monkeypatch.setattr(layout, "_apply_fonts", lambda *_args: None)
    monkeypatch.setattr(layout, "_apply_column_widths", lambda *_args: None)
    monkeypatch.setattr(layout, "_apply_row_heights", lambda *_args: None)

    layout.apply_page_formatting(worksheet, frame, mode=0, max_groups_limit=1)

    assert worksheet.row_breaks.brk[0].id == 4
