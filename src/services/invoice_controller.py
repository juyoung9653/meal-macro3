"""AnnualDeliveryBook 기반 청구서 생성 서비스."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from ..core.date_utils import get_week_ranges
from ..core.delivery_matrix import (
    ALLOWED_MEALS,
    AnnualDeliveryBook,
    DeliveryCellValue,
    DeliveryRow,
    MonthState,
    RosterEntry,
)
from ..core.invoice_data import (
    EnrichedPersonDict,
    MealPrices,
    MonthlyDelivery,
    MonthlyDeliveryBatch,
    WeeklyDelivery,
)
from ..core.models import FULL_ADDRESS_FIELD, combine_address
from ..core.settings import get_settings
from ..excel.annual_workbook import AnnualWorkbook
from ..generators.invoice_generator import make_invoice

_MEAL_ORDER = {meal: index for index, meal in enumerate(ALLOWED_MEALS)}


@dataclass
class InvoiceGenerationConfig:
    """청구서 생성 입력.

    ``annual_book``을 직접 제공하는 경로가 우선이며, 없으면
    ``annual_workbook_path`` 또는 중앙 설정의 연간 워크북 경로를
    :class:`AnnualWorkbook`으로 읽는다.
    """

    template_path: str
    output_path: str
    year: int
    month: int
    annual_book: AnnualDeliveryBook | None = None
    annual_workbook_path: str | Path | None = None
    annual_workbook_password: str | None = None
    col_mapping: dict = field(default_factory=dict)
    unit_prices: Mapping[str, int] | None = None
    meal_type: str = "급식"


@dataclass
class InvoiceWarning:
    code: str
    message: str
    details: dict = field(default_factory=dict)


@dataclass
class InvoiceResult:
    success: bool
    count: int = 0
    batch: MonthlyDeliveryBatch | None = None
    # Deprecated compatibility projection for non-application callers.
    enriched: list[EnrichedPersonDict] = field(default_factory=list)
    warnings: list[InvoiceWarning] = field(default_factory=list)
    error: str | None = None


@dataclass
class _PersonDeliverySummary:
    counts: dict[str, int]
    weekly: list[WeeklyDelivery]


class InvoiceService:
    """연간 배송 책을 유일한 배송 사실로 사용하는 청구서 서비스."""

    def __init__(self, annual_book: AnnualDeliveryBook | None = None):
        self._annual_book = annual_book

    def generate(self, config: InvoiceGenerationConfig) -> InvoiceResult:
        try:
            book = self._resolve_book(config)
        except Exception as error:  # noqa: BLE001 - 사용자용 결과로 변환
            return InvoiceResult(
                success=False,
                error=f"연간 배송 데이터 로드 실패:\n{error}",
            )

        validation_error = self._validate_target_month(book, config.year, config.month)
        if validation_error:
            return InvoiceResult(success=False, error=validation_error)
        if not book.roster:
            return InvoiceResult(success=False, error="명단 데이터가 없습니다.")

        try:
            unit_prices = self._resolve_unit_prices(config.unit_prices)
        except (TypeError, ValueError) as error:
            return InvoiceResult(success=False, error=f"급식 단가 설정 오류:\n{error}")

        summaries = _calculate_monthly_summaries(book, config.year, config.month)
        try:
            rows_by_person = _rows_by_person(book, config.year, config.month)
        except ValueError as error:
            return InvoiceResult(
                success=False,
                error=f"배송 메타데이터 오류:\n{error}",
            )
        deliveries = tuple(
            _build_monthly_delivery(
                entry,
                rows_by_person.get(entry, ()),
                summaries[entry],
            )
            for entry in book.roster
        )
        batch = MonthlyDeliveryBatch(
            year=config.year,
            month=config.month,
            prices=MealPrices(
                breakfast=unit_prices["조식"],
                lunch=unit_prices["중식"],
                dinner=unit_prices["석식"],
            ),
            is_vacation_month=self._is_vacation_month(book, config.year, config.month),
            deliveries=tuple(item for item in deliveries if item.total_count > 0),
        )

        if not Path(config.template_path).is_file():
            return InvoiceResult(
                success=False,
                error=f"템플릿 파일 없음:\n{config.template_path}",
            )

        try:
            count, rendered = make_invoice(
                batch,
                config.col_mapping,
                config.output_path,
                template_path=config.template_path,
                year=config.year,
                month=config.month,
                unit_prices=unit_prices,
                is_vacation_month=batch.is_vacation_month,
                meal_type=config.meal_type,
                profile_conflicts=book.profile_conflicts,
            )
        except Exception as error:  # noqa: BLE001 - 생성 실패를 결과로 반환
            return InvoiceResult(success=False, error=f"청구서 생성 실패:\n{error}")

        if not isinstance(rendered, MonthlyDeliveryBatch):
            return InvoiceResult(success=False, error="청구서 생성 결과 형식이 올바르지 않습니다.")
        if count != len(rendered.deliveries):
            return InvoiceResult(
                success=False,
                error=(
                    "청구서 생성 결과 건수가 일치하지 않습니다: "
                    f"작성 {count}건, 출력 대상 {len(rendered.deliveries)}건"
                ),
            )
        warnings = []
        if book.profile_conflicts:
            warnings.append(
                InvoiceWarning(
                    code="DB 데이터 검토",
                    message=(
                        "같은 사람의 인적사항에서 서로 다른 값 "
                        f"{len(book.profile_conflicts)}건을 발견했습니다. "
                        "빈칸은 자동 보완했고, 불일치는 생성된 청구서의 "
                        "'DB검토' 시트에 기록했습니다."
                    ),
                    details={"count": len(book.profile_conflicts)},
                )
            )
        return InvoiceResult(
            success=True,
            count=count,
            batch=batch,
            enriched=[_legacy_record(item, batch.prices) for item in batch.deliveries],
            warnings=warnings,
        )

    def _resolve_book(self, config: InvoiceGenerationConfig) -> AnnualDeliveryBook:
        if config.annual_book is not None:
            return config.annual_book
        if self._annual_book is not None:
            return self._annual_book

        settings = get_settings()
        configured_path = settings.get("annual_workbook", "path", default="")
        path = config.annual_workbook_path or configured_path
        if not path:
            raise ValueError(
                "연간 배송 Excel 경로가 설정되지 않았습니다. "
                "AnnualDeliveryBook 또는 annual_workbook_path를 제공하세요."
            )

        if config.annual_workbook_password is None:
            password = settings.get("annual_workbook", "password", default="")
        else:
            password = config.annual_workbook_password
        return AnnualWorkbook(path, password=password).load()

    @staticmethod
    def _validate_target_month(
        book: AnnualDeliveryBook, year: int, month: int
    ) -> str | None:
        if not 1 <= month <= 12:
            return "청구 대상 월은 1부터 12 사이여야 합니다."
        if book.year != year:
            return (
                f"청구 대상 연도는 {year}년이지만 연간 배송 데이터는 "
                f"{book.year}년입니다."
            )
        if not book.dates:
            return "연간 배송 데이터에 날짜 헤더가 없습니다."

        state = book.month_state(year, month)
        if state is MonthState.MISSING:
            return f"연간 배송 데이터에 {year}년 {month}월 날짜 헤더가 없습니다."
        if state is MonthState.UNENTERED:
            return (
                f"{year}년 {month}월 배송현황이 미입력 상태입니다. "
                "월 데이터를 입력한 뒤 다시 생성하세요."
            )
        return None

    @staticmethod
    def _resolve_unit_prices(
        overrides: Mapping[str, int] | None,
    ) -> dict[str, int]:
        configured = get_settings().get("invoice", "unit_prices", default={})
        if not isinstance(configured, Mapping):
            configured = {}
        if overrides is not None and not isinstance(overrides, Mapping):
            raise TypeError("unit_prices는 식종별 매핑이어야 합니다.")

        result: dict[str, int] = {}
        for meal in ALLOWED_MEALS:
            raw_value: Any = 9500
            if meal in configured:
                raw_value = configured[meal]
            if overrides is not None and meal in overrides:
                raw_value = overrides[meal]
            if isinstance(raw_value, bool):
                raise TypeError(f"{meal} 단가는 정수여야 합니다.")
            try:
                price = int(raw_value)
            except (TypeError, ValueError) as error:
                raise ValueError(f"{meal} 단가는 정수여야 합니다: {raw_value!r}") from error
            if isinstance(raw_value, float) and not raw_value.is_integer():
                raise ValueError(f"{meal} 단가는 정수여야 합니다: {raw_value!r}")
            if price < 0:
                raise ValueError(f"{meal} 단가는 0 이상이어야 합니다.")
            result[meal] = price
        return result

    @staticmethod
    def _is_vacation_month(
        book: AnnualDeliveryBook, year: int, month: int
    ) -> bool:
        """대상 월 중식 행에 실제 배송(1)이 있을 때만 방학 월로 본다."""
        if year != book.year:
            return False
        target_dates = tuple(
            day for day in book.dates if day.year == year and day.month == month
        )
        return any(
            row.meal == "중식"
            and any(
                row.raw_value_on(day) is DeliveryCellValue.DELIVERED
                for day in target_dates
            )
            for row in book.delivery_rows
        )


def _calculate_monthly_summaries(
    book: AnnualDeliveryBook, year: int, month: int
) -> dict[RosterEntry, _PersonDeliverySummary]:
    """대상 월의 모든 배송 셀을 한 번 순회해 월/주차 집계를 함께 만든다."""
    weeks = get_week_ranges(year, month)
    week_by_date = {
        day: index
        for index, (_week, start, end) in enumerate(weeks)
        for day in book.dates
        if start <= day <= end
    }

    summaries: dict[RosterEntry, _PersonDeliverySummary] = {}
    for entry in book.roster:
        weekly: list[WeeklyDelivery] = [
            WeeklyDelivery(
                week=week,
                start_date=start,
                end_date=end,
                breakfast_count=0,
                lunch_count=0,
                dinner_count=0,
            )
            for week, start, end in weeks
        ]
        summaries[entry] = _PersonDeliverySummary(
            counts={meal: 0 for meal in ALLOWED_MEALS},
            weekly=weekly,
        )

    target_dates = tuple(
        day for day in book.dates if day.year == year and day.month == month
    )
    for row in book.delivery_rows:
        summary = summaries[row.person]
        breakfast_multiplier = _breakfast_invoice_multiplier(row, target_dates)
        for day in target_dates:
            if row.raw_value_on(day) is not DeliveryCellValue.DELIVERED:
                continue
            increment = breakfast_multiplier if row.meal == "조식" else 1
            summary.counts[row.meal] += increment
            week_index = week_by_date.get(day)
            if week_index is not None:
                item = summary.weekly[week_index]
                summary.weekly[week_index] = WeeklyDelivery(
                    week=item.week,
                    start_date=item.start_date,
                    end_date=item.end_date,
                    breakfast_count=(
                        item.breakfast_count
                        + (increment if row.meal == "조식" else 0)
                    ),
                    lunch_count=(
                        item.lunch_count
                        + (increment if row.meal == "중식" else 0)
                    ),
                    dinner_count=(
                        item.dinner_count
                        + (increment if row.meal == "석식" else 0)
                    ),
                )

    return summaries


def _breakfast_invoice_multiplier(
    row: DeliveryRow,
    target_dates: tuple[date, ...],
) -> int:
    """대상 월의 토·일이 모두 비면 VBA처럼 조식 배송값을 3배한다."""
    if row.meal != "조식":
        return 1
    weekend_values = (
        row.raw_value_on(day)
        for day in target_dates
        if day.weekday() >= 5
    )
    if all(
        value is None or value is DeliveryCellValue.BLANK
        for value in weekend_values
    ):
        return 3
    return 1


def _rows_by_person(
    book: AnnualDeliveryBook, year: int, month: int
) -> dict[RosterEntry, tuple[DeliveryRow, ...]]:
    """대상 월에 실제 배송이 있는 행만 사람별 메타데이터 행으로 묶는다."""
    target_dates = tuple(
        day for day in book.dates if day.year == year and day.month == month
    )
    grouped: dict[RosterEntry, list[DeliveryRow]] = {}
    for row in book.delivery_rows:
        if not any(
            row.raw_value_on(day) is DeliveryCellValue.DELIVERED
            for day in target_dates
        ):
            continue

        if not row.staff.strip():
            raise ValueError(
                f"{row.description}, 필드=배송담당(staff): "
                f"{year}년 {month}월 실제 배송행의 값이 비어 있습니다."
            )

        grouped.setdefault(row.person, []).append(row)
    return {
        person: tuple(
            sorted(
                rows,
                key=lambda row: (
                    _MEAL_ORDER[row.meal],
                    row.sequence_sort_key,
                    row.staff.casefold(),
                ),
            )
        )
        for person, rows in grouped.items()
    }


def _build_monthly_delivery(
    roster: RosterEntry,
    delivery_rows: tuple[DeliveryRow, ...],
    summary: _PersonDeliverySummary,
) -> MonthlyDelivery:
    note_value = roster.extra.get("비고", "")
    return MonthlyDelivery(
        roster=roster,
        note="" if note_value is None else str(note_value).strip(),
        delivery_staff=_join_unique(row.staff for row in delivery_rows),
        delivery_sequence=_join_unique(row.sequence_display for row in delivery_rows),
        breakfast_count=summary.counts["조식"],
        lunch_count=summary.counts["중식"],
        dinner_count=summary.counts["석식"],
        weekly=tuple(summary.weekly),
    )


def _legacy_record(delivery: MonthlyDelivery, prices: MealPrices) -> EnrichedPersonDict:
    """Explicit one-way compatibility projection; application paths never consume it."""
    record = delivery.roster.as_dict()
    if "도로명주소" in record or "상세주소" in record:
        record[FULL_ADDRESS_FIELD] = combine_address(
            record.get("도로명주소"), record.get("상세주소")
        )
    record.update(
        {
            "_note": delivery.note,
            "_total": delivery.total_count,
            "_amount": f"{delivery.amount(prices):,}원",
            "_bf_cnt": delivery.breakfast_count,
            "_lh_cnt": delivery.lunch_count,
            "_dn_cnt": delivery.dinner_count,
            "weekly_meal_counts": [
                {
                    "week": item.week,
                    "start_date": f"{item.start_date:%Y%m%d}",
                    "end_date": f"{item.end_date:%Y%m%d}",
                    "조식": item.breakfast_count,
                    "중식": item.lunch_count,
                    "석식": item.dinner_count,
                }
                for item in delivery.weekly
            ],
            "배송담당": delivery.delivery_staff,
            "배송순서": delivery.delivery_sequence,
            "연번": delivery.delivery_sequence,
        }
    )
    return record  # type: ignore[return-value]


def _join_unique(values: Iterable[object]) -> str:
    unique: list[str] = []
    for value in values:
        text = str(value).strip()
        if text and text not in unique:
            unique.append(text)
    return " / ".join(unique)
