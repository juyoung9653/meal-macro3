"""확인서 대상자 병합 및 템플릿 생성 테스트."""

from pathlib import Path

import openpyxl
import pytest

from src.generators.confirm_generator import _merge_confirm_people, make_confirm
from src.templates.template_scanner import scan_placeholders


@pytest.mark.parametrize(
    ("col_mapping", "expected_dong"),
    [({}, "정자동"), ({"관할동": "행정동"}, "판교동")],
)
def test_confirm_uses_jurisdiction_dong_and_omits_driver_phone(
    tmp_path, monkeypatch, col_mapping, expected_dong
):
    template = Path(__file__).resolve().parents[1] / "assets" / "확인서템플릿.xlsx"
    from src.generators import confirm_generator

    settings = confirm_generator.get_settings()

    class SettingsWithoutCache:
        def get(self, *args, **kwargs):
            if args == ("cache", "placeholder_cache"):
                return ""
            return settings.get(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(settings, name)

    monkeypatch.setattr(confirm_generator, "get_settings", lambda: SettingsWithoutCache())
    people = [{
        "성명": "홍길동", "생년월일": "2010-01-02", "세대주성명": "보호자",
        "핸드폰": "010-1111-2222", "주소": "분당구 테스트로 1", "동": "서현동",
        "관할동": "정자동", "행정동": "판교동", "_note": "",
    }]
    if col_mapping:
        del people[0]["관할동"]

    created = make_confirm(
        people, col_mapping, str(tmp_path), str(template), 2026, 9,
        delivery_person="배송기사", delivery_phone="010-9999-8888",
    )

    wb = openpyxl.load_workbook(tmp_path / created[0]["file"])
    ws = wb[created[0]["sheet_name"]]
    placeholders = scan_placeholders(str(template), ["1명"], cache_path=None)["1명"]
    assert "dong" in placeholders
    assert "d_phone" not in placeholders
    assert ws["C3"].value == expected_dong
    assert ws["K3"].value == "배송기사"
    phone_row, phone_col = placeholders["phone1"][0]
    assert ws.cell(phone_row, phone_col).value == "010-1111-2222"
    assert not any(
        "010-9999-8888" in str(cell.value)
        for row in ws for cell in row if cell.value is not None
    )


def test_confirm_merges_only_matching_name_and_birthdate():
    people = [
        {
            "성명": "홍 길동",
            "생년월일": "2010-01-02",
            "_breakfast": 1,
            "_dinner": 0,
            "_lunch": 0,
            "_bf_cnt": 4,
            "_dn_cnt": 0,
            "_lh_cnt": 0,
            "_lunch_dates": [],
        },
        {
            "성명": "홍길동",
            "생년월일": "2010.01.02",
            "_breakfast": 0,
            "_dinner": 0,
            "_lunch": 1,
            "_bf_cnt": 0,
            "_dn_cnt": 0,
            "_lh_cnt": 5,
            "_lunch_dates": ["2025-06-02"],
        },
        {
            "성명": "홍길동",
            "생년월일": "2011-01-02",
            "_breakfast": 0,
            "_dinner": 1,
            "_lunch": 0,
            "_bf_cnt": 0,
            "_dn_cnt": 4,
            "_lh_cnt": 0,
            "_lunch_dates": [],
        },
    ]

    merged = _merge_confirm_people(people, {})

    assert len(merged) == 2
    assert merged[0]["_breakfast"] == 1
    assert merged[0]["_lunch"] == 1
    assert merged[0]["_bf_cnt"] == 4
    assert merged[0]["_lh_cnt"] == 5
    assert merged[0]["_lunch_dates"] == ["2025-06-02"]


def test_confirm_does_not_merge_when_birthdate_is_missing():
    people = [
        {"성명": "홍길동", "생년월일": "", "_breakfast": 1},
        {"성명": "홍길동", "생년월일": "", "_lunch": 1},
    ]

    assert len(_merge_confirm_people(people, {})) == 2


@pytest.mark.parametrize(
    "alternate_birthdate",
    ["100102", "100102-3******", "100102-3123456"],
)
def test_confirm_merges_common_birthdate_formats(alternate_birthdate):
    people = [
        {"성명": "홍길동", "생년월일": "2010-01-02", "_breakfast": 1},
        {"성명": "홍 길동", "생년월일": alternate_birthdate, "_lunch": 1},
    ]

    merged = _merge_confirm_people(people, {})

    assert len(merged) == 1
    assert merged[0]["_breakfast"] == 1
    assert merged[0]["_lunch"] == 1
