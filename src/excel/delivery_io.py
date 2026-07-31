"""선택한 연간 배송일을 출력용 표로 투영한다."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

import pandas as pd

from src.core.delivery_matrix import (
    ALLOWED_MEALS,
    AnnualDeliveryBook,
    DayState,
    DeliveryRow,
)

_ROSTER_ALIASES: Mapping[str, tuple[str, ...]] = {
    "이름": ("성명", "이름"),
    "관할동": ("관할동",),
    "상세주소": ("상세주소", "주소"),
    "신주소": ("도로명주소", "신주소"),
    "핸드폰": ("핸드폰",),
    "비고2": ("비고", "비고2"),
}


@dataclass(frozen=True)
class Choice:
    meal: str
    ship: str
    day: date
    cols: tuple[str, ...] | list[str]


def available_delivery_dates(book: AnnualDeliveryBook) -> list[date]:
    """입력 흔적(0 또는 1)이 있는 날짜를 실제 날짜순으로 반환한다."""
    return [day for day in book.dates if book.day_state(day) is DayState.ENTERED]


def available_meals(book: AnnualDeliveryBook, day: date) -> list[str]:
    """해당 날짜에 실제 배송(1)이 있는 식종만 표준 식종 순서로 반환한다."""
    delivered_meals = {row.meal for row in book.delivered_rows_on(day)}
    return [meal for meal in ALLOWED_MEALS if meal in delivered_meals]


def available_staff(book: AnnualDeliveryBook, day: date, meal: str) -> list[str]:
    """해당 날짜와 식종의 실제 배송(1)을 담당하는 유효 담당자를 반환한다."""
    staff = {
        row.staff
        for row in book.delivered_rows_on(day, meal=meal)
        if not _is_blank(row.staff)
    }
    return sorted(staff)


def make_result(book: AnnualDeliveryBook, ch: Choice) -> pd.DataFrame:
    """선택 날짜·식종·담당자의 실제 배송행을 요청 컬럼대로 투영한다."""
    _validate_choice(book, ch)
    columns = list(ch.cols)
    delivery_rows = book.delivered_rows_on(
        ch.day,
        meal=ch.meal,
        staff=ch.ship,
    )

    for row in delivery_rows:
        if _is_blank(_sequence_display(row)):
            raise ValueError(
                "배송현황 연번이 빈칸입니다: "
                f"{_delivery_row_identifier(row)}."
            )

    values = [
        [_project_value(column, row) for column in columns]
        for row in delivery_rows
    ]
    return pd.DataFrame(values, columns=columns)


def _validate_choice(book: AnnualDeliveryBook, ch: Choice) -> None:
    if ch.meal not in ALLOWED_MEALS:
        allowed = ", ".join(ALLOWED_MEALS)
        raise ValueError(
            f"허용되지 않은 식종입니다: {ch.meal!r}. 허용값: {allowed}"
        )
    if not isinstance(ch.day, date):
        raise TypeError("날짜는 datetime.date 값이어야 합니다.")

    state = book.day_state(ch.day)
    if state is DayState.MISSING:
        raise ValueError(
            f"배송현황에 선택 날짜가 없습니다: {ch.day.isoformat()}"
        )
    if state is DayState.NO_DELIVERY:
        raise ValueError(
            f"선택 날짜의 배송현황이 전체 빈칸입니다: {ch.day.isoformat()}"
        )
    if _is_blank(ch.ship):
        raise ValueError("배송담당은 빈칸일 수 없습니다.")


def _project_value(column: str, row: DeliveryRow) -> Any:
    if column in ("배송순서", "연번"):
        return _sequence_display(row)
    if column in ("배송담당", "배송_new"):
        return _normalize_value(row.staff)
    if column in ("식종", "식사 구분"):
        return _normalize_value(row.meal)
    return _roster_value(row.person.extra, column)


def _delivery_row_identifier(row: DeliveryRow) -> str:
    name = row.person.display_name or "(빈 값)"
    return f"성명={name}, Excel 원본 행={row.source_row}"


def _roster_value(extra: Mapping[str, Any], column: str) -> Any:
    source_columns = _ROSTER_ALIASES.get(column, (column,))
    for source_column in source_columns:
        if source_column not in extra:
            continue
        value = _normalize_value(extra[source_column])
        if value != "":
            return value
    return ""


def _sequence_display(row: DeliveryRow) -> str:
    if _is_missing(row.sequence):
        return ""
    return row.sequence_display


def _normalize_value(value: Any) -> Any:
    return "" if _is_missing(value) else value


def _is_blank(value: Any) -> bool:
    normalized = _normalize_value(value)
    return isinstance(normalized, str) and not normalized.strip()


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False
