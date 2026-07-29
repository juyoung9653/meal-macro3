from dataclasses import dataclass, field
from typing import Any, NotRequired, TypedDict


class WeeklyMealCount(TypedDict):
    """확인서에 바로 전달하는 한 주의 식종별 배송 건수."""

    week: int
    start_date: str  # YYYYMMDD
    end_date: str  # YYYYMMDD
    조식: int
    중식: int
    석식: int


class EnrichedPersonDict(TypedDict):
    """청구서 생성 후 확인서 탭으로 전달되는 인원 레코드."""

    _note: str
    _total: int
    _amount: str
    _bf_cnt: int
    _dn_cnt: int
    _lh_cnt: int
    weekly_meal_counts: list[WeeklyMealCount]
    배송순서: str
    배송담당: str
    연번: str

    # 명단 원본 필드는 Excel 헤더에 따라 동적으로 함께 존재한다.
    비고: NotRequired[Any]
    핸드폰: NotRequired[Any]


@dataclass
class InvoiceData:
    """청구서 탭에서 확인서 탭으로 넘기는 현재 상태 묶음."""

    invoices: list[EnrichedPersonDict] = field(default_factory=list)
    col_mapping: dict = field(default_factory=dict)
    year: int = 0
    month: int = 0
