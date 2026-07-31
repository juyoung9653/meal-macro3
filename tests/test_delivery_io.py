"""AnnualDeliveryBook 기반 배송대타 투영 테스트."""

from dataclasses import FrozenInstanceError
from datetime import date

import pandas as pd
import pytest

from src.core.delivery_matrix import (
    AnnualDeliveryBook,
    DeliveryCellValue,
    DeliveryRow,
    RosterEntry,
)
from src.excel.delivery_io import (
    Choice,
    available_delivery_dates,
    available_meals,
    available_staff,
    make_result,
)

DELIVERY_DAY = date(2031, 6, 15)
ZERO_ONLY_DAY = date(2031, 6, 16)
FULL_BLANK_DAY = date(2031, 6, 17)
DATES = (FULL_BLANK_DAY, DELIVERY_DAY, ZERO_ONLY_DAY)


def _values(
    delivery_value: DeliveryCellValue,
    *,
    zero_value: DeliveryCellValue = DeliveryCellValue.BLANK,
) -> dict[date, DeliveryCellValue]:
    return {
        DELIVERY_DAY: delivery_value,
        ZERO_ONLY_DAY: zero_value,
        FULL_BLANK_DAY: DeliveryCellValue.BLANK,
    }


@pytest.fixture
def annual_book() -> AnnualDeliveryBook:
    shared_phone = "010-0000-0000"
    shared_road_address = "공통 도로명주소"
    first_person = RosterEntry(
        source_row=2,
        extra={
            "성명": "일번",
            "이름": "사용하면 안 됨",
            "관할동": "정자동",
            "상세주소": "상세 1",
            "주소": "사용하면 안 됨",
            "도로명주소": shared_road_address,
            "신주소": "사용하면 안 됨",
            "핸드폰": shared_phone,
            "비고": "비고 1",
            "비고2": "사용하면 안 됨",
            "고객코드": "0001",
            "결측": None,
            "배송순서": "999",
            "연번": "999",
            "배송담당": "명단 담당",
            "배송_new": "명단 담당",
            "식종": "석식",
            "식사 구분": "석식",
        },
    )
    second_person = RosterEntry(
        source_row=5,
        extra={
            "이름": "이번",
            "관할동": None,
            "주소": "상세 2",
            "신주소": shared_road_address,
            "핸드폰": shared_phone,
            "비고2": "비고 2",
            "고객코드": "0002",
            "결측": float("nan"),
        },
    )
    tenth_person = RosterEntry(
        source_row=8,
        extra={
            "성명": None,
            "이름": "십번",
            "관할동": "수내동",
            "상세주소": float("nan"),
            "주소": "상세 10",
            "도로명주소": None,
            "신주소": shared_road_address,
            "핸드폰": shared_phone,
            "비고": None,
            "비고2": "비고 10",
            "고객코드": float("nan"),
        },
    )
    paused_person = RosterEntry(
        source_row=11,
        extra={"성명": "중지", "핸드폰": shared_phone},
    )
    blank_person = RosterEntry(
        source_row=14,
        extra={"성명": "빈칸", "핸드폰": shared_phone},
    )
    dinner_person = RosterEntry(
        source_row=17,
        extra={"성명": "저녁", "핸드폰": shared_phone},
    )
    roster = (
        first_person,
        second_person,
        tenth_person,
        paused_person,
        blank_person,
        dinner_person,
    )
    rows = (
        DeliveryRow(
            tenth_person,
            "중식",
            "기사A",
            "10",
            _values(DeliveryCellValue.DELIVERED),
            source_row=9,
        ),
        DeliveryRow(
            first_person,
            "중식",
            "기사A",
            "001",
            _values(
                DeliveryCellValue.DELIVERED,
                zero_value=DeliveryCellValue.PAUSED,
            ),
            source_row=3,
        ),
        DeliveryRow(
            second_person,
            "중식",
            "기사A",
            2,
            _values(DeliveryCellValue.DELIVERED),
            source_row=6,
        ),
        DeliveryRow(
            paused_person,
            "중식",
            "중지기사",
            4,
            _values(DeliveryCellValue.PAUSED),
            source_row=12,
        ),
        DeliveryRow(
            blank_person,
            "중식",
            "빈칸기사",
            5,
            _values(DeliveryCellValue.BLANK),
            source_row=15,
        ),
        DeliveryRow(
            dinner_person,
            "석식",
            "기사B",
            1,
            _values(DeliveryCellValue.DELIVERED),
            source_row=19,
        ),
    )
    return AnnualDeliveryBook(2031, roster, rows, DATES)


def test_choice_is_frozen_and_accepts_list_or_tuple_columns():
    list_choice = Choice("중식", "기사A", DELIVERY_DAY, ["이름"])
    tuple_choice = Choice("중식", "기사A", DELIVERY_DAY, ("이름",))

    assert list_choice.cols == ["이름"]
    assert tuple_choice.cols == ("이름",)
    with pytest.raises(FrozenInstanceError):
        list_choice.meal = "석식"  # type: ignore[misc]


def test_make_result_projects_exact_requested_columns(
    annual_book: AnnualDeliveryBook,
):
    columns = (
        "배송순서",
        "연번",
        "배송담당",
        "배송_new",
        "식종",
        "식사 구분",
        "이름",
        "관할동",
        "상세주소",
        "신주소",
        "핸드폰",
        "비고2",
        "고객코드",
        "결측",
        "없는컬럼",
    )

    result = make_result(
        annual_book,
        Choice("중식", "기사A", DELIVERY_DAY, columns),
    )

    expected = pd.DataFrame(
        [
            [
                "001",
                "001",
                "기사A",
                "기사A",
                "중식",
                "중식",
                "일번",
                "정자동",
                "상세 1",
                "공통 도로명주소",
                "010-0000-0000",
                "비고 1",
                "0001",
                "",
                "",
            ],
            [
                "2",
                "2",
                "기사A",
                "기사A",
                "중식",
                "중식",
                "이번",
                "",
                "상세 2",
                "공통 도로명주소",
                "010-0000-0000",
                "비고 2",
                "0002",
                "",
                "",
            ],
            [
                "10",
                "10",
                "기사A",
                "기사A",
                "중식",
                "중식",
                "십번",
                "수내동",
                "상세 10",
                "공통 도로명주소",
                "010-0000-0000",
                "비고 10",
                "",
                "",
                "",
            ],
        ],
        columns=columns,
    )
    pd.testing.assert_frame_equal(result, expected)


def test_selection_uses_only_one_cells_and_domain_numeric_order(
    annual_book: AnnualDeliveryBook,
):
    result = make_result(
        annual_book,
        Choice("중식", "기사A", DELIVERY_DAY, ["배송순서"]),
    )

    assert result.to_dict("records") == [
        {"배송순서": "001"},
        {"배송순서": "2"},
        {"배송순서": "10"},
    ]


def test_options_use_day_state_but_meals_and_staff_use_only_one_cells(
    annual_book: AnnualDeliveryBook,
):
    assert available_delivery_dates(annual_book) == [DELIVERY_DAY, ZERO_ONLY_DAY]
    assert available_meals(annual_book, DELIVERY_DAY) == ["중식", "석식"]
    assert available_staff(annual_book, DELIVERY_DAY, "중식") == ["기사A"]

    assert available_meals(annual_book, ZERO_ONLY_DAY) == []
    assert available_staff(annual_book, ZERO_ONLY_DAY, "중식") == []
    assert FULL_BLANK_DAY not in available_delivery_dates(annual_book)


def test_zero_only_date_returns_empty_frame_with_exact_columns(
    annual_book: AnnualDeliveryBook,
):
    columns = ["없는컬럼", "이름"]

    result = make_result(
        annual_book,
        Choice("중식", "기사A", ZERO_ONLY_DAY, columns),
    )

    assert result.empty
    assert result.columns.tolist() == columns


@pytest.mark.parametrize("ship", ["", "   "])
def test_rejects_blank_staff_choice(annual_book: AnnualDeliveryBook, ship: str):
    with pytest.raises(ValueError, match="배송담당.*빈칸"):
        make_result(
            annual_book,
            Choice("중식", ship, DELIVERY_DAY, ["이름"]),
        )


def test_rejects_invalid_meal(annual_book: AnnualDeliveryBook):
    with pytest.raises(ValueError, match="허용되지 않은 식종.*간식"):
        make_result(
            annual_book,
            Choice("간식", "기사A", DELIVERY_DAY, ["이름"]),
        )


@pytest.mark.parametrize(
    ("day", "message"),
    [
        (date(2030, 6, 15), "선택 날짜가 없습니다.*2030-06-15"),
        (date(2031, 6, 18), "선택 날짜가 없습니다.*2031-06-18"),
        (FULL_BLANK_DAY, "전체 빈칸.*2031-06-17"),
    ],
)
def test_rejects_missing_actual_date_or_full_blank_day(
    annual_book: AnnualDeliveryBook,
    day: date,
    message: str,
):
    with pytest.raises(ValueError, match=message):
        make_result(
            annual_book,
            Choice("중식", "기사A", day, ["이름"]),
        )


def test_rejects_non_date_value(annual_book: AnnualDeliveryBook):
    with pytest.raises(TypeError, match="datetime.date"):
        make_result(
            annual_book,
            Choice("중식", "기사A", "2031-06-15", ["이름"]),  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("sequence", [None, "   ", float("nan")])
def test_rejects_selected_row_with_blank_sequence(sequence: object):
    person = RosterEntry(source_row=20, extra={"성명": "연번없음"})
    row = DeliveryRow(
        person=person,
        meal="중식",
        staff="기사A",
        sequence=sequence,
        values={DELIVERY_DAY: DeliveryCellValue.DELIVERED},
        source_row=21,
    )
    book = AnnualDeliveryBook(
        2031,
        (person,),
        (row,),
        (DELIVERY_DAY,),
    )

    with pytest.raises(
        ValueError,
        match="성명=연번없음.*Excel 원본 행=21",
    ):
        make_result(
            book,
            Choice("중식", "기사A", DELIVERY_DAY, ["이름"]),
        )
