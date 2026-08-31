"""AnnualDeliveryBook 기반 청구서 서비스 테스트."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import Mock

import pytest

from src.core.delivery_matrix import (
    ALLOWED_MEALS,
    AnnualDeliveryBook,
    DeliveryCellValue,
    DeliveryRow,
    RosterEntry,
)
from src.services import invoice_controller
from src.services.invoice_controller import InvoiceGenerationConfig, InvoiceService

_MEAL_SOURCE_ROW_OFFSETS = {"조식": 0, "중식": 1, "석식": 2}


def _delivery_row(
    person: RosterEntry,
    meal: str,
    staff: str,
    sequence: object,
    dates: tuple[date, ...],
    *,
    delivered: tuple[date, ...] = (),
    paused: tuple[date, ...] = (),
) -> DeliveryRow:
    values = {day: DeliveryCellValue.BLANK for day in dates}
    values.update({day: DeliveryCellValue.DELIVERED for day in delivered})
    values.update({day: DeliveryCellValue.PAUSED for day in paused})
    return DeliveryRow(
        person=person,
        meal=meal,
        staff=staff,
        sequence=sequence,
        values=values,
        source_row=person.source_row + _MEAL_SOURCE_ROW_OFFSETS[meal],
    )


def _complete_meal_rows(
    roster: tuple[RosterEntry, ...],
    rows: tuple[DeliveryRow, ...],
    dates: tuple[date, ...],
) -> tuple[DeliveryRow, ...]:
    completed_rows = list(rows)
    for person in roster:
        present_meals = {row.meal for row in rows if row.person is person}
        completed_rows.extend(
            _delivery_row(person, meal, "", "", dates)
            for meal in ALLOWED_MEALS
            if meal not in present_meals
        )
    return tuple(completed_rows)


def _service_book() -> AnnualDeliveryBook:
    # 물리적 헤더 순서를 흉내 내어 역순/타 월 날짜를 섞는다.
    dates = (
        date(2026, 7, 1),
        date(2026, 6, 30),
        date(2026, 6, 28),
        date(2026, 6, 21),
        date(2026, 6, 14),
        date(2026, 6, 7),
        date(2026, 6, 2),  # 모든 행 blank인 비배송일
        date(2026, 6, 1),
        date(2026, 5, 31),
    )
    person = RosterEntry(
        source_row=2,
        extra={
            "성명": "홍길동",
            "학교명": "분당초",
            "관할동": "정자동",
            "도로명주소": "성남시 1",
            "세대주\n성명": "홍보호",
            "핸드폰": "010-1111-1111",
            "비고": "견과류 알레르기",
            # 기존 명단 플래그가 0이어도 배송 행의 1을 억제하면 안 된다.
            "조식 여부": 0,
            "중식 여부": 0,
            "석식 여부": 0,
        },
    )
    person_without_delivery_rows = RosterEntry(
        source_row=5,
        extra={
            "성명": "배송행없음",
            "학교명": "분당초",
            "관할동": "정자동",
            "도로명주소": "성남시 2",
            "세대주\n성명": "김보호",
            "핸드폰": "010-2222-2222",
            "비고": "",
            # 식사 여부 컬럼 자체가 없어도 된다.
        },
    )
    roster = (person, person_without_delivery_rows)
    rows = (
        _delivery_row(
            person,
            "석식",
            "기사B",
            2,
            dates,
            delivered=(date(2026, 6, 30),),
        ),
        _delivery_row(
            person,
            "조식",
            "기사A",
            "003",
            dates,
            delivered=(
                date(2026, 5, 31),  # 타 월: 제외
                date(2026, 6, 1),
                date(2026, 6, 7),
                date(2026, 7, 1),  # 타 월: 제외
            ),
        ),
        _delivery_row(
            person,
            "중식",
            "기사A",
            "003",
            dates,
            delivered=(date(2026, 6, 14),),
        ),
    )
    return AnnualDeliveryBook(
        2026,
        roster,
        _complete_meal_rows(roster, rows, dates),
        dates,
    )


def _month_validation_book(
    year: int,
    dates: tuple[date, ...],
    *,
    delivered: tuple[date, ...] = (),
    include_delivery_row: bool = True,
) -> AnnualDeliveryBook:
    person = RosterEntry(source_row=2, extra={"성명": "홍길동"})
    delivery_rows: tuple[DeliveryRow, ...] = ()
    if include_delivery_row:
        delivery_rows = (
            _delivery_row(
                person,
                "중식",
                "기사A",
                1,
                dates,
                delivered=delivered,
            ),
        )
    roster = (person,)
    return AnnualDeliveryBook(
        year,
        roster,
        _complete_meal_rows(roster, delivery_rows, dates),
        dates,
    )


@pytest.fixture
def template_path(tmp_path: Path) -> str:
    path = tmp_path / "template.xlsx"
    path.touch()
    return str(path)


@pytest.fixture
def invoice_spy(monkeypatch: pytest.MonkeyPatch) -> Mock:
    spy = Mock(
        side_effect=lambda batch, *_args, **_kwargs: (len(batch.deliveries), batch)
    )
    monkeypatch.setattr(invoice_controller, "make_invoice", spy)
    return spy


def _config(
    template_path: str,
    book: AnnualDeliveryBook | None = None,
    **overrides: object,
) -> InvoiceGenerationConfig:
    values: dict[str, object] = {
        "template_path": template_path,
        "output_path": "out.xlsx",
        "year": 2026,
        "month": 6,
        "annual_book": book,
    }
    values.update(overrides)
    return InvoiceGenerationConfig(**values)  # type: ignore[arg-type]


def test_counts_only_target_month_delivered_and_ignores_roster_flags(
    template_path: str, invoice_spy: Mock
):
    result = InvoiceService().generate(_config(template_path, _service_book()))

    assert result.success, result.error
    assert result.count == len(result.enriched) == 1
    person = result.enriched[0]
    assert (person["_bf_cnt"], person["_lh_cnt"], person["_dn_cnt"]) == (2, 1, 1)
    assert person["_total"] == 4
    assert invoice_spy.call_args.args[0] is result.batch

    # 원본 비고는 그대로 남고 모든 레거시 전달 필드는 더 이상 만들지 않는다.
    assert person.get("비고") == "견과류 알레르기"
    assert person["_note"] == "견과류 알레르기"
    assert {
        "_breakfast",
        "_dinner",
        "_lunch",
        "_delivery_start",
        "_delivery_end",
        "_daily",
        "_delivery_rows_raw",
        "_reception_memo",
        "excluded_dates",
    }.isdisjoint(person)


def test_generates_invoice_for_people_with_one_meal_row_each(
    template_path: str,
    invoice_spy: Mock,
):
    day = date(2026, 6, 1)
    people = tuple(
        RosterEntry(source_row=row, extra={"성명": name})
        for row, name in ((2, "김아침"), (3, "이저녁"), (4, "박점심"))
    )
    rows = tuple(
        DeliveryRow(
            person=person,
            meal=meal,
            staff="김기사",
            sequence=None,
            values={day: DeliveryCellValue.DELIVERED},
            source_row=person.source_row,
        )
        for person, meal in zip(people, ("조식", "석식", "중식"))
    )
    book = AnnualDeliveryBook(2026, people, rows, (day,))

    result = InvoiceService().generate(_config(template_path, book))

    assert result.success, result.error
    assert result.count == 3
    counts_by_name = {
        item["성명"]: (item["_bf_cnt"], item["_lh_cnt"], item["_dn_cnt"])
        for item in result.enriched
    }
    assert counts_by_name == {
        "김아침": (3, 0, 0),
        "이저녁": (0, 0, 1),
        "박점심": (0, 1, 0),
    }


def test_projects_staff_and_sequence_from_person_delivery_rows_deterministically(
    template_path: str, invoice_spy: Mock
):
    result = InvoiceService().generate(_config(template_path, _service_book()))

    assert result.success, result.error
    person = result.enriched[0]
    # 식종의 정규 순서(조식→중식→석식), 중복 제거 규칙.
    assert person["배송담당"] == "기사A / 기사B"
    assert person["배송순서"] == "003 / 2"
    assert person["연번"] == "003 / 2"


def test_metadata_uses_only_rows_delivered_in_target_month(
    template_path: str, invoice_spy: Mock
):
    days = (date(2026, 6, 1), date(2026, 7, 1))
    person = RosterEntry(source_row=2, extra={"성명": "홍길동"})
    book = AnnualDeliveryBook(
        2026,
        (person,),
        _complete_meal_rows(
            (person,),
            (
                _delivery_row(
                    person,
                    "조식",
                    "6월기사",
                    "006",
                    days,
                    delivered=(date(2026, 6, 1),),
                ),
                _delivery_row(
                    person,
                    "석식",
                    "7월기사",
                    "007",
                    days,
                    delivered=(date(2026, 7, 1),),
                ),
            ),
            days,
        ),
        days,
    )

    result = InvoiceService().generate(_config(template_path, book))

    assert result.success, result.error
    assert result.count == len(result.enriched) == 1
    assert result.enriched[0]["배송담당"] == "6월기사"
    assert result.enriched[0]["배송순서"] == "006"
    assert result.enriched[0]["연번"] == "006"


def test_rejects_blank_staff_on_target_month_delivered_row(
    template_path: str,
    invoice_spy: Mock,
):
    days = (date(2026, 6, 1),)
    person = RosterEntry(source_row=2, extra={"성명": "홍길동"})
    book = AnnualDeliveryBook(
        2026,
        (person,),
        _complete_meal_rows(
            (person,),
            (
                _delivery_row(
                    person,
                    "조식",
                    "  ",
                    "001",
                    days,
                    delivered=days,
                ),
            ),
            days,
        ),
        days,
    )

    result = InvoiceService().generate(_config(template_path, book))

    assert not result.success
    assert result.error is not None
    assert "'홍길동' (명단 2행)" in result.error
    assert "원본 2행" in result.error
    assert "조식" in result.error
    assert "배송담당(staff)" in result.error
    invoice_spy.assert_not_called()


def test_allows_blank_sequence_on_target_month_delivered_row(
    template_path: str,
    invoice_spy: Mock,
):
    days = (date(2026, 6, 1),)
    person = RosterEntry(source_row=2, extra={"성명": "홍길동"})
    book = AnnualDeliveryBook(
        2026,
        (person,),
        _complete_meal_rows(
            (person,),
            (
                _delivery_row(
                    person,
                    "조식",
                    "기사A",
                    "  ",
                    days,
                    delivered=days,
                ),
            ),
            days,
        ),
        days,
    )

    result = InvoiceService().generate(_config(template_path, book))

    assert result.success, result.error
    assert result.enriched[0]["배송담당"] == "기사A"
    assert result.enriched[0]["배송순서"] == ""
    assert result.enriched[0]["연번"] == ""
    invoice_spy.assert_called_once()


def test_attaches_exact_weekly_meal_counts_once_from_typed_dates(
    template_path: str, invoice_spy: Mock
):
    result = InvoiceService().generate(_config(template_path, _service_book()))

    assert result.success, result.error
    assert result.enriched[0]["weekly_meal_counts"] == [
        {
            "week": 1,
            "start_date": "20260601",
            "end_date": "20260606",
            "조식": 1,
            "중식": 0,
            "석식": 0,
        },
        {
            "week": 2,
            "start_date": "20260607",
            "end_date": "20260613",
            "조식": 1,
            "중식": 0,
            "석식": 0,
        },
        {
            "week": 3,
            "start_date": "20260614",
            "end_date": "20260620",
            "조식": 0,
            "중식": 1,
            "석식": 0,
        },
        {
            "week": 4,
            "start_date": "20260621",
            "end_date": "20260627",
            "조식": 0,
            "중식": 0,
            "석식": 0,
        },
        {
            "week": 5,
            "start_date": "20260628",
            "end_date": "20260630",
            "조식": 0,
            "중식": 0,
            "석식": 1,
        },
    ]


@pytest.mark.parametrize(
    ("delivered", "paused", "expected_count"),
    [
        ((date(2026, 6, 5),), (), 3),
        (
            (
                date(2026, 6, 5),
                date(2026, 6, 6),
                date(2026, 6, 7),
            ),
            (),
            3,
        ),
        ((date(2026, 6, 5),), (date(2026, 6, 6),), 1),
    ],
)
def test_breakfast_uses_weekend_cells_or_vba_three_meal_fallback(
    template_path: str,
    invoice_spy: Mock,
    delivered: tuple[date, ...],
    paused: tuple[date, ...],
    expected_count: int,
):
    days = tuple(date(2026, 6, day) for day in range(5, 8))
    person = RosterEntry(source_row=2, extra={"성명": "김아침"})
    book = AnnualDeliveryBook(
        2026,
        (person,),
        (
            _delivery_row(
                person,
                "조식",
                "기사A",
                1,
                days,
                delivered=delivered,
                paused=paused,
            ),
        ),
        days,
    )

    result = InvoiceService().generate(_config(template_path, book))

    assert result.success, result.error
    assert result.enriched[0]["_bf_cnt"] == expected_count
    assert sum(
        week["조식"]
        for week in result.enriched[0]["weekly_meal_counts"]
    ) == expected_count
    invoice_spy.assert_called_once()


def test_blank_only_month_is_rejected_as_unentered(
    template_path: str, invoice_spy: Mock
):
    days = (date(2026, 6, 1), date(2026, 6, 2))
    person = RosterEntry(source_row=2, extra={"성명": "홍길동"})
    book = AnnualDeliveryBook(
        2026,
        (person,),
        (_delivery_row(person, "중식", "기사A", 1, days),),
        days,
    )

    result = InvoiceService().generate(_config(template_path, book))

    assert not result.success
    assert "미입력" in (result.error or "")
    invoice_spy.assert_not_called()


def test_zero_only_entered_month_proceeds_with_zero_invoice(
    template_path: str, invoice_spy: Mock
):
    days = (date(2026, 6, 1), date(2026, 6, 2))
    person = RosterEntry(source_row=2, extra={"성명": "홍길동"})
    book = AnnualDeliveryBook(
        2026,
        (person,),
        _complete_meal_rows(
            (person,),
            (
                _delivery_row(
                    person,
                    "중식",
                    "기사A",
                    1,
                    days,
                    paused=(date(2026, 6, 1),),
                ),
            ),
            days,
        ),
        days,
    )

    result = InvoiceService().generate(_config(template_path, book))

    assert result.success, result.error
    assert result.count == 0
    assert result.enriched == []
    assert result.batch is not None and result.batch.deliveries == ()
    invoice_spy.assert_called_once()


@pytest.mark.parametrize(
    ("book", "year", "month", "message"),
    [
        (
            _month_validation_book(
                2025,
                (date(2025, 6, 1),),
                delivered=(date(2025, 6, 1),),
            ),
            2026,
            6,
            "연도",
        ),
        (
            _month_validation_book(2026, (), include_delivery_row=False),
            2026,
            6,
            "날짜 헤더",
        ),
        (
            _month_validation_book(
                2026,
                (date(2026, 5, 1),),
                delivered=(date(2026, 5, 1),),
            ),
            2026,
            6,
            "6월 날짜 헤더",
        ),
        (
            _month_validation_book(2026, (date(2026, 6, 1),)),
            2026,
            6,
            "미입력",
        ),
    ],
)
def test_rejects_wrong_year_missing_headers_and_unentered_month(
    template_path: str,
    invoice_spy: Mock,
    book: AnnualDeliveryBook,
    year: int,
    month: int,
    message: str,
):
    result = InvoiceService().generate(
        _config(template_path, book, year=year, month=month)
    )

    assert not result.success
    assert message in (result.error or "")
    invoice_spy.assert_not_called()


@pytest.mark.parametrize("month", [0, 13])
def test_rejects_invalid_month_numbers_before_generation(
    template_path: str, invoice_spy: Mock, month: int
):
    result = InvoiceService().generate(
        _config(template_path, _service_book(), month=month)
    )

    assert not result.success
    assert "1부터 12" in (result.error or "")
    invoice_spy.assert_not_called()


def test_vacation_detection_uses_only_target_month_delivered_lunch():
    days = (date(2026, 7, 1), date(2026, 8, 1))
    person = RosterEntry(source_row=2, extra={"성명": "홍길동"})
    book = AnnualDeliveryBook(
        2026,
        (person,),
        _complete_meal_rows(
            (person,),
            (
                _delivery_row(
                    person,
                    "중식",
                    "기사A",
                    1,
                    days,
                    delivered=(date(2026, 7, 1),),
                ),
                _delivery_row(
                    person,
                    "조식",
                    "기사A",
                    1,
                    days,
                ),
            ),
            days,
        ),
        days,
    )

    assert InvoiceService._is_vacation_month(book, 2026, 7)
    assert not InvoiceService._is_vacation_month(book, 2026, 8)


def test_uses_default_prices_and_allows_partial_explicit_override(
    template_path: str, invoice_spy: Mock
):
    config = _config(
        template_path,
        _service_book(),
        unit_prices={"조식": 10_000, "기타": 1},
    )

    result = InvoiceService().generate(config)

    assert result.success, result.error
    assert invoice_spy.call_args.kwargs["unit_prices"] == {
        "조식": 10_000,
        "중식": 9_500,
        "석식": 9_500,
    }


def test_passes_global_note_to_invoice_writer(
    template_path: str,
    invoice_spy: Mock,
):
    note = "단가 변경: 중식 1~14일 10,000원 / 15~30일 11,000원"

    result = InvoiceService().generate(
        _config(template_path, _service_book(), global_note=note)
    )

    assert result.success, result.error
    assert invoice_spy.call_args.kwargs["global_note"] == note


def test_passes_output_password_to_invoice_writer(
    template_path: str,
    invoice_spy: Mock,
):
    result = InvoiceService().generate(
        _config(
            template_path,
            _service_book(),
            output_password="shared-1234",
        )
    )

    assert result.success, result.error
    assert invoice_spy.call_args.kwargs["output_password"] == "shared-1234"


def test_uses_central_settings_unit_price_override(
    template_path: str, invoice_spy: Mock
):
    invoice_controller.get_settings().set(
        "invoice",
        "unit_prices",
        value={"조식": 1_111, "중식": 2_222, "석식": 3_333, "기타": 1},
    )

    result = InvoiceService().generate(_config(template_path, _service_book()))

    assert result.success, result.error
    assert invoice_spy.call_args.kwargs["unit_prices"] == {
        "조식": 1_111,
        "중식": 2_222,
        "석식": 3_333,
    }
    assert result.enriched[0]["_amount"] == "7,777원"


def test_loads_book_through_annual_workbook_when_only_path_is_configured(
    template_path: str, invoice_spy: Mock, monkeypatch: pytest.MonkeyPatch
):
    book = _service_book()
    adapter = Mock()
    adapter.load.return_value = book
    adapter_type = Mock(return_value=adapter)
    monkeypatch.setattr(invoice_controller, "AnnualWorkbook", adapter_type)

    result = InvoiceService().generate(
        _config(
            template_path,
            None,
            annual_workbook_path="annual.xlsx",
            annual_workbook_password="secret",
        )
    )

    assert result.success, result.error
    adapter_type.assert_called_once_with("annual.xlsx", password="secret")
    adapter.load.assert_called_once_with()


def test_missing_book_and_path_is_a_clear_failure(
    template_path: str, invoice_spy: Mock, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        invoice_controller.get_settings(),
        "_data",
        {"invoice": {"unit_prices": {"조식": 9500, "중식": 9500, "석식": 9500}}},
    )

    result = InvoiceService().generate(_config(template_path, None))

    assert not result.success
    assert "연간 배송" in (result.error or "")
    assert "경로" in (result.error or "")
    invoice_spy.assert_not_called()
