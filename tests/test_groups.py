"""배송 그룹 페이지 분할의 입력값 검증 테스트."""

from __future__ import annotations

import pandas as pd
import pytest

from src.excel.groups import apply_page_breaks_by_groups, compute_page_chunks

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
