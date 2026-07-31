"""청구서와 확인서 사이에서 사용하는 enriched 인원 레코드 builder."""

from collections.abc import Mapping, Sequence
from typing import Any, cast

from .delivery_matrix import ALLOWED_MEALS
from .invoice_data import EnrichedPersonDict, WeeklyMealCount

FULL_ADDRESS_FIELD = "전체주소"


def combine_address(road_address: object, detail_address: object) -> str:
    """도로명주소와 상세주소를 공백 하나로 연결한 표시 주소를 만든다."""
    parts: list[str] = []
    for value in (road_address, detail_address):
        if value is None:
            continue
        text = " ".join(str(value).split())
        if text:
            parts.append(text)
    return " ".join(parts)


def build_enriched_person_dict(
    raw: Mapping[str, Any],
    *,
    note: str,
    meal_counts: Mapping[str, int],
    weekly_meal_counts: Sequence[WeeklyMealCount],
    delivery_staff: str,
    delivery_sequence: str,
    unit_prices: Mapping[str, int],
) -> EnrichedPersonDict:
    """명단 원본과 계산 완료 값을 현재 enriched 계약으로 조립한다."""
    breakfast_count = meal_counts.get("조식", 0)
    lunch_count = meal_counts.get("중식", 0)
    dinner_count = meal_counts.get("석식", 0)
    total = breakfast_count + lunch_count + dinner_count
    amount = sum(meal_counts.get(meal, 0) * unit_prices[meal] for meal in ALLOWED_MEALS)
    weekly_copy = [cast(WeeklyMealCount, dict(item)) for item in weekly_meal_counts]

    enriched = dict(raw)
    if "도로명주소" in raw or "상세주소" in raw:
        enriched[FULL_ADDRESS_FIELD] = combine_address(
            raw.get("도로명주소"),
            raw.get("상세주소"),
        )
    enriched.update(
        {
            "_note": note,
            "_bf_cnt": breakfast_count,
            "_lh_cnt": lunch_count,
            "_dn_cnt": dinner_count,
            "_total": total,
            "_amount": f"{amount:,}원",
            "weekly_meal_counts": weekly_copy,
            "배송담당": delivery_staff,
            "배송순서": delivery_sequence,
            "연번": delivery_sequence,
        }
    )
    return cast(EnrichedPersonDict, enriched)
