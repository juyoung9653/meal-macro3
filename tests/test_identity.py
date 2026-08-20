"""이름·생년월일 동일인 판정용 정규화 테스트."""

from datetime import date, datetime, timezone

import pytest

from src.core.identity import normalize_birthdate, normalize_identity_name


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2010-01-02", "20100102"),
        ("2010.1.2", "20100102"),
        ("10/01/02", "20100102"),
        ("100102", "20100102"),
        (100102, "20100102"),
        ("100102-3******", "20100102"),
        ("100102-3123456", "20100102"),
        (date(2010, 1, 2), "20100102"),
        (datetime(2010, 1, 2, 12, 30, tzinfo=timezone.utc), "20100102"),
    ],
)
def test_normalize_birthdate_common_formats(raw, expected):
    assert normalize_birthdate(raw) == expected


def test_normalize_identity_name_ignores_whitespace_and_case():
    assert normalize_identity_name(" Hong Gil Dong ") == "honggildong"
