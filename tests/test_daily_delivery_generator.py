"""일일배송 컬럼 우선순위, 날짜 계산, 템플릿 생성 테스트."""

from datetime import date
from pathlib import Path

import pytest
from openpyxl import load_workbook

from src.generators.daily_delivery_generator import (
    build_daily_delivery_sheet,
    build_delivery_routes,
    format_household_distribution,
    make_daily_delivery,
    prepare_daily_delivery_people,
    resolve_delivery_assignment,
    select_delivery_dates,
    weekly_confirmation_memo_entries,
)

TEMPLATE_PATH = Path("assets/일일배송템플릿.xlsx")
LUNCH_TEMPLATE_PATH = Path("assets/중식일일배송템플릿.xlsx")


def _person(
    driver: str,
    primary: str,
    secondary: str,
    address: str,
    *,
    breakfast: int = 1,
    dinner: int = 1,
    lunch: int = 0,
    lunch_dates: list[str] | None = None,
    note: str = "",
) -> dict:
    person = {
        "기사": driver,
        "1순위": primary,
        "2순위": secondary,
        "주소": address,
        "_breakfast": breakfast,
        "_dinner": dinner,
        "_lunch": lunch,
        "_note": note,
    }
    if lunch_dates is not None:
        person["_lunch_dates"] = lunch_dates
    return person


def _build(people: list[dict], target: date = date(2026, 5, 2)):
    return build_daily_delivery_sheet(
        people,
        target,
        driver_col="기사",
        region_primary_col="1순위",
        region_secondary_col="2순위",
        address_col="주소",
    )


class TestColumnAssignments:
    def test_primary_region_wins_when_both_have_values(self):
        person = _person("기사 A", "1순위 구역", "2순위 구역", "주소")

        assert resolve_delivery_assignment(person, "기사", "1순위", "2순위") == (
            "기사 A",
            "1순위 구역",
        )

    def test_blank_primary_falls_back_to_secondary(self):
        person = _person("기사 A", "  ", "2순위 구역", "주소")

        assert resolve_delivery_assignment(person, "기사", "1순위", "2순위") == (
            "기사 A",
            "2순위 구역",
        )

    def test_secondary_column_can_be_omitted(self):
        person = _person("기사 A", "1순위 구역", "", "주소")

        assert resolve_delivery_assignment(person, "기사", "1순위") == (
            "기사 A",
            "1순위 구역",
        )

    @pytest.mark.parametrize(
        ("driver", "primary", "secondary"),
        [("", "구역", "대체"), ("기사 A", "", "")],
    )
    def test_missing_driver_or_effective_region_is_unmatched(
        self, driver: str, primary: str, secondary: str
    ):
        person = _person(driver, primary, secondary, "주소")

        assert resolve_delivery_assignment(person, "기사", "1순위", "2순위") is None

    def test_routes_group_by_driver_and_keep_first_seen_region_order(self):
        people = [
            _person("기사 A", "A-1", "", "1"),
            _person("기사 B", "B-1", "", "2"),
            _person("기사 A", "A-2", "", "3"),
            _person("기사 A", "A-1", "", "4"),
        ]

        routes, keys = build_delivery_routes(people, "기사", "1순위", "2순위")

        assert [(route.driver, route.region) for route in routes] == [
            ("기사 A", "A-1"),
            ("기사 A", "A-2"),
            ("기사 B", "B-1"),
        ]
        assert keys[("기사 A", "A-1")] == "route_1"
        assert keys[("기사 A", "A-2")] == "route_2"


def test_prepare_people_from_roster_without_generating_invoice():
    original = [
        {"조식": 1, "석식": "0", "학기중 비고": "5/2조식중지", "성명": "A"},
        {"조식": 1, "석식": 1, "중지여부": "배송중지", "성명": "B"},
    ]

    prepared = prepare_daily_delivery_people(
        original,
        breakfast_col="조식",
        dinner_col="석식",
        note_col="학기중 비고",
        stop_col="중지여부",
    )

    assert len(prepared) == 1
    assert prepared[0]["성명"] == "A"
    assert prepared[0]["_breakfast"] == 1
    assert prepared[0]["_dinner"] == "0"
    assert prepared[0]["_note"] == "5/2조식중지"
    assert "_breakfast" not in original[0]


def test_prepare_people_ignores_comment_note_even_with_source_column():
    people = [{"비고": "  // 5/2조식중지"},
              {"비고": "문 앞에 놓아주세요", "_note": ""},
              {"비고": "5/2조식중지 // 메모"}]
    prepared = prepare_daily_delivery_people(people, note_col="비고")

    assert [person["_note"] for person in prepared] == [
        "", "문 앞에 놓아주세요", "5/2조식중지 // 메모"
    ]
    assert people[0]["비고"] == "  // 5/2조식중지"


@pytest.mark.parametrize(
    "value", ["Y", "y", "예", "있음", "O", 2, 0.5, True]
)
def test_daily_delivery_rejects_non_one_meal_markers(value):
    sheet = _build(
        [_person("기사 A", "1순위 구역", "", "주소", breakfast=value, dinner=0)]
    )

    assert sheet.rows == []
    assert (sheet.breakfast_total, sheet.dinner_total, sheet.lunch_total) == (
        0,
        0,
        0,
    )


@pytest.mark.parametrize("value", [1, 1.0, "1", " 1 ", "1.0"])
def test_daily_delivery_accepts_numeric_or_text_one(value):
    sheet = _build(
        [_person("기사 A", "1순위 구역", "", "주소", breakfast=value, dinner=0)]
    )

    assert sheet.breakfast_total == 1


class TestDailyAggregation:
    def test_sheet_can_be_built_with_primary_region_only(self):
        sheet = build_daily_delivery_sheet(
            [_person("기사 A", "1순위 구역", "", "주소")],
            date(2026, 5, 2),
            driver_col="기사",
            region_primary_col="1순위",
            address_col="주소",
        )

        assert [(row.route.driver, row.route.region) for row in sheet.rows] == [
            ("기사 A", "1순위 구역")
        ]

    def test_date_specific_pause_fallback_and_unmatched_row(self):
        people = [
            _person("기사 A", "1순위 구역", "무시할 구역", "A아파트 101동"),
            _person(
                "기사 A",
                "",
                "2순위 구역",
                "A아파트 101동",
                note="5/2석식일시중지",
            ),
            _person("", "미분류 구역", "", "새 주소"),
        ]

        sheet = _build(people)
        primary = next(row for row in sheet.rows if row.route.region == "1순위 구역")
        secondary = next(row for row in sheet.rows if row.route.region == "2순위 구역")
        unmatched = next(row for row in sheet.rows if row.route.key == "unmatched")

        assert (primary.breakfast, primary.dinner) == (1, 1)
        assert (secondary.breakfast, secondary.dinner) == (1, 0)
        assert "석식 제외" in secondary.note
        assert (unmatched.breakfast, unmatched.dinner) == (1, 1)
        assert "배송담당 공란 1명" in unmatched.note
        assert sheet.unmatched_count == 1
        assert sheet.breakfast_total == 3
        assert sheet.dinner_total == 2

    def test_duplicate_assignments_are_aggregated(self):
        people = [
            _person("기사 A", "같은 구역", "", "A"),
            _person("기사 A", "같은 구역", "", "B"),
        ]

        sheet = _build(people)

        assert len(sheet.rows) == 1
        assert (sheet.rows[0].breakfast, sheet.rows[0].dinner) == (2, 2)

    def test_lunch_uses_each_persons_calculated_delivery_dates(self):
        people = [
            _person(
                "기사 A",
                "같은 구역",
                "",
                "A",
                breakfast=0,
                dinner=0,
                lunch=1,
                lunch_dates=["2026-05-04"],
            )
        ]

        active = _build(people, date(2026, 5, 4))
        inactive = _build(people, date(2026, 5, 5))

        assert active.rows[0].lunch == 1
        assert active.lunch_total == 1
        assert inactive.rows == []
        assert inactive.lunch_total == 0

    def test_lunch_daily_dates_can_cross_the_invoice_month(self):
        person = _person(
            "기사 A",
            "같은 구역",
            "",
            "A",
            breakfast=0,
            dinner=0,
            lunch=0,
        )
        person["_daily_lunch_dates"] = ["2026-07-01"]

        active = _build([person], date(2026, 7, 1))
        inactive = _build([person], date(2026, 6, 30))

        assert active.lunch_total == 1
        assert inactive.lunch_total == 0

    def test_new_delivery_is_inactive_before_start(self):
        people = [_person("기사 A", "A 구역", "", "주소", note="5/3~신규배송")]

        before = _build(people, date(2026, 5, 2))
        active = _build(people, date(2026, 5, 3))

        assert before.rows == []
        assert (before.breakfast_total, before.dinner_total) == (0, 0)
        assert (active.rows[0].breakfast, active.rows[0].dinner) == (1, 1)

    def test_route_without_any_delivery_count_is_omitted(self):
        people = [
            _person("기사 A", "배송 구역", "", "주소 1"),
            _person(
                "기사 A",
                "중지 구역",
                "",
                "주소 2",
                note="5/2 일시중지",
            ),
        ]

        sheet = _build(people, date(2026, 5, 2))

        assert [
            (row.route.region, row.breakfast, row.dinner) for row in sheet.rows
        ] == [("배송 구역", 1, 1)]

    def test_household_distribution_uses_same_address(self):
        people = [
            {"주소": "A"},
            {"주소": "A"},
            {"주소": "B"},
            {"주소": "C"},
            {"주소": "C"},
            {"주소": "C"},
        ]
        assert format_household_distribution(people, "주소") == "3*1/2*1/1*1"

    def test_confirmation_memos_are_filtered_to_the_sheet_week(self):
        note = "석식: 6/13~6/15\n조식: 6/21~6/22\n일반 메모"

        assert weekly_confirmation_memo_entries(note, date(2026, 6, 12)) == [
            "석식: 6/13~6/15"
        ]
        assert weekly_confirmation_memo_entries(note, date(2026, 6, 15)) == [
            "석식: 6/13~6/15"
        ]
        assert weekly_confirmation_memo_entries(note, date(2026, 6, 22)) == [
            "조식: 6/21~6/22"
        ]

    def test_route_note_shows_address_household_size_and_current_week_memo(self):
        note = "석식: 6/13~6/15\n조식: 6/21~6/22"
        people = [
            _person("기사 A", "A 구역", "", "A아파트 101동", note=note),
            _person("기사 A", "A 구역", "", "A아파트 101동", note=note),
        ]

        sheet = _build(people, date(2026, 6, 12))

        assert sheet.rows[0].note == ("A아파트 101동(2명) 석식: 6/13~6/15")
        assert "6/21" not in sheet.rows[0].note


class TestDateRange:
    def test_skips_weekends_holidays_and_exclusions(self):
        result = select_delivery_dates(
            date(2026, 5, 1),
            date(2026, 5, 6),
            excluded_dates={date(2026, 5, 4)},
            holiday_provider=lambda _year: {date(2026, 5, 5)},
        )
        assert result == [date(2026, 5, 1), date(2026, 5, 6)]

    def test_can_include_every_date(self):
        result = select_delivery_dates(
            date(2026, 5, 1),
            date(2026, 5, 3),
            skip_non_business_days=False,
            excluded_dates={date(2026, 5, 2)},
            holiday_provider=lambda _year: {date(2026, 5, 1)},
        )
        assert result == [date(2026, 5, 1), date(2026, 5, 2), date(2026, 5, 3)]

    def test_rejects_reverse_range(self):
        with pytest.raises(ValueError, match="시작일"):
            select_delivery_dates(date(2026, 5, 2), date(2026, 5, 1))


def test_daily_delivery_template_is_single_sanitized_sheet():
    workbook = load_workbook(TEMPLATE_PATH, data_only=False)
    worksheet = workbook.active

    assert workbook.sheetnames == ["일일배송"]
    assert worksheet.max_row == 5
    assert worksheet["A4"].value == "분\n당\n구"
    assert all(worksheet.cell(4, column).value is None for column in range(2, 11))
    assert worksheet["A5"].value == "합계"
    assert all(worksheet.cell(5, column).value is None for column in range(4, 11))


def test_lunch_daily_delivery_template_is_a_separate_ready_to_use_file():
    workbook = load_workbook(LUNCH_TEMPLATE_PATH, data_only=False)
    worksheet = workbook.active

    assert workbook.sheetnames == ["중식일일배송"]
    assert worksheet.max_row == 5
    assert worksheet["D2"].value == "중식"
    assert worksheet["D3"].value == "중식"
    assert worksheet["F3"].value == "중식합계"
    assert worksheet["J3"].value == "중식갯수"
    merges = {str(value) for value in worksheet.merged_cells.ranges}
    assert {
        "D3:E3",
        "F3:H3",
        "D4:E4",
        "F4:H4",
        "D5:E5",
        "F5:H5",
    } <= merges


def test_make_daily_delivery_uses_template_and_resizes_rows(tmp_path):
    output = tmp_path / "daily.xlsx"
    people = [
        _person("기사 A", "A 구역", "", "A아파트 101동"),
        _person("기사 A", "", "B 구역", "B아파트 101동"),
    ]

    results = make_daily_delivery(
        people,
        [date(2026, 5, 4), date(2026, 5, 5)],
        str(output),
        template_path=str(TEMPLATE_PATH),
        lunch_template_path=str(LUNCH_TEMPLATE_PATH),
        driver_col="기사",
        region_primary_col="1순위",
        region_secondary_col="2순위",
        address_col="주소",
    )

    assert [item.delivery_date for item in results] == [
        date(2026, 5, 5),
        date(2026, 5, 4),
    ]
    workbook = load_workbook(output, data_only=False)
    assert workbook.sheetnames == ["26.05.05", "26.05.04"]
    worksheet = workbook["26.05.05"]
    assert worksheet["A1"].value.date() == date(2026, 5, 5)
    assert worksheet["C4"].value == "A 구역"
    assert worksheet["C5"].value == "B 구역"
    assert worksheet["D4"].value == 1
    assert worksheet["E4"].value == 1
    assert worksheet["F4"].value == "=SUM(D4:D5)"
    assert worksheet["A6"].value == "합계"
    assert worksheet["D6"].value == "=SUM(D4:D5)"
    merges = {str(value) for value in worksheet.merged_cells.ranges}
    assert "A1:I1" in merges
    assert "A4:A5" in merges
    assert "B4:B5" in merges
    assert worksheet.freeze_panes == "C4"
    assert worksheet.print_area == "'26.05.05'!$A$1:$I$6"
    template = load_workbook(TEMPLATE_PATH).active
    assert worksheet.page_setup.scale == template.page_setup.scale
    assert worksheet.page_setup.orientation == template.page_setup.orientation


def test_make_daily_delivery_rejects_all_zero_meal_columns(tmp_path):
    output = tmp_path / "daily.xlsx"
    people = [
        _person(
            "기사 A",
            "A 구역",
            "",
            "주소",
            breakfast=0,
            dinner=0,
        )
    ]

    with pytest.raises(ValueError, match="식종 컬럼"):
        make_daily_delivery(
            people,
            [date(2026, 5, 4)],
            str(output),
            template_path=str(TEMPLATE_PATH),
            driver_col="기사",
            region_primary_col="1순위",
            address_col="주소",
        )

    assert not output.exists()


def test_make_daily_delivery_creates_lunch_sheet_with_merged_meal_columns(
    tmp_path,
):
    output = tmp_path / "daily-lunch.xlsx"
    people = [
        _person(
            "기사 A",
            "A 구역",
            "",
            "A아파트 101동",
            breakfast=0,
            dinner=0,
            lunch=1,
            lunch_dates=["2026-05-04"],
        )
    ]

    results = make_daily_delivery(
        people,
        [date(2026, 5, 4), date(2026, 5, 5)],
        str(output),
        template_path=str(TEMPLATE_PATH),
        lunch_template_path=str(LUNCH_TEMPLATE_PATH),
        driver_col="기사",
        region_primary_col="1순위",
        address_col="주소",
    )

    assert [item.delivery_date for item in results] == [date(2026, 5, 4)]
    workbook = load_workbook(output, data_only=False)
    assert workbook.sheetnames == ["중식_26.05.04"]
    worksheet = workbook.active
    assert worksheet["D2"].value == "중식"
    assert worksheet["D3"].value == "중식"
    assert worksheet["F3"].value == "중식합계"
    assert worksheet["D4"].value == 1
    assert worksheet["F4"].value == "=SUM(D4:D4)"
    merges = {str(value) for value in worksheet.merged_cells.ranges}
    assert {"D3:E3", "F3:H3", "D4:E4", "F4:H4"} <= merges


def test_lunch_generation_requires_the_separate_lunch_template(tmp_path):
    output = tmp_path / "daily-lunch.xlsx"
    people = [
        _person(
            "기사 A",
            "A 구역",
            "",
            "주소",
            breakfast=0,
            dinner=0,
            lunch=1,
            lunch_dates=["2026-05-04"],
        )
    ]

    with pytest.raises(ValueError, match="중식 일일배송 템플릿"):
        make_daily_delivery(
            people,
            [date(2026, 5, 4)],
            str(output),
            template_path=str(TEMPLATE_PATH),
            lunch_template_path=str(tmp_path / "missing.xlsx"),
            driver_col="기사",
            region_primary_col="1순위",
            address_col="주소",
        )

    assert not output.exists()


def test_term_and_lunch_sheets_each_omit_zero_delivery_rows(tmp_path):
    output = tmp_path / "daily-combined.xlsx"
    people = [
        _person("기사 A", "학기중 구역", "", "주소 1"),
        _person(
            "기사 A",
            "중식 구역",
            "",
            "주소 2",
            breakfast=0,
            dinner=0,
            lunch=1,
            lunch_dates=["2026-05-04"],
        ),
    ]

    results = make_daily_delivery(
        people,
        [date(2026, 5, 4)],
        str(output),
        template_path=str(TEMPLATE_PATH),
        lunch_template_path=str(LUNCH_TEMPLATE_PATH),
        driver_col="기사",
        region_primary_col="1순위",
        address_col="주소",
    )

    assert results[0].lunch_total == 1
    workbook = load_workbook(output, data_only=False)
    assert workbook.sheetnames == ["26.05.04", "중식_26.05.04"]
    assert workbook["26.05.04"]["C4"].value == "학기중 구역"
    assert workbook["26.05.04"]["C5"].value is None
    assert workbook["중식_26.05.04"]["C4"].value == "중식 구역"
    assert workbook["중식_26.05.04"]["C5"].value is None


def test_make_daily_delivery_renders_totals_only_when_date_has_no_deliveries(
    tmp_path,
):
    output = tmp_path / "daily-empty.xlsx"
    people = [
        _person(
            "기사 A",
            "A 구역",
            "",
            "주소",
            note="5/4 일시중지",
        )
    ]

    results = make_daily_delivery(
        people,
        [date(2026, 5, 4)],
        str(output),
        template_path=str(TEMPLATE_PATH),
        driver_col="기사",
        region_primary_col="1순위",
        address_col="주소",
    )

    assert results[0].rows == []
    workbook = load_workbook(output, data_only=False)
    worksheet = workbook["26.05.04"]
    assert worksheet.max_row == 4
    assert worksheet["A4"].value == "합계"
    assert [worksheet.cell(4, column).value for column in (4, 5, 6, 8)] == [
        0,
        0,
        0,
        0,
    ]
    assert worksheet.print_area == "'26.05.04'!$A$1:$I$4"
