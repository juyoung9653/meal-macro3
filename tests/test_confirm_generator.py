"""준비된 주차 데이터를 사용하는 확인서 생성기 테스트."""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import date
from pathlib import Path

import openpyxl
import pytest

from src.core.delivery_matrix import RosterEntry
from src.core.invoice_data import (
    MealPrices,
    MonthlyDelivery,
    MonthlyDeliveryBatch,
    WeeklyDelivery,
)
from src.generators.confirm_generator import make_confirm_publication
from src.templates.template_scanner import scan_placeholders


def _weekly_counts(
    *, breakfast: int = 0, lunch: int = 0, dinner: int = 0
) -> tuple[WeeklyDelivery, ...]:
    ranges = [(1, 6), (7, 13), (14, 20), (21, 27), (28, 30)]
    return tuple(
        WeeklyDelivery(
            week,
            date(2026, 6, start),
            date(2026, 6, end),
            breakfast if week == 1 else 0,
            lunch if week == 1 else 0,
            dinner if week == 1 else 0,
        )
        for week, (start, end) in enumerate(ranges, start=1)
    )


def _roster_person(
    name: str,
    address: object,
    *,
    detail_address: object = "",
    dong: object = "정자동",
    source_row: int = 2,
) -> MonthlyDelivery:
    return MonthlyDelivery(
        roster=RosterEntry(
            source_row,
            {
                "성명": name,
                "세대주 성명": f"{name} 보호자",
                "핸드폰": "010-1111-1111",
                "관할동": dong,
                "도로명주소": address,
                "상세주소": detail_address,
            },
        ),
        note="",
        delivery_staff="기사A",
        delivery_sequence="",
        breakfast_count=0,
        lunch_count=1,
        dinner_count=0,
        weekly=_weekly_counts(lunch=1),
    )


def _typed_delivery(name: str, household_key: str, address: str) -> MonthlyDelivery:
    return MonthlyDelivery(
        RosterEntry(
            2 if name == "첫째" else 3,
            {
                "성명": name,
                "세대주 성명": "홍보호",
                "핸드폰": "010-1111-1111",
                "관할동": "정자동",
                "도로명주소": address,
                "상세주소": "101호",
            },
        ),
        "",
        "기사A",
        "001",
        0,
        1,
        0,
        (WeeklyDelivery(1, date(2026, 6, 1), date(2026, 6, 6), 0, 1, 0),),
        billing_amount=10_000,
        household_key=household_key,
    )


def test_typed_db_household_ids_do_not_merge_same_address(
    tmp_path: Path,
    confirm_template: str,
):
    batch = MonthlyDeliveryBatch(
        2026,
        6,
        MealPrices(10_000, 10_000, 10_000),
        False,
        (
            _typed_delivery("첫째", "household-1", "성남대로 1"),
            _typed_delivery("둘째", "household-2", "성남대로 1"),
        ),
    )

    created, publication = make_confirm_publication(
        batch,
        {},
        tmp_path / "stable-households.xlsx",
        confirm_template,
        meal_type="급식",
    )

    assert publication.output_path == tmp_path / "stable-households.xlsx"
    assert created[0]["total_hh"] == 2
    assert created[0]["count_1"] == 2
    assert created[0]["count_2"] == 0


def test_typed_same_household_requires_one_consistent_address(tmp_path: Path):
    batch = MonthlyDeliveryBatch(
        2026,
        6,
        MealPrices(10_000, 10_000, 10_000),
        False,
        (
            _typed_delivery("첫째", "household-1", "성남대로 1"),
            _typed_delivery("둘째", "household-1", "성남대로 2"),
        ),
    )

    with pytest.raises(ValueError, match="같은 가구의 출력값") as error:
        make_confirm_publication(
            batch,
            {},
            tmp_path / "invalid-household.xlsx",
            "사용되지않는.xlsx",
            meal_type="급식",
        )

    message = str(error.value)
    for value in ("첫째", "둘째", "성남대로 1", "성남대로 2", "다른 항목: 도로명주소"):
        assert value in message
    assert "household-1" not in message
    assert "명단 2행" not in message


@pytest.fixture
def confirm_template() -> str:
    path = "assets/급식/급식_확인서템플릿.xlsx"
    if not os.path.exists(path):
        pytest.skip("급식 확인서 템플릿 없음")
    return path


def test_renders_existing_template_from_prepared_weekly_data_without_reader_calls(
    tmp_path: Path, confirm_template: str
):
    people = [
        MonthlyDelivery(
            roster=RosterEntry(
                2,
                {
                    "성명": "홍길동",
                    "세대주 성명": "홍보호",
                    "핸드폰": "010-1111-1111",
                    "관할동": "정자동",
                    "도로명주소": "성남시 분당구 같은집",
                },
            ),
            note="견과류 알레르기",
            delivery_staff="기사A",
            delivery_sequence="",
            breakfast_count=1,
            lunch_count=2,
            dinner_count=0,
            weekly=_weekly_counts(breakfast=1, lunch=2),
        ),
        MonthlyDelivery(
            roster=RosterEntry(
                3,
                {
                    "성명": "김영희",
                    "세대주 성명": "김보호",
                    "핸드폰": "010-2222-2222",
                    "관할동": "정자동",
                    "도로명주소": "성남시 분당구 같은집",
                },
            ),
            note="",
            delivery_staff="기사B",
            delivery_sequence="",
            breakfast_count=0,
            lunch_count=0,
            dinner_count=3,
            weekly=_weekly_counts(dinner=3),
        ),
    ]

    created, _publication = make_confirm_publication(
        batch=MonthlyDeliveryBatch(
            2026, 6, MealPrices(9500, 9500, 9500), False, tuple(people)
        ),
        col_mapping={},
        output_path=tmp_path / "확인서_202606.xlsx",
        template_path=confirm_template,
        meal_type="급식",
    )

    assert created == [
        {
            "sheet_name": "정자동",
            "total_hh": 1,
            "count_1": 0,
            "count_2": 1,
            "count_3": 0,
            "count_4": 0,
            "count_5": 0,
            "file": "확인서_202606.xlsx",
        }
    ]

    output = tmp_path / "확인서_202606.xlsx"
    wb = openpyxl.load_workbook(output, data_only=False)
    try:
        assert wb.sheetnames == ["정자동"]
        ws = wb["정자동"]
        assert ws["K3"].value == "기사A / 기사B"
        assert ws["G6"].value == 1
        assert ws["H6"].value is None
        assert ws["I6"].value == 2
        assert ws["G11"].value is None
        assert ws["H11"].value == 3
        assert ws["I11"].value is None
        placeholders = scan_placeholders(confirm_template, ["2명"])["2명"]
        carer1 = placeholders["carer1"][0]
        carer2 = placeholders["carer2"][0]
        assert ws.cell(*carer1).value == "홍보호"
        assert ws.cell(*carer2).value == "김보호"
    finally:
        wb.close()


def test_same_road_with_different_detail_addresses_are_separate_households(
    tmp_path: Path,
    confirm_template: str,
):
    people = [
        _roster_person("101호 대상", "성남대로 1", detail_address="101호"),
        _roster_person("102호 대상", "성남대로 1", detail_address="102호"),
    ]

    created, _publication = make_confirm_publication(
        batch=MonthlyDeliveryBatch(
            2026, 6, MealPrices(9500, 9500, 9500), False, tuple(people)
        ),
        col_mapping={},
        output_path=tmp_path / "확인서_202606.xlsx",
        template_path=confirm_template,
        meal_type="급식",
    )

    assert created[0]["total_hh"] == 2
    assert created[0]["count_1"] == 2
    assert created[0]["count_2"] == 0
    workbook = openpyxl.load_workbook(tmp_path / created[0]["file"])
    try:
        worksheet = workbook[created[0]["sheet_name"]]
        values = {
            cell.value
            for row in worksheet.iter_rows()
            for cell in row
            if isinstance(cell.value, str)
        }
        assert "성남대로 1" in values
        assert not {"성남대로 1 101호", "성남대로 1 102호"} & values
    finally:
        workbook.close()


def test_none_roster_values_render_as_blanks_with_safe_sheet_fallback(
    tmp_path: Path, confirm_template: str
):
    person = MonthlyDelivery(
        roster=RosterEntry(
            2,
            {
                "성명": None,
                "세대주 성명": None,
                "핸드폰": None,
                "관할동": None,
                "도로명주소": "성남시 분당구 유효주소",
            },
        ),
        note="",
        delivery_staff="",
        delivery_sequence="",
        breakfast_count=0,
        lunch_count=1,
        dinner_count=0,
        weekly=_weekly_counts(lunch=1),
    )

    created, _publication = make_confirm_publication(
        batch=MonthlyDeliveryBatch(
            2026, 6, MealPrices(9500, 9500, 9500), False, (person,)
        ),
        col_mapping={},
        output_path=tmp_path / "확인서_202606.xlsx",
        template_path=confirm_template,
        meal_type="급식",
    )

    assert created[0]["sheet_name"] == "기타"
    wb = openpyxl.load_workbook(tmp_path / created[0]["file"], data_only=False)
    try:
        ws = wb["기타"]
        assert ws["F3"].value == "성남시 분당구 유효주소"
        for coordinate in ("C3", "K3", "B6", "C6", "D6"):
            assert ws[coordinate].value is None
        assert all(
            "None" not in cell.value
            for row in ws.iter_rows()
            for cell in row
            if isinstance(cell.value, str)
        )
    finally:
        wb.close()


def test_rejects_single_blank_mapped_address_with_dong_and_name(tmp_path: Path):
    person = MonthlyDelivery(
        roster=RosterEntry(
            2, {"이름": "주소없는대상자", "동": "정자동", "주소": " \r\n "}
        ),
        note="",
        delivery_staff="",
        delivery_sequence="",
        breakfast_count=0,
        lunch_count=1,
        dinner_count=0,
        weekly=(),
    )

    with pytest.raises(ValueError) as exc_info:
        make_confirm_publication(
            batch=MonthlyDeliveryBatch(
                2026, 6, MealPrices(9500, 9500, 9500), False, (person,)
            ),
            col_mapping={"도로명주소": "주소", "성명": "이름", "관할동": "동"},
            output_path=tmp_path / "확인서_202606.xlsx",
            template_path="사용되지않는_확인서템플릿.xlsx",
            meal_type="급식",
        )

    message = str(exc_info.value)
    assert "빈 주소" in message
    assert "정자동" in message
    assert "성명=주소없는대상자, 명단 2행" in message
    assert list(tmp_path.iterdir()) == []


def test_rejects_multiple_blank_addresses_with_names_and_roster_positions(
    tmp_path: Path,
):
    names = ["첫번째", "두번째"]
    people = [
        _roster_person(names[0], None),
        _roster_person(names[1], " \n ", source_row=3),
    ]

    with pytest.raises(ValueError) as exc_info:
        make_confirm_publication(
            batch=MonthlyDeliveryBatch(
                2026, 6, MealPrices(9500, 9500, 9500), False, tuple(people)
            ),
            col_mapping={},
            output_path=tmp_path / "확인서_202606.xlsx",
            template_path="사용되지않는_확인서템플릿.xlsx",
            meal_type="급식",
        )

    message = str(exc_info.value)
    assert "빈 주소" in message
    assert "정자동" in message
    for source_row, name in enumerate(names, start=2):
        assert f"성명={name}, 명단 {source_row}행" in message
    assert list(tmp_path.iterdir()) == []


def test_rejects_six_person_household_with_names_and_roster_positions(
    tmp_path: Path,
):
    address = "성남시 분당구 여섯명가구"
    names = [f"대상자{i}" for i in range(1, 7)]
    people = [
        _roster_person(name, address, source_row=index)
        for index, name in enumerate(names, start=2)
    ]

    with pytest.raises(ValueError) as exc_info:
        make_confirm_publication(
            batch=MonthlyDeliveryBatch(
                2026, 6, MealPrices(9500, 9500, 9500), False, tuple(people)
            ),
            col_mapping={},
            output_path=tmp_path / "확인서_202606.xlsx",
            template_path="사용되지않는_확인서템플릿.xlsx",
            meal_type="급식",
        )

    message = str(exc_info.value)
    assert address in message
    assert "6명" in message
    for source_row, name in enumerate(names, start=2):
        assert f"성명={name}, 명단 {source_row}행" in message
    assert list(tmp_path.iterdir()) == []


def test_renders_five_person_household_without_omission(
    tmp_path: Path, confirm_template: str
):
    address = "성남시 분당구 다섯명가구"
    names = [f"대상자{i}" for i in range(1, 6)]
    people = [
        _roster_person(name, address, source_row=index)
        for index, name in enumerate(names, start=2)
    ]

    created, _publication = make_confirm_publication(
        batch=MonthlyDeliveryBatch(
            2026, 6, MealPrices(9500, 9500, 9500), False, tuple(people)
        ),
        col_mapping={},
        output_path=tmp_path / "확인서_202606.xlsx",
        template_path=confirm_template,
        meal_type="급식",
    )

    assert created[0]["total_hh"] == 1
    assert created[0]["count_5"] == 1
    placeholders = scan_placeholders(confirm_template, ["5명"])["5명"]
    wb = openpyxl.load_workbook(tmp_path / created[0]["file"], data_only=False)
    try:
        ws = wb[created[0]["sheet_name"]]
        for index, name in enumerate(names, start=1):
            row, column = placeholders[f"child{index}"][0]
            assert ws.cell(row=row, column=column).value == name
    finally:
        wb.close()


@pytest.mark.parametrize("phone_token", [None, "{d_phone}", "{phone1}"])
def test_confirmation_can_omit_phone_output(tmp_path: Path, phone_token):
    # Users can remove phone placeholders or explicitly leave them unmapped.
    template = tmp_path / "confirmation.xlsx"
    workbook = openpyxl.Workbook()
    workbook.remove(workbook.active)
    for size in range(1, 6):
        sheet = workbook.create_sheet(f"{size}명")
        sheet["A1"] = "{title}"
        sheet["A2"] = "{child1}"
        sheet["A3"] = "{delivery}"
        if phone_token is not None:
            sheet["B2"] = phone_token
    workbook.save(template)
    workbook.close()

    person = _typed_delivery("합성 이름", "household", "합성 주소")
    output = tmp_path / "confirmation-output.xlsx"
    created, _ = make_confirm_publication(
        batch=MonthlyDeliveryBatch(
            2026, 6, MealPrices(9500, 9500, 9500), False, (person,)
        ),
        col_mapping={"{phone1}": ""},
        output_path=output,
        template_path=str(template),
        meal_type="행복도시락",
    )

    workbook = openpyxl.load_workbook(output)
    try:
        sheet = workbook[created[0]["sheet_name"]]
        assert sheet["A2"].value == "합성 이름"
        assert sheet["A3"].value == "기사A"
        assert sheet["B2"].value is None
        assert all(
            cell.value != person.roster.extra["핸드폰"]
            for row in sheet for cell in row
        )
    finally:
        workbook.close()


@pytest.mark.parametrize("profile", ["급식", "행복도시락"])
@pytest.mark.parametrize("primary", ["선택 주소", "  ", None, 0])
def test_confirmation_maps_address_and_dong_with_fallbacks(tmp_path, profile, primary):
    template = f"assets/{profile}/{profile}_확인서템플릿.xlsx"
    people = []
    for index, name in enumerate(("첫째", "둘째")):
        person = _typed_delivery(name, "same-household", f"원본 주소 {index}")
        values = dict(person.roster.extra, **{
            "상세주소": f"{index + 1}01호", "custom:address": primary,
            "custom:backup_address": "대체 주소", "custom:dong": " ",
            "custom:backup_dong": "선택동",
        })
        people.append(replace(person, roster=replace(person.roster, extra=values)))
    output = tmp_path / "mapped.xlsx"
    created, _ = make_confirm_publication(
        MonthlyDeliveryBatch(2026, 6, MealPrices(10000, 10000, 10000), False, tuple(people)),
        {"도로명주소": "custom:address", "관할동": "custom:dong"}, output, template,
        meal_type=profile,
        column_fallbacks={"도로명주소": "custom:backup_address", "관할동": "custom:backup_dong"},
    )
    assert created[0]["count_2"] == 1
    placeholders = scan_placeholders(template, ["2명"])["2명"]
    wb = openpyxl.load_workbook(output)
    try:
        assert wb.sheetnames == ["선택동"]
        sheet = wb["선택동"]
        expected = "대체 주소" if primary is None or primary == "  " else str(primary)
        assert sheet.cell(*placeholders["addr"][0]).value == expected
        if profile == "급식":
            assert sheet.cell(*placeholders["dong"][0]).value == "선택동"
    finally:
        wb.close()


def test_confirmation_empty_output_mapping_keeps_db_household(tmp_path, confirm_template):
    people = tuple(_typed_delivery(name, "same-household", "원본 주소") for name in ("첫째", "둘째"))
    output = tmp_path / "blank.xlsx"
    created, _ = make_confirm_publication(
        MonthlyDeliveryBatch(2026, 6, MealPrices(10000, 10000, 10000), False, people),
        {"도로명주소": "", "관할동": ""}, output, confirm_template,
    )
    assert created[0]["count_2"] == 1
    wb = openpyxl.load_workbook(output)
    try:
        assert wb.sheetnames == ["기타"]
        placeholders = scan_placeholders(confirm_template, ["2명"])["2명"]
        for field in ("addr", "dong"):
            assert wb["기타"].cell(*placeholders[field][0]).value is None
    finally:
        wb.close()


def test_confirmation_conflict_shows_actual_identity_and_mapped_values(tmp_path):
    people = []
    for index, name in enumerate(("첫째", "둘째")):
        person = _typed_delivery(name, "internal-household-id", "원본 주소")
        values = dict(person.roster.extra, **{
            "생년월일": f"2015-01-0{index + 1}", "학교명": "가상초",
            "custom:dong": f"선택{index}동", "custom:name": "출력용 별칭",
        })
        people.append(replace(person, roster=replace(person.roster, extra=values)))
    with pytest.raises(ValueError) as error:
        make_confirm_publication(
            MonthlyDeliveryBatch(2026, 6, MealPrices(10000, 10000, 10000), False, tuple(people)),
            {"성명": "custom:name", "관할동": "custom:dong"}, tmp_path / "bad.xlsx", "unused.xlsx",
        )
    message = str(error.value)
    for value in ("첫째", "둘째", "2015-01-01", "2015-01-02", "가상초", "선택0동", "선택1동", "다른 항목: 관할동"):
        assert value in message
    assert "internal-household-id" not in message
    assert "출력용 별칭" not in message
    assert not list(tmp_path.iterdir())


def test_confirmation_external_text_stays_literal(tmp_path, confirm_template):
    person = _roster_person("=1+1", "#N/A")
    batch = MonthlyDeliveryBatch(2026, 6, MealPrices(9500, 9500, 9500), False, (person,))
    output = tmp_path / "literal-confirm.xlsx"
    make_confirm_publication(batch, {}, output, confirm_template, meal_type="급식")
    workbook = openpyxl.load_workbook(output)
    try:
        cells = [cell for sheet in workbook for row in sheet for cell in row
                 if cell.value in ("=1+1", "#N/A")]
        assert {cell.value for cell in cells} == {"=1+1", "#N/A"}
        assert all(cell.data_type == "s" for cell in cells)
        assert workbook.active["I6"].value == 1
    finally:
        workbook.close()
