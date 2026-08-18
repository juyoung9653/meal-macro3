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
        date(2026, 8, 3),
        date(2026, 7, 29),
        date(2026, 6, 16),
        date(2026, 7, 28),
        date(2026, 6, 15),
    )
    blank = DeliveryCellValue.BLANK
    blank_values = {day: blank for day in dates}
    p001 = RosterEntry(
        source_row=2,
        extra={"성명": "홍길동", "전화": "010-1111-1111"},
    )
    p002 = RosterEntry(
        source_row=5,
        extra={"성명": "김영희", "메모": "원본 보존"},
    )
    roster = (p001, p002)
    rows = (
        DeliveryRow(
            person=p001,
            meal="중식",
            staff="김기사",
            sequence="010",
            values={
                date(2026, 6, 15): DeliveryCellValue.DELIVERED,
                date(2026, 6, 16): blank,
                date(2026, 7, 28): DeliveryCellValue.DELIVERED,
                date(2026, 7, 29): blank,
                date(2026, 8, 3): blank,
                date(2026, 9, 1): blank,
            },
            source_row=3,
        ),
        DeliveryRow(
            person=p002,
            meal="중식",
            staff="김기사",
            sequence=2,
            values={
                date(2026, 6, 15): DeliveryCellValue.DELIVERED,
                date(2026, 6, 16): blank,
                date(2026, 7, 28): blank,
                date(2026, 7, 29): blank,
                date(2026, 8, 3): blank,
                date(2026, 9, 1): blank,
            },
            source_row=6,
        ),
        DeliveryRow(
            person=p001,
            meal="석식",
            staff="이기사",
            sequence="A-1",
            values={
                date(2026, 6, 15): blank,
                date(2026, 6, 16): blank,
                date(2026, 7, 28): DeliveryCellValue.PAUSED,
                date(2026, 7, 29): blank,
                date(2026, 8, 3): DeliveryCellValue.DELIVERED,
                date(2026, 9, 1): blank,
            },
            source_row=4,
        ),
        DeliveryRow(p001, "조식", "", "", blank_values, source_row=2),
        DeliveryRow(p002, "조식", "", "", blank_values, source_row=5),
        DeliveryRow(p002, "석식", "", "", blank_values, source_row=7),
    )
    return AnnualDeliveryBook(
        year=2026,
        roster=roster,
        delivery_rows=rows,
        dates=dates,
    )


def test_builds_person_indexes_and_preserves_roster_extras(
    annual_book: AnnualDeliveryBook,
):
    p001, _p002 = annual_book.roster
    assert p001.extra == {
        "성명": "홍길동",
        "전화": "010-1111-1111",
    }

    row = annual_book.get_delivery_row(p001, "중식")
    assert row is not None
    assert row.person is p001
    assert row.source_row == 3
    assert annual_book.rows_by_person[p001] == (
        row,
        annual_book.get_delivery_row(p001, "석식"),
        annual_book.get_delivery_row(p001, "조식"),
    )

    equivalent = RosterEntry(source_row=2, extra={"성명": "홍길동"})
    assert equivalent is not p001
    assert equivalent != p001
    assert annual_book.get_delivery_row(equivalent, "중식") is None
    assert equivalent not in annual_book.rows_by_person

    unknown = RosterEntry(source_row=8, extra={"성명": "없는 사람"})
    assert annual_book.get_delivery_row(unknown, "중식") is None


def test_roster_extra_is_immutable_with_explicit_mutable_copy(
    annual_book: AnnualDeliveryBook,
):
    entry = annual_book.roster[0]

    with pytest.raises(TypeError):
        cast(dict[str, object], entry.extra)["성명"] = "변조"

    mutable_copy = entry.as_dict()
    mutable_copy["성명"] = "복사본만 변경"
    assert entry.extra["성명"] == "홍길동"
    assert entry.as_dict()["성명"] == "홍길동"


def test_book_requires_delivery_row_to_reference_canonical_person_object():
    day = date(2026, 6, 15)
    canonical = RosterEntry(source_row=2, extra={"성명": "홍길동"})
    detached = RosterEntry(source_row=2, extra={"성명": "홍길동"})
    row = DeliveryRow(
        person=detached,
        meal="중식",
        staff="김기사",
        sequence=1,
        values={day: DeliveryCellValue.DELIVERED},
    )

    with pytest.raises(ValueError, match="동일 객체"):
        AnnualDeliveryBook(2026, (canonical,), (row,), (day,))


def test_book_requires_at_least_one_delivery_row_for_each_roster_person():
    day = date(2026, 6, 15)
    person = RosterEntry(source_row=2, extra={"성명": "홍길동"})

    with pytest.raises(ValueError, match="배송 행이 하나도 없습니다"):
        AnnualDeliveryBook(2026, (person,), (), (day,))


def test_book_allows_only_the_meal_rows_that_person_uses():
    day = date(2026, 6, 15)
    person = RosterEntry(source_row=2, extra={"성명": "홍길동"})
    lunch = DeliveryRow(
        person=person,
        meal="중식",
        staff="김기사",
        sequence=None,
        values={day: DeliveryCellValue.DELIVERED},
    )

    book = AnnualDeliveryBook(2026, (person,), (lunch,), (day,))

    assert book.rows_by_person[person] == (lunch,)
    assert book.get_delivery_row(person, "조식") is None
    assert book.get_delivery_row(person, "중식") is lunch
    assert book.get_delivery_row(person, "석식") is None


def test_empty_book_is_allowed():
    book = AnnualDeliveryBook(2026, (), (), (date(2026, 6, 15),))

    assert book.roster == ()
    assert book.delivery_rows == ()
    assert book.rows_by_person == {}


def test_book_rejects_missing_or_additional_delivery_value_dates():
    first = date(2026, 6, 15)
    second = date(2026, 6, 16)
    additional = date(2026, 6, 17)
    person = RosterEntry(source_row=2)
    roster = (person,)

    sparse_row = DeliveryRow(
        person,
        "중식",
        "김기사",
        1,
        {first: DeliveryCellValue.DELIVERED},
    )
    with pytest.raises(ValueError, match="누락.*20260616"):
        AnnualDeliveryBook(2026, roster, (sparse_row,), (first, second))

    extra_row = DeliveryRow(
        person,
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
        date(2026, 8, 3),
        date(2026, 9, 1),
    )
    assert annual_book.available_months == (6, 7, 8, 9)


def test_month_and_day_states(annual_book: AnnualDeliveryBook):
    assert annual_book.month_state(2025, 6) is MonthState.MISSING
    assert annual_book.month_state(2026, 10) is MonthState.MISSING
    assert annual_book.month_state(2026, 9) is MonthState.UNENTERED
    assert annual_book.month_state(2026, 6) is MonthState.ENTERED
    assert annual_book.month_state(2026, 8) is MonthState.ENTERED

    assert annual_book.day_state(date(2026, 6, 14)) is DayState.MISSING
    assert annual_book.day_state(date(2026, 6, 16)) is DayState.NO_DELIVERY
    assert annual_book.day_state(date(2026, 6, 15)) is DayState.ENTERED


def test_raw_three_state_and_effective_state_are_separate(
    annual_book: AnnualDeliveryBook,
):
    p001, p002 = annual_book.roster
    dinner = annual_book.get_delivery_row(p001, "석식")
    stopped_lunch = annual_book.get_delivery_row(p002, "중식")
    assert dinner is not None
    assert stopped_lunch is not None

    # 이후 배송이 없는 개별 빈칸은 중지이며 raw blank 자체도 보존한다.
    assert stopped_lunch.raw_value_on(date(2026, 7, 28)) is DeliveryCellValue.BLANK
    assert (
        annual_book.effective_delivery_state(
            p002, "중식", date(2026, 7, 28)
        )
        is EffectiveDeliveryState.STOPPED
    )

    # 명시적 0은 이후 배송 여부와 관계없이 일시중지다.
    assert dinner.raw_value_on(date(2026, 7, 28)) is DeliveryCellValue.PAUSED
    assert (
        annual_book.effective_delivery_state(
            p001, "석식", date(2026, 7, 28)
        )
        is EffectiveDeliveryState.PAUSED
    )
    assert (
        annual_book.effective_delivery_state(
            p001, "중식", date(2026, 7, 28)
        )
        is EffectiveDeliveryState.DELIVERED
    )

    # 월 전체 미입력/전면 비배송일의 blank는 중지로 해석하지 않는다.
    assert (
        annual_book.effective_delivery_state(
            p001, "석식", date(2026, 9, 1)
        )
        is EffectiveDeliveryState.MONTH_UNENTERED
    )
    assert (
        annual_book.effective_delivery_state(
            p001, "석식", date(2026, 6, 16)
        )
        is EffectiveDeliveryState.NO_DELIVERY
    )
    assert (
        annual_book.effective_delivery_state(
            p001, "석식", date(2026, 6, 14)
        )
        is EffectiveDeliveryState.DATE_MISSING
    )


def test_blank_before_later_delivery_is_inferred_as_paused():
    paused_day = date(2026, 7, 28)
    resumed_day = date(2026, 7, 29)
    paused_person = RosterEntry(source_row=2, extra={"성명": "재개 대상"})
    other_person = RosterEntry(source_row=3, extra={"성명": "당일 배송 대상"})
    paused_row = DeliveryRow(
        paused_person,
        "중식",
        "김기사",
        1,
        {
            paused_day: DeliveryCellValue.BLANK,
            resumed_day: DeliveryCellValue.DELIVERED,
        },
    )
    other_row = DeliveryRow(
        other_person,
        "중식",
        "김기사",
        2,
        {
            paused_day: DeliveryCellValue.DELIVERED,
            resumed_day: DeliveryCellValue.BLANK,
        },
    )
    book = AnnualDeliveryBook(
        2026,
        (paused_person, other_person),
        (paused_row, other_row),
        (paused_day, resumed_day),
    )

    assert paused_row.raw_value_on(paused_day) is DeliveryCellValue.BLANK
    assert (
        book.effective_delivery_state(paused_person, "중식", paused_day)
        is EffectiveDeliveryState.PAUSED
    )


def test_delivery_queries_count_and_numeric_sequence_sort(
    annual_book: AnnualDeliveryBook,
):
    p001, p002 = annual_book.roster
    delivered = annual_book.delivered_rows_on(
        date(2026, 6, 15), meal="중식", staff="김기사"
    )
    assert tuple(row.person for row in delivered) == (p002, p001)
    assert delivered[0].person is p002
    assert delivered[1].person is p001
    assert delivered[0].sequence == 2
    assert delivered[1].sequence == "010"
    assert delivered[1].sequence_display == "010"

    assert annual_book.monthly_delivery_count(p001, "중식", 2026, 6) == 1
    assert annual_book.monthly_delivery_count(p001, "중식", 2026, 7) == 1
    assert annual_book.monthly_delivery_count(p001, "중식", 2026, 9) == 0


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
