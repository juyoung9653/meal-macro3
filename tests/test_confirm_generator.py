"""확인서 대상자 동일인 병합 테스트."""

from src.generators.confirm_generator import _merge_confirm_people


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
