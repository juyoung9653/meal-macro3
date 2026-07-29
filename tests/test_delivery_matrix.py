"""연간 배송 매트릭스 도메인 테스트."""

from datetime import date
from typing import cast

import pytest

from src.core.delivery_matrix import (
    AnnualDeliveryBook,
    DayState,
    DeliveryCellValue,
    DeliveryRow,
    EffectiveDeliveryState,
    MonthState,
    RosterEntry,
)


@pytest.fixture
def annual_book() -> AnnualDeliveryBook:
    dates = (
        date(2026, 9, 1),
        date(2026, 7, 29),
        date(2026, 6, 16),
        date(2026, 7, 28),
        date(2026, 6, 15),
    )
    blank = DeliveryCellValue.BLANK
    roster = (
        RosterEntry("P001", {"성명": "홍길동", "전화": "010-1111-1111"}),
        RosterEntry("P002", {"성명": "김영희", "메모": "원본 보존"}),
    )
    rows = (
        DeliveryRow(
            person_id="P001",
            meal="중식",
            staff="김기사",
            sequence="010",
            values={
                date(2026, 6, 15): DeliveryCellValue.DELIVERED,
                date(2026, 6, 16): blank,
                date(2026, 7, 28): DeliveryCellValue.DELIVERED,
                date(2026, 7, 29): blank,
                date(2026, 9, 1): blank,
            },
        ),
        DeliveryRow(
            person_id="P002",
            meal="중식",
            staff="김기사",
            sequence=2,
            values={
                date(2026, 6, 15): DeliveryCellValue.DELIVERED,
                date(2026, 6, 16): blank,
                date(2026, 7, 28): blank,
                date(2026, 7, 29): blank,
                date(2026, 9, 1): blank,
            },
        ),
        DeliveryRow(
            person_id="P001",
            meal="석식",
            staff="이기사",
            sequence="A-1",
            values={
                date(2026, 6, 15): blank,
                date(2026, 6, 16): blank,
                date(2026, 7, 28): DeliveryCellValue.PAUSED,
                date(2026, 7, 29): blank,
                date(2026, 9, 1): blank,
            },
        ),
    )
    return AnnualDeliveryBook(
        year=2026,
        roster=roster,
        delivery_rows=rows,
        dates=dates,
    )


def test_builds_indexes_and_preserves_roster_extras(annual_book: AnnualDeliveryBook):
    assert annual_book.roster_by_id["P001"].extra == {
        "성명": "홍길동",
        "전화": "010-1111-1111",
    }
    row = annual_book.delivery_rows_by_key[("P001", "중식")]
    assert annual_book.get_delivery_row("P001", "중식") is row
    assert annual_book.get_delivery_row("없는ID", "중식") is None


def test_roster_extra_is_immutable_with_explicit_mutable_copy(
    annual_book: AnnualDeliveryBook,
):
    entry = annual_book.roster_by_id["P001"]

    with pytest.raises(TypeError):
        cast(dict[str, object], entry.extra)["성명"] = "변조"

    mutable_copy = entry.as_dict()
    mutable_copy["성명"] = "복사본만 변경"
    assert entry.extra["성명"] == "홍길동"
    assert entry.as_dict()["성명"] == "홍길동"


def test_book_rejects_missing_or_additional_delivery_value_dates():
    first = date(2026, 6, 15)
    second = date(2026, 6, 16)
    additional = date(2026, 6, 17)
    roster = (RosterEntry("P001"),)

    sparse_row = DeliveryRow(
        "P001", "중식", "김기사", 1, {first: DeliveryCellValue.DELIVERED}
    )
    with pytest.raises(ValueError, match="누락.*20260616"):
        AnnualDeliveryBook(2026, roster, (sparse_row,), (first, second))

    extra_row = DeliveryRow(
        "P001",
        "중식",
        "김기사",
        1,
        {
            first: DeliveryCellValue.DELIVERED,
            second: DeliveryCellValue.BLANK,
            additional: DeliveryCellValue.BLANK,
        },
    )
    with pytest.raises(ValueError, match="추가.*20260617"):
        AnnualDeliveryBook(2026, roster, (extra_row,), (first, second))


def test_dates_are_sorted_by_actual_date_not_input_order(annual_book: AnnualDeliveryBook):
    assert annual_book.dates == (
        date(2026, 6, 15),
        date(2026, 6, 16),
        date(2026, 7, 28),
        date(2026, 7, 29),
        date(2026, 9, 1),
    )
    assert annual_book.available_months == (6, 7, 9)


def test_month_and_day_states(annual_book: AnnualDeliveryBook):
    assert annual_book.month_state(2025, 6) is MonthState.MISSING
    assert annual_book.month_state(2026, 8) is MonthState.MISSING
    assert annual_book.month_state(2026, 9) is MonthState.UNENTERED
    assert annual_book.month_state(2026, 6) is MonthState.ENTERED

    assert annual_book.day_state(date(2026, 6, 14)) is DayState.MISSING
    assert annual_book.day_state(date(2026, 6, 16)) is DayState.NO_DELIVERY
    assert annual_book.day_state(date(2026, 6, 15)) is DayState.ENTERED


def test_raw_tristate_and_effective_state_are_separate(annual_book: AnnualDeliveryBook):
    dinner = annual_book.get_delivery_row("P001", "석식")
    assert dinner is not None

    # 입력된 날짜의 개별 blank는 중지이지만 raw blank 자체는 보존한다.
    assert dinner.raw_value_on(date(2026, 6, 15)) is DeliveryCellValue.BLANK
    assert (
        annual_book.effective_delivery_state(
            "P001", "석식", date(2026, 6, 15)
        )
        is EffectiveDeliveryState.PAUSED
    )

    # 명시적 0도 raw에서 구별된다.
    assert dinner.raw_value_on(date(2026, 7, 28)) is DeliveryCellValue.PAUSED

    # 월 전체 미입력/전면 비배송일의 blank는 중지로 해석하지 않는다.
    assert (
        annual_book.effective_delivery_state(
            "P001", "석식", date(2026, 9, 1)
        )
        is EffectiveDeliveryState.MONTH_UNENTERED
    )
    assert (
        annual_book.effective_delivery_state(
            "P001", "석식", date(2026, 6, 16)
        )
        is EffectiveDeliveryState.NO_DELIVERY
    )
    assert (
        annual_book.effective_delivery_state(
            "P001", "석식", date(2026, 6, 14)
        )
        is EffectiveDeliveryState.DATE_MISSING
    )


def test_delivery_queries_count_and_numeric_sequence_sort(
    annual_book: AnnualDeliveryBook,
):
    delivered = annual_book.delivered_rows_on(
        date(2026, 6, 15), meal="중식", staff="김기사"
    )
    assert [row.person_id for row in delivered] == ["P002", "P001"]
    assert delivered[0].sequence == 2
    assert delivered[1].sequence == "010"
    assert delivered[1].sequence_display == "010"

    assert annual_book.monthly_delivery_count("P001", "중식", 2026, 6) == 1
    assert annual_book.monthly_delivery_count("P001", "중식", 2026, 7) == 1
    assert annual_book.monthly_delivery_count("P001", "중식", 2026, 9) == 0


def test_previous_actual_delivery_date_is_meal_specific(
    annual_book: AnnualDeliveryBook,
):
    assert annual_book.previous_delivery_date(date(2026, 9, 1), "중식") == date(
        2026, 7, 28
    )
    assert annual_book.previous_delivery_date(date(2026, 7, 28), "중식") == date(
        2026, 6, 15
    )
    assert annual_book.previous_delivery_date(date(2026, 7, 28), "석식") is None
