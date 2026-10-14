"""청구·확인서 공유 상태와 표시 주소 테스트."""

import pytest

from src.core.models import combine_address


@pytest.mark.parametrize(
    ("road", "detail", "expected"),
    [
        (" 성남대로  1 ", " 101호 ", "성남대로 1 101호"),
        (None, "101호", "101호"),
        ("성남대로 1", None, "성남대로 1"),
        (None, None, ""),
    ],
)
def test_combines_address_without_blank_or_duplicate_spaces(road, detail, expected):
    assert combine_address(road, detail) == expected
