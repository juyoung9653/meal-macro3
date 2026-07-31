"""청구/확인서 전달 모델과 enriched builder 테스트."""

from dataclasses import fields

from src.core.invoice_data import InvoiceData, WeeklyMealCount
from src.core.models import FULL_ADDRESS_FIELD, build_enriched_person_dict

LEGACY_FIELDS = {
    "_breakfast",
    "_dinner",
    "_lunch",
    "_delivery_start",
    "_delivery_end",
    "_daily",
    "_delivery_rows_raw",
    "_reception_memo",
    "excluded_dates",
}


def _weekly_counts() -> list[WeeklyMealCount]:
    return [
        {
            "week": 1,
            "start_date": "20260601",
            "end_date": "20260606",
            "조식": 2,
            "중식": 1,
            "석식": 0,
        }
    ]


class TestBuildEnrichedPersonDict:
    def test_builds_only_current_enriched_contract(self):
        weekly = _weekly_counts()
        enriched = build_enriched_person_dict(
            raw={
                "성명": "홍길동",
                "도로명주소": "성남대로 1",
                "상세주소": "101호",
                "비고": "원본",
            },
            note="원본",
            meal_counts={"조식": 2, "중식": 1, "석식": 3},
            weekly_meal_counts=weekly,
            delivery_staff="기사A",
            delivery_sequence="003",
            unit_prices={"조식": 10_000, "중식": 9_000, "석식": 8_000},
        )

        assert enriched.get("성명") == "홍길동"
        assert enriched.get(FULL_ADDRESS_FIELD) == "성남대로 1 101호"
        assert enriched["_note"] == "원본"
        assert (enriched["_bf_cnt"], enriched["_lh_cnt"], enriched["_dn_cnt"]) == (
            2,
            1,
            3,
        )
        assert enriched["_total"] == 6
        assert enriched["_amount"] == "53,000원"
        assert enriched["weekly_meal_counts"] == weekly
        assert enriched["weekly_meal_counts"] is not weekly
        assert enriched["배송담당"] == "기사A"
        assert enriched["배송순서"] == "003"
        assert enriched["연번"] == "003"
        assert LEGACY_FIELDS.isdisjoint(enriched)

    def test_does_not_mutate_raw_or_weekly_inputs(self):
        raw = {"성명": "홍길동"}
        weekly = _weekly_counts()

        enriched = build_enriched_person_dict(
            raw=raw,
            note="",
            meal_counts={"조식": 0, "중식": 0, "석식": 0},
            weekly_meal_counts=weekly,
            delivery_staff="",
            delivery_sequence="",
            unit_prices={"조식": 9500, "중식": 9500, "석식": 9500},
        )
        enriched["weekly_meal_counts"][0]["조식"] = 99

        assert raw == {"성명": "홍길동"}
        assert weekly[0]["조식"] == 2


def test_invoice_data_contains_only_current_cross_tab_state():
    assert {item.name for item in fields(InvoiceData)} == {
        "invoices",
        "col_mapping",
        "year",
        "month",
    }
