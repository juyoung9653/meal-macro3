"""연간 배송 장부에서 배송 리포트를 집계하는 서비스."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum

from ..core.delivery_matrix import (
    ALLOWED_MEALS,
    AnnualDeliveryBook,
    DayState,
    DeliveryRow,
    RosterEntry,
)
from ..core.diff_format import fmt_cell


class ReportStatus(StrEnum):
    """단일 배송 리포트 생성 결과 상태."""

    READY = "READY"
    INVALID_MEAL = "INVALID_MEAL"
    DATE_MISSING = "DATE_MISSING"
    NON_DELIVERY_DAY = "NON_DELIVERY_DAY"
    NO_DELIVERIES_FOR_MEAL = "NO_DELIVERIES_FOR_MEAL"
    INVALID_DATA = "INVALID_DATA"


@dataclass(frozen=True)
class DeliveryReportRow:
    """한 배송담당자의 리포트 표시 값."""

    staff_label: str
    household_size_cells: tuple[str, str, str, str, str]
    person_total: int
    memo: str
    reference_areas: str


@dataclass(frozen=True)
class DeliveryReportTotals:
    """리포트의 가구원 수별·인원 합계."""

    household_counts: tuple[int, int, int, int, int]
    person_total: int


@dataclass(frozen=True)
class DeliveryReport:
    """한 날짜·식종의 불변 배송 리포트."""

    day: date
    meal: str
    rows: tuple[DeliveryReportRow, ...]
    totals: DeliveryReportTotals


@dataclass(frozen=True)
class DeliveryReportResult:
    """단일 날짜·식종에 대한 배송 리포트 생성 결과."""

    day: date
    meal: str
    status: ReportStatus
    report: DeliveryReport | None = None
    message: str = ""
    is_ready: bool = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "is_ready", self.status is ReportStatus.READY)


@dataclass(frozen=True)
class _DeliveryAggregate:
    buckets_by_staff: dict[str, tuple[int, ...]]
    persons_by_staff: dict[str, frozenset[RosterEntry]]


class _InvalidDeliveryData(ValueError):
    pass


REPORT_HEADERS = ("배송담당", "1", "2", "3", "4", "5", "Total", "비고", "참고")
_EMPTY_BUCKETS = (0, 0, 0, 0, 0)


class DeliveryService:
    """``AnnualDeliveryBook``만 사용해 배송 리포트를 집계한다."""

    def __init__(
        self,
        book: AnnualDeliveryBook,
        staff_sort: Mapping[str, Sequence[str]] | None = None,
    ) -> None:
        self._book = book
        self._staff_sort = {
            meal: self._normalized_staff_order(names)
            for meal, names in (staff_sort or {}).items()
        }

    def build_report(self, day: date, meal: str) -> DeliveryReportResult:
        """날짜와 식종 하나를 집계하고 상태가 명시된 결과를 반환한다."""
        if meal not in ALLOWED_MEALS:
            return DeliveryReportResult(
                day=day,
                meal=meal,
                status=ReportStatus.INVALID_MEAL,
                message=(
                    f"유효하지 않은 식종입니다: {meal!r}. "
                    f"허용 식종: {', '.join(ALLOWED_MEALS)}"
                ),
            )

        day_state = self._book.day_state(day)
        if day_state is DayState.MISSING:
            return DeliveryReportResult(
                day=day,
                meal=meal,
                status=ReportStatus.DATE_MISSING,
                message=f"배송현황에 {day:%Y%m%d} 날짜가 없습니다.",
            )
        if day_state is DayState.NO_DELIVERY:
            return DeliveryReportResult(
                day=day,
                meal=meal,
                status=ReportStatus.NON_DELIVERY_DAY,
                message=f"{day:%Y%m%d} 배송 열이 전체 빈칸입니다.",
            )

        current_rows = self._book.delivered_rows_on(day, meal=meal)
        if not current_rows:
            return DeliveryReportResult(
                day=day,
                meal=meal,
                status=ReportStatus.NO_DELIVERIES_FOR_MEAL,
                message=f"{day:%Y%m%d} {meal}에 값 1인 배송이 없습니다.",
            )

        previous_day = self._book.previous_delivery_date(day, meal)
        previous_rows = (
            self._book.delivered_rows_on(previous_day, meal=meal)
            if previous_day is not None
            else ()
        )

        try:
            current = self._aggregate_rows(
                current_rows, context=f"현재일 {day:%Y%m%d} {meal}"
            )
            previous = self._aggregate_rows(
                previous_rows,
                context=(
                    f"직전 배송일 {previous_day:%Y%m%d} {meal}"
                    if previous_day is not None
                    else f"직전 배송일 없음 {meal}"
                ),
            )
        except _InvalidDeliveryData as exc:
            return DeliveryReportResult(
                day=day,
                meal=meal,
                status=ReportStatus.INVALID_DATA,
                message=str(exc),
            )

        return DeliveryReportResult(
            day=day,
            meal=meal,
            status=ReportStatus.READY,
            report=self._build_report(day, meal, current, previous),
        )

    def build_all(
        self,
        start: date,
        end: date,
        meal_filter: str | Sequence[str] | None = None,
    ) -> list[DeliveryReport]:
        """범위 내 장부 날짜에서 준비된 리포트만 반환한다."""
        if end < start:
            raise ValueError("종료일은 시작일보다 빠를 수 없습니다.")

        if meal_filter is None:
            requested_meals: Sequence[str] = ALLOWED_MEALS
        elif isinstance(meal_filter, str):
            requested_meals = (meal_filter,)
        else:
            requested_meals = tuple(meal_filter)

        if not requested_meals:
            raise ValueError("식종 필터는 하나 이상 선택해야 합니다.")
        invalid_meals = [
            meal for meal in requested_meals if meal not in ALLOWED_MEALS
        ]
        if invalid_meals:
            raise ValueError(
                f"유효하지 않은 식종 필터입니다: {invalid_meals!r}. "
                f"허용 식종: {', '.join(ALLOWED_MEALS)}"
            )
        selected_set = set(requested_meals)
        meals = tuple(meal for meal in ALLOWED_MEALS if meal in selected_set)
        results: list[DeliveryReport] = []

        for day in self._book.dates:
            if day < start:
                continue
            if day > end:
                break
            for meal in meals:
                report = self.build_report(day, meal)
                if report.status is ReportStatus.INVALID_DATA:
                    raise ValueError(report.message)
                if not report.is_ready:
                    continue
                if report.report is None:
                    raise RuntimeError("READY 배송 리포트에 출력 데이터가 없습니다.")
                results.append(report.report)

        return results

    @staticmethod
    def _normalized_staff_order(names: Sequence[str]) -> tuple[str, ...]:
        ordered: list[str] = []
        seen: set[str] = set()
        for name in names:
            normalized = name.strip()
            if normalized and normalized not in seen:
                ordered.append(normalized)
                seen.add(normalized)
        return tuple(ordered)

    @staticmethod
    def _aggregate_rows(
        rows: Sequence[DeliveryRow], *, context: str
    ) -> _DeliveryAggregate:
        locations: dict[tuple[str, str, int | None], set[RosterEntry]] = defaultdict(
            set
        )
        persons_by_staff: dict[str, set[RosterEntry]] = defaultdict(set)

        for row in rows:
            staff = row.staff.strip()
            sequence = row.sequence_display.strip()
            if not staff:
                raise _InvalidDeliveryData(
                    f"{context}: 배송담당자가 빈칸입니다. {row.description}"
                )
            # 순번이 없으면 서로 다른 대상자를 한 배송지로 잘못 합치지 않는다.
            source_row = None if sequence else row.person.source_row
            locations[(staff, sequence, source_row)].add(row.person)
            persons_by_staff[staff].add(row.person)

        buckets_by_staff: dict[str, list[int]] = defaultdict(lambda: [0] * 5)
        for (staff, sequence, _source_row), people in sorted(
            locations.items(),
            key=lambda item: (
                item[0][0].casefold(),
                item[0][0],
                item[0][1].casefold(),
                item[0][1],
                item[0][2] or 0,
            ),
        ):
            household_size = len(people)
            if household_size >= 6:
                descriptions = ", ".join(
                    person.description
                    for person in sorted(people, key=lambda person: person.source_row)
                )
                raise _InvalidDeliveryData(
                    f"{context}: 담당자 {staff!r}, 연번 {sequence!r} 배송지는 "
                    f"6명 이상입니다({household_size}명; 대상: {descriptions}). "
                    "현재 1~5명 템플릿만 지원합니다."
                )
            buckets_by_staff[staff][household_size - 1] += 1

        return _DeliveryAggregate(
            buckets_by_staff={
                staff: tuple(buckets)
                for staff, buckets in buckets_by_staff.items()
            },
            persons_by_staff={
                staff: frozenset(people)
                for staff, people in persons_by_staff.items()
            },
        )

    def _build_report(
        self,
        day: date,
        meal: str,
        current: _DeliveryAggregate,
        previous: _DeliveryAggregate,
    ) -> DeliveryReport:
        all_staff = set(current.buckets_by_staff) | set(previous.buckets_by_staff)
        staff_order = {
            staff: index
            for index, staff in enumerate(self._staff_sort.get(meal, ()))
        }
        sorted_staff = sorted(
            all_staff,
            key=lambda staff: (
                (0, staff_order[staff], "", "")
                if staff in staff_order
                else (1, 0, staff.casefold(), staff)
            ),
        )

        rows: list[DeliveryReportRow] = []
        totals = [0, 0, 0, 0, 0]
        person_total = 0

        for staff in sorted_staff:
            current_buckets = current.buckets_by_staff.get(staff, _EMPTY_BUCKETS)
            previous_buckets = previous.buckets_by_staff.get(staff, _EMPTY_BUCKETS)
            household_count = sum(current_buckets)
            person_count = sum(
                size * count for size, count in enumerate(current_buckets, start=1)
            )
            rows.append(
                DeliveryReportRow(
                    staff_label=f"{staff}\n({household_count})",
                    household_size_cells=(
                        fmt_cell(current_buckets[0], previous_buckets[0]),
                        fmt_cell(current_buckets[1], previous_buckets[1]),
                        fmt_cell(current_buckets[2], previous_buckets[2]),
                        fmt_cell(current_buckets[3], previous_buckets[3]),
                        fmt_cell(current_buckets[4], previous_buckets[4]),
                    ),
                    person_total=person_count,
                    memo="",
                    reference_areas=self._area_text(staff, current, previous),
                )
            )

            for index, count in enumerate(current_buckets):
                totals[index] += count
            person_total += person_count

        return DeliveryReport(
            day=day,
            meal=meal,
            rows=tuple(rows),
            totals=DeliveryReportTotals(
                household_counts=(
                    totals[0],
                    totals[1],
                    totals[2],
                    totals[3],
                    totals[4],
                ),
                person_total=person_total,
            ),
        )

    def _area_text(
        self,
        staff: str,
        current: _DeliveryAggregate,
        previous: _DeliveryAggregate,
    ) -> str:
        people = current.persons_by_staff.get(
            staff, frozenset()
        ) | previous.persons_by_staff.get(staff, frozenset())
        areas: set[str] = set()
        for person in people:
            raw_area = person.extra.get("관할동")
            if raw_area is None:
                continue
            area = str(raw_area).strip()
            if area:
                areas.add(area)

        ordered = sorted(areas, key=lambda value: (value.casefold(), value))
        return "\n".join(
            ", ".join(ordered[index : index + 2])
            for index in range(0, len(ordered), 2)
        )
