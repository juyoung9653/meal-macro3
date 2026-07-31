"""AnnualDeliveryBook 기반 배송 리포트 서비스 계약 테스트."""

from dataclasses import FrozenInstanceError
from datetime import date

import pytest

from src.core.delivery_matrix import (
    ALLOWED_MEALS,
    AnnualDeliveryBook,
    DeliveryCellValue,
    DeliveryRow,
    RosterEntry,
)
from src.services.delivery_service import (
    DeliveryReportResult,
    DeliveryService,
    ReportStatus,
)

BLANK = DeliveryCellValue.BLANK
PAUSED = DeliveryCellValue.PAUSED
DELIVERED = DeliveryCellValue.DELIVERED
HEADER: list[object] = [
    "배송담당",
    "1",
    "2",
    "3",
    "4",
    "5",
    "Total",
    "비고",
    "참고",
]


def _person(
    source_row: int,
    extra: dict[str, str] | None = None,
) -> RosterEntry:
    return RosterEntry(source_row=source_row, extra=extra or {})


def _row(
    person: RosterEntry,
    dates: tuple[date, ...],
    *,
    meal: str = "중식",
    staff: str = "김기사",
    sequence: object = 1,
    delivered: tuple[date, ...] = (),
    paused: tuple[date, ...] = (),
    source_row: int | None = None,
) -> DeliveryRow:
    values = {day: BLANK for day in dates}
    values.update({day: DELIVERED for day in delivered})
    values.update({day: PAUSED for day in paused})
    return DeliveryRow(person, meal, staff, sequence, values, source_row)


def _book(
    dates: tuple[date, ...],
    rows: tuple[DeliveryRow, ...],
) -> AnnualDeliveryBook:
    roster = tuple(dict.fromkeys(row.person for row in rows))
    year = dates[0].year if dates else 2026
    return AnnualDeliveryBook(year, roster, rows, dates)


def test_report_result_is_frozen_and_marks_ready_from_status():
    day = date(2026, 6, 1)
    result = DeliveryReportResult(day, "중식", ReportStatus.READY, [HEADER])

    assert result.is_ready is True
    attribute = "message"
    with pytest.raises(FrozenInstanceError):
        setattr(result, attribute, "변경")

    not_ready = DeliveryReportResult(day, "중식", ReportStatus.DATE_MISSING)
    assert not_ready.is_ready is False


def test_invalid_meal_is_rejected_before_date_lookup():
    service = DeliveryService(AnnualDeliveryBook(2026, (), (), ()))

    result = service.build_report(date(2026, 6, 1), "야식")

    assert result.status is ReportStatus.INVALID_MEAL
    assert result.table is None
    assert "야식" in result.message
    assert result.is_ready is False


def test_missing_date_and_all_blank_day_have_distinct_statuses():
    blank_day = date(2026, 6, 1)
    person = _person(2)
    row = _row(person, (blank_day,))
    service = DeliveryService(_book((blank_day,), (row,)))

    missing = service.build_report(date(2026, 6, 2), "중식")
    all_blank = service.build_report(blank_day, "중식")

    assert missing.status is ReportStatus.DATE_MISSING
    assert "20260602" in missing.message
    assert all_blank.status is ReportStatus.NON_DELIVERY_DAY
    assert all_blank.table is None


def test_zero_only_day_has_no_deliveries_for_meal():
    day = date(2026, 6, 1)
    person = _person(2)
    row = _row(person, (day,), paused=(day,))
    service = DeliveryService(_book((day,), (row,)))

    result = service.build_report(day, "중식")

    assert result.status is ReportStatus.NO_DELIVERIES_FOR_MEAL
    assert "값 1" in result.message


def test_entered_day_without_requested_meal_has_no_deliveries_for_meal():
    day = date(2026, 6, 1)
    person = _person(2)
    lunch = _row(person, (day,), delivered=(day,))
    service = DeliveryService(_book((day,), (lunch,)))

    result = service.build_report(day, "석식")

    assert result.status is ReportStatus.NO_DELIVERIES_FOR_MEAL


def test_weekend_report_uses_only_one_and_ignores_zero_blank_rows():
    saturday = date(2026, 6, 13)
    assert saturday.weekday() == 5
    people = (
        _person(2, {"관할동": "정자동"}),
        _person(5),
        _person(8),
    )
    rows = (
        _row(people[0], (saturday,), staff="주말기사", delivered=(saturday,)),
        _row(people[1], (saturday,), staff=" ", sequence=" ", paused=(saturday,)),
        _row(people[2], (saturday,), staff=" ", sequence=" "),
    )
    book = _book((saturday,), rows)

    result = DeliveryService(book).build_report(saturday, "중식")

    assert result.status is ReportStatus.READY
    assert result.table == [
        HEADER,
        ["주말기사\n(1)", "1(+1)", "", "", "", "", 1, "", "정자동"],
        ["합계", 1, 0, 0, 0, 0, 1, "", ""],
    ]


def test_groups_households_by_trimmed_staff_and_sequence():
    day = date(2026, 6, 1)
    dates = (day,)
    people = tuple(_person(source_row) for source_row in (2, 5, 8, 11))
    rows = (
        _row(people[0], dates, staff=" 김기사 ", sequence=" 01 ", delivered=dates),
        _row(people[1], dates, staff="김기사", sequence="01", delivered=dates),
        _row(people[2], dates, staff="김기사", sequence=2, delivered=dates),
        _row(people[3], dates, staff="이기사", sequence="01", delivered=dates),
    )

    result = DeliveryService(
        _book(dates, rows), staff_sort={"중식": ("김기사", "이기사")}
    ).build_report(day, "중식")

    assert result.table == [
        HEADER,
        ["김기사\n(2)", "1(+1)", "1(+1)", "", "", "", 3, "", ""],
        ["이기사\n(1)", "1(+1)", "", "", "", "", 1, "", ""],
        ["합계", 2, 1, 0, 0, 0, 4, "", ""],
    ]


def test_delivery_row_is_staff_source_and_sort_has_registered_then_casefold_order():
    day = date(2026, 6, 1)
    dates = (day,)
    people = tuple(
        _person(source_row, {"배송담당": "명단기사"})
        for source_row in (2, 5, 8)
    )
    rows = (
        _row(people[0], dates, staff="Bravo", sequence=1, delivered=dates),
        _row(people[1], dates, staff="등록기사", sequence=1, delivered=dates),
        _row(people[2], dates, staff="alpha", sequence=1, delivered=dates),
    )
    service = DeliveryService(
        _book(dates, rows),
        staff_sort={"중식": (" 등록기사 ",)},
    )

    result = service.build_report(day, "중식")

    assert result.table is not None
    assert [row[0] for row in result.table[1:-1]] == [
        "등록기사\n(1)",
        "alpha\n(1)",
        "Bravo\n(1)",
    ]
    assert all("명단기사" not in str(row[0]) for row in result.table)


def test_previous_day_is_aggregated_independently_and_previous_only_staff_is_shown():
    previous = date(2026, 5, 29)
    current = date(2026, 6, 1)
    dates = (previous, current)
    people = tuple(_person(source_row) for source_row in (2, 5, 8))
    rows = (
        _row(people[0], dates, staff="현기사", sequence=1, delivered=dates),
        _row(people[1], dates, staff="현기사", sequence=1, delivered=(previous,)),
        _row(people[2], dates, staff="이전기사", sequence=9, delivered=(previous,)),
    )
    service = DeliveryService(
        _book(dates, rows),
        staff_sort={"중식": ("현기사", "이전기사")},
    )

    result = service.build_report(current, "중식")

    assert result.table == [
        HEADER,
        ["현기사\n(1)", "1(+1)", "0(-1)", "", "", "", 1, "", ""],
        ["이전기사\n(0)", "0(-1)", "", "", "", "", 0, "", ""],
        ["합계", 1, 0, 0, 0, 0, 1, "", ""],
    ]


def test_reference_areas_join_current_and_previous_people_uniquely_and_deterministically():
    previous = date(2026, 5, 29)
    current = date(2026, 6, 1)
    dates = (previous, current)
    people = (
        _person(2, {"관할동": "나동"}),
        _person(5, {"관할동": "가동"}),
        _person(8, {"관할동": "가동"}),
        _person(11, {"관할동": "다동"}),
    )
    rows = (
        _row(people[0], dates, staff="김기사", sequence=1, delivered=(current,)),
        _row(people[1], dates, staff="김기사", sequence=2, delivered=(previous,)),
        _row(people[2], dates, staff="김기사", sequence=3, delivered=(current,)),
        _row(people[3], dates, staff="김기사", sequence=4, delivered=(previous,)),
    )

    result = DeliveryService(_book(dates, rows)).build_report(current, "중식")

    assert result.table is not None
    assert result.table[1][8] == "가동, 나동\n다동"
    assert result.table[1][7] == ""


@pytest.mark.parametrize(
    ("staff", "sequence", "missing_name"),
    [
        ("   ", 1, "담당자"),
        ("김기사", "   ", "연번"),
    ],
)
def test_blank_delivered_location_key_is_invalid_data(
    staff: str, sequence: object, missing_name: str
):
    day = date(2026, 6, 1)
    person = _person(20, {"성명": "오류 대상"})
    row = _row(
        person,
        (day,),
        staff=staff,
        sequence=sequence,
        delivered=(day,),
        source_row=21,
    )

    result = DeliveryService(_book((day,), (row,))).build_report(day, "중식")

    assert result.status is ReportStatus.INVALID_DATA
    assert result.table is None
    assert missing_name in result.message
    assert "오류 대상" in result.message
    assert "명단 20~22행" in result.message
    assert "원본 21행" in result.message


def test_invalid_previous_location_key_reports_person_and_source_rows():
    previous = date(2026, 5, 29)
    current = date(2026, 6, 1)
    dates = (previous, current)
    current_person = _person(2, {"성명": "현재 대상"})
    previous_person = _person(11, {"성명": "직전 오류 대상"})
    rows = (
        _row(current_person, dates, sequence=1, delivered=(current,), source_row=3),
        _row(
            previous_person,
            dates,
            sequence=" ",
            delivered=(previous,),
            source_row=12,
        ),
    )

    result = DeliveryService(_book(dates, rows)).build_report(current, "중식")

    assert result.status is ReportStatus.INVALID_DATA
    assert "직전 오류 대상" in result.message
    assert "명단 11~13행" in result.message
    assert "원본 12행" in result.message
    assert "직전 배송일" in result.message


def test_six_or_more_people_at_one_location_is_invalid_data():
    day = date(2026, 6, 1)
    people = tuple(
        _person(2 + index * 3, {"성명": f"대상 {index + 1}"})
        for index in range(6)
    )
    rows = tuple(
        _row(person, (day,), sequence="A-1", delivered=(day,))
        for person in people
    )

    result = DeliveryService(_book((day,), rows)).build_report(day, "중식")

    assert result.status is ReportStatus.INVALID_DATA
    assert "6명 이상" in result.message
    assert "1~5명 템플릿" in result.message


def test_build_all_returns_exact_ready_export_tuples_in_allowed_meal_order():
    previous = date(2026, 6, 12)
    blank_day = date(2026, 6, 13)
    zero_only_day = date(2026, 6, 14)
    ready_day = date(2026, 6, 15)
    after_range = date(2026, 6, 16)
    dates = (after_range, ready_day, zero_only_day, blank_day, previous)
    lunch_person = _person(2, {"성명": "중식 대상", "관할동": "가동"})
    dinner_person = _person(5, {"성명": "석식 대상", "관할동": "나동"})
    lunch = _row(
        lunch_person,
        dates,
        meal="중식",
        staff="중식기사",
        delivered=(previous, ready_day, after_range),
        paused=(zero_only_day,),
    )
    dinner = _row(
        dinner_person,
        dates,
        meal="석식",
        staff="석식기사",
        delivered=(ready_day,),
    )
    book = _book(dates, (lunch, dinner))
    service = DeliveryService(book)

    results = service.build_all(blank_day, ready_day)

    lunch_table = [
        HEADER,
        ["중식기사\n(1)", "1", "", "", "", "", 1, "", "가동"],
        ["합계", 1, 0, 0, 0, 0, 1, "", ""],
    ]
    dinner_table = [
        HEADER,
        ["석식기사\n(1)", "1(+1)", "", "", "", "", 1, "", "나동"],
        ["합계", 1, 0, 0, 0, 0, 1, "", ""],
    ]
    assert results == [
        ("20260615", "중식", lunch_table),
        ("20260615", "석식", dinner_table),
    ]
    assert [meal for _, meal, _ in results] == [
        meal for meal in ALLOWED_MEALS if meal in {"중식", "석식"}
    ]
    assert service.build_all(blank_day, ready_day, meal_filter="석식") == [
        ("20260615", "석식", dinner_table)
    ]


def test_build_all_raises_instead_of_silently_skipping_invalid_delivery_data():
    day = date(2026, 6, 1)
    person = _person(14, {"성명": "연번 오류 대상"})
    row = _row(person, (day,), sequence="", delivered=(day,), source_row=15)
    service = DeliveryService(_book((day,), (row,)))

    with pytest.raises(ValueError, match="연번.*연번 오류 대상.*원본 15행"):
        service.build_all(day, day)


def test_build_all_validates_range_and_meal_filter():
    service = DeliveryService(AnnualDeliveryBook(2026, (), (), ()))
    first = date(2026, 6, 1)
    second = date(2026, 6, 2)

    with pytest.raises(ValueError, match="종료일"):
        service.build_all(second, first)
    with pytest.raises(ValueError, match="식종 필터"):
        service.build_all(first, second, meal_filter="")
    with pytest.raises(ValueError, match="야식"):
        service.build_all(first, second, meal_filter="야식")
