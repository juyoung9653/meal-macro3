"""청구서 컨트롤러 — 데이터 보강, 배송 계산, 청구서 생성 오케스트레이션"""

import calendar
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from PyQt6.QtWidgets import QMessageBox, QWidget

from ..core.identity import normalize_birthdate, normalize_identity_name
from ..core.meal_types import MealType
from ..generators.daily_delivery_generator import (
    DAILY_ADDRESS_KEY,
    DAILY_DRIVER_KEY,
    DAILY_REGION_PRIMARY_KEY,
    DAILY_REGION_SECONDARY_KEY,
)
from ..generators.invoice_generator import make_invoice
from ..templates.config import auto_detect_semantic_columns
from .delivery_calculator import (
    count_delivery_days,
    to_bool,
)
from .holiday_fetcher import get_kr_holidays
from .lunch_data import LunchRoster
from .note_parser import expand_note_dates
from .schedule_counter import _get_schedule
from .vacation_calculator import get_lunch_delivery_dates_with_note

log = logging.getLogger(__name__)


LUNCH_PERSON_REQUIRED_FIELDS = (
    "구분",
    "학교명",
    "학년",
    "성명",
    "생년월일",
    "세대주성명",
    "아동과의관계",
    "관할동",
    "주소",
    "핸드폰",
)
LUNCH_DAILY_REQUIRED_FIELDS = (DAILY_DRIVER_KEY, DAILY_REGION_PRIMARY_KEY)
LUNCH_DAILY_MAPPING_FIELDS = (
    DAILY_DRIVER_KEY,
    DAILY_REGION_PRIMARY_KEY,
    DAILY_REGION_SECONDARY_KEY,
)
DAILY_DELIVERY_VALUE_FIELDS = LUNCH_DAILY_MAPPING_FIELDS + (DAILY_ADDRESS_KEY,)
LUNCH_MAPPING_LABELS = {
    DAILY_DRIVER_KEY: "배송 담당",
    DAILY_REGION_PRIMARY_KEY: "담당 구역 1순위",
    DAILY_REGION_SECONDARY_KEY: "담당 구역 2순위",
}


# ── Result types (pure data, no UI) ──


@dataclass
class InvoiceWarning:
    """A non-fatal issue that should be shown to the user but doesn't block generation."""

    code: str  # e.g., "unmatched_school", "lunch_not_loaded", "no_lunch_roster"
    message: str
    details: dict = field(default_factory=dict)


@dataclass
class InvoiceResult:
    """Structured result from InvoiceService.generate()."""

    success: bool
    count: int = 0
    enriched: list[dict] = field(default_factory=list)
    warnings: list[InvoiceWarning] = field(default_factory=list)
    error: str | None = None


# ── Pure business logic (no UI dependencies) ──


def _calc_delivery_dates(
    year: int,
    month: int,
    entry,  # LunchSchoolEntry
    grade: int = 0,
    excluded_dates: set[date] | None = None,
) -> tuple[date | None, date | None]:
    """개인별 방학 배송시작일/종료일 계산 (실제 첫/마지막 배송일).

    배송기간: 방학식+1일 ~ 개학식-1일 (급식여부 무관, 월 경계 무관).
    급식여부 + 학년제외로 배송 없음 판정 시 None 반환.
    """
    direct_range = entry.start_date is not None or entry.end_date is not None

    # 급식여부 + 학년제외 → 배송 발생 여부만 판단 (예전 일정 모델)
    vacation_meal = entry.vacation_meal
    opening_meal = entry.opening_meal
    if grade > 0 and not direct_range:
        if (
            entry.all_grades_excluded_vacation
            or grade in entry.excluded_grades_vacation
        ):
            vacation_meal = False
        if entry.all_grades_excluded_opening or grade in entry.excluded_grades_opening:
            opening_meal = False

    # 배송 없음: 학년제외로 양쪽 모두 미급식 → 배송일 없음
    if not direct_range and not vacation_meal and not opening_meal:
        return None, None

    # ── 배송기간: 새 일정은 시작/종료일을 포함, 예전 일정은 +1/-1 ──
    if direct_range:
        eff_start, eff_end = entry.effective_range()
    else:
        eff_start = (
            entry.vacation_date + timedelta(days=1) if entry.vacation_date else None
        )
        eff_end = entry.opening_date - timedelta(days=1) if entry.opening_date else None

    if eff_start is None:
        return None, None
    if eff_end is None:
        last_day = calendar.monthrange(year, month)[1]
        eff_end = date(year, month, last_day)

    if eff_start > eff_end:
        return None, None

    # 공휴일: 배송기간이 걸치는 모든 연도 수집
    holidays: set[date] = set()
    for y in range(eff_start.year, eff_end.year + 1):
        holidays.update(get_kr_holidays(y))
    blocked = set(holidays) | (excluded_dates or set())
    schedule = _get_schedule()[MealType.LUNCH]

    # 첫 배송일 찾기
    first_delivery = None
    d = eff_start
    while d <= eff_end:
        if d.weekday() in schedule and d not in blocked:
            first_delivery = d
            break
        d += timedelta(days=1)

    # 마지막 배송일 찾기
    last_delivery = None
    d = eff_end
    while d >= eff_start:
        if d.weekday() in schedule and d not in blocked:
            last_delivery = d
            break
        d -= timedelta(days=1)

    return first_delivery, last_delivery


def _canonicalize_person(
    person: dict,
    mapping: dict,
    template_columns: list[str],
) -> dict:
    """원본 명단 한 행에 템플릿 기준의 공통 키를 함께 저장한다."""

    canonical = dict(person)
    note_column = str(mapping.get("__note", "") or "")
    if note_column and note_column in canonical:
        canonical[note_column] = _import_note(canonical[note_column])
    if "비고" in canonical:
        canonical["비고"] = _import_note(canonical["비고"])
    for template_column in template_columns:
        source_column = str(mapping.get(template_column, "") or "")
        if source_column and source_column in person:
            canonical[template_column] = canonical.get(source_column, "")
    return canonical


def _import_note(value: Any) -> str:
    note = str(value or "")
    return "" if note.lstrip().startswith("//") else note


def _missing_mapping_fields(
    mapping: dict,
    headers: list[str],
    required_fields: tuple[str, ...],
) -> list[str]:
    header_set = set(headers)
    return [
        field_name
        for field_name in required_fields
        if not (source_column := str(mapping.get(field_name, "") or ""))
        or source_column not in header_set
    ]


def _person_identity(person: dict) -> tuple[str, str] | None:
    """이름과 생년월일이 모두 있을 때만 동일인 키를 만든다."""

    name = normalize_identity_name(person.get("성명", ""))
    birthdate = normalize_birthdate(person.get("생년월일", ""))
    if not name or not birthdate:
        return None
    return name, birthdate


def _combine_notes(first: Any, second: Any) -> str:
    lines: list[str] = []
    for raw in (first, second):
        for line in str(raw or "").splitlines():
            clean = line.strip()
            if clean and clean not in lines:
                lines.append(clean)
    return "\n".join(lines)


def _merge_people_by_identity(people: list[dict]) -> list[dict]:
    """이름+생년월일 동일인을 한 행으로 합치고 식사 정보는 OR/최댓값 처리."""

    merged: list[dict] = []
    positions: dict[tuple[str, str], int] = {}
    for person in people:
        identity = _person_identity(person)
        if identity is None or identity not in positions:
            if identity is not None:
                positions[identity] = len(merged)
            merged.append(dict(person))
            continue

        target = merged[positions[identity]]
        for key, value in person.items():
            if key.startswith("_") and key not in DAILY_DELIVERY_VALUE_FIELDS:
                continue
            if not str(target.get(key, "") or "").strip() and str(value or "").strip():
                target[key] = value

        for key in ("_breakfast", "_dinner", "_lunch"):
            target[key] = (
                1 if to_bool(target.get(key, 0)) or to_bool(person.get(key, 0)) else 0
            )
        for key in ("_bf_cnt", "_dn_cnt", "_lh_cnt"):
            target[key] = max(
                int(target.get(key, 0) or 0),
                int(person.get(key, 0) or 0),
            )

        target["_note"] = _combine_notes(
            target.get("_note", ""), person.get("_note", "")
        )
        for date_key in ("_lunch_dates", "_daily_lunch_dates"):
            lunch_dates = {
                str(item) for item in target.get(date_key, []) if str(item).strip()
            }
            lunch_dates.update(
                str(item) for item in person.get(date_key, []) if str(item).strip()
            )
            target[date_key] = sorted(lunch_dates)
        if not target.get("_delivery_start") and person.get("_delivery_start"):
            target["_delivery_start"] = person["_delivery_start"]
        if not target.get("_delivery_end") and person.get("_delivery_end"):
            target["_delivery_end"] = person["_delivery_end"]

    return merged


def _parse_grade(value: Any) -> int:
    match = re.search(r"\d+", str(value or ""))
    return int(match.group()) if match else 0


def _is_stopped(person: dict, headers: list[str]) -> bool:
    stop_columns = [h for h in headers if "중지" in h or "중단" in h]
    return any(
        "중지" in str(person.get(column, "")) or "중단" in str(person.get(column, ""))
        for column in stop_columns
    )


def _mapped_note(person: dict, mapping: dict) -> str:
    note_column = str(mapping.get("__note", "") or "")
    note = _import_note(person.get(note_column, "")) if note_column else ""
    return note if re.search(r"\d{1,2}[/.]\d{1,2}|매주", note) else ""


class InvoiceService:
    """Pure business logic for invoice generation. No UI dependencies."""

    def generate(
        self,
        roster_data: list[dict],
        output_path: str,
        year: int,
        month: int,
        breakfast_price: int,
        dinner_price: int,
        lunch_price: int,
        template_path: str,
        col_mapping: dict,
        excluded_dates: set[date],
        roster_headers: list[str],
        template_columns: list[str],
        has_lunch_file: bool = False,
        lunch_roster: LunchRoster | None = None,
        lunch_mapping: dict | None = None,
        lunch_person_data: list[dict] | None = None,
        lunch_person_headers: list[str] | None = None,
        lunch_person_mapping: dict | None = None,
        provider_name: str = "",
        write_workbook: bool = True,
    ) -> InvoiceResult:
        """두 개인 명단과 학교별 방학 일정을 결합해 청구서를 만든다.

        roster_data는 조식·석식 명단, lunch_person_data는 중식 명단이다.
        중식 명단의 모든 행은 중식 대상으로 보며 학교 일정의 시작일~종료일을
        적용한다. 두 명단에서 이름+생년월일이 같은 사람은 한 행으로 합친다.

        lunch_person_data가 None인 호출은 예전 방중 플래그 방식을 유지한다.
        """

        separate_lunch_roster = lunch_person_data is not None
        lunch_rows = lunch_person_data or []
        if not roster_data and not lunch_rows:
            return InvoiceResult(success=False, error="명단 데이터가 없습니다.")

        if separate_lunch_roster and lunch_rows:
            lunch_map = lunch_person_mapping or {}
            missing_fields = _missing_mapping_fields(
                lunch_map,
                lunch_person_headers or [],
                LUNCH_PERSON_REQUIRED_FIELDS + LUNCH_DAILY_REQUIRED_FIELDS,
            )
            if missing_fields:
                missing_labels = [
                    LUNCH_MAPPING_LABELS.get(field_name, field_name)
                    for field_name in missing_fields
                ]
                return InvoiceResult(
                    success=False,
                    error=(
                        "중식 명단 컬럼 매핑을 확인하세요.\n"
                        "필수 항목: " + ", ".join(missing_labels)
                    ),
                )
            if roster_data:
                main_missing_fields = _missing_mapping_fields(
                    col_mapping,
                    roster_headers,
                    ("성명", "생년월일"),
                )
                if main_missing_fields:
                    return InvoiceResult(
                        success=False,
                        error=(
                            "조식·석식 명단 컬럼 매핑을 확인하세요.\n"
                            "중식 명단과 동일인을 합치기 위한 필수 항목: "
                            + ", ".join(main_missing_fields)
                        ),
                    )

        warnings: list[InvoiceWarning] = []
        schedule_available = bool(
            lunch_roster is not None and getattr(lunch_roster, "entries", [])
        )
        is_vacation_month = bool(
            lunch_roster and lunch_roster.is_vacation_month(year, month)
        )
        lunch_warned: set[str] = set()
        missing_school_count = 0

        if separate_lunch_roster and lunch_rows and not schedule_available:
            return InvoiceResult(
                success=False,
                error=(
                    "학교별 방학 일정이 없거나 유효한 일정이 0건입니다.\n"
                    "일정 파일의 학교명·시작일·종료일 컬럼을 확인하세요."
                ),
            )

        def apply_lunch(
            canonical: dict,
            source_person: dict,
            source_mapping: dict,
        ) -> tuple[int, list[str], list[str], str, str]:
            nonlocal missing_school_count

            if not schedule_available or lunch_roster is None:
                return 0, [], [], "", ""

            school_name = str(canonical.get("학교명", "") or "").strip()
            if not school_name and lunch_mapping:
                legacy_school_col = str(
                    lunch_mapping.get("roster_school_col", "") or ""
                )
                school_name = str(
                    source_person.get(legacy_school_col, "") or ""
                ).strip()

            if not school_name:
                missing_school_count += 1
                return 0, [], [], "", ""

            entry = lunch_roster.find_school(school_name)
            if entry is None:
                if school_name:
                    lunch_warned.add(school_name)
                return 0, [], [], "", ""

            grade_value = canonical.get("학년", "")
            if not grade_value and lunch_mapping:
                legacy_grade_col = str(lunch_mapping.get("roster_grade_col", "") or "")
                grade_value = source_person.get(legacy_grade_col, "")
            grade = _parse_grade(grade_value)

            effective_start, effective_end = entry.effective_range()
            month_start = date(year, month, 1)
            month_end = date(year, month, calendar.monthrange(year, month)[1])
            range_start = effective_start or month_start
            range_end = effective_end or month_end
            note = _mapped_note(source_person, source_mapping)
            all_dates: set[date] = set()
            cursor = date(range_start.year, range_start.month, 1)
            last_month = date(range_end.year, range_end.month, 1)
            while cursor <= last_month and range_start <= range_end:
                month_dates = get_lunch_delivery_dates_with_note(
                    cursor.year,
                    cursor.month,
                    entry,
                    grade=grade,
                    excluded_dates=(excluded_dates.copy() if excluded_dates else None),
                )
                lunch_note_exclusions = {
                    note_date
                    for note_date, meals in expand_note_dates(
                        note, cursor.year, cursor.month
                    )
                    if MealType.LUNCH in meals
                }
                all_dates.update(
                    delivery_date
                    for delivery_date in month_dates
                    if delivery_date not in lunch_note_exclusions
                )
                if cursor.month == 12:
                    cursor = date(cursor.year + 1, 1, 1)
                else:
                    cursor = date(cursor.year, cursor.month + 1, 1)

            dates = sorted(
                delivery_date
                for delivery_date in all_dates
                if delivery_date.year == year and delivery_date.month == month
            )
            delivery_start, delivery_end = _calc_delivery_dates(
                year,
                month,
                entry,
                grade=grade,
                excluded_dates=excluded_dates.copy() if excluded_dates else None,
            )
            return (
                len(dates),
                [delivery_date.isoformat() for delivery_date in dates],
                [delivery_date.isoformat() for delivery_date in sorted(all_dates)],
                delivery_start.isoformat() if delivery_start else "",
                delivery_end.isoformat() if delivery_end else "",
            )

        enriched_rows: list[dict] = []
        breakfast_column = str(col_mapping.get("__breakfast", "") or "")
        dinner_column = str(col_mapping.get("__dinner", "") or "")
        vacation_column = str(col_mapping.get("__vacation", "") or "")

        for person in roster_data:
            if _is_stopped(person, roster_headers):
                continue

            canonical = _canonicalize_person(person, col_mapping, template_columns)
            note = _mapped_note(person, col_mapping)
            has_breakfast = to_bool(person.get(breakfast_column, 0))
            has_dinner = to_bool(person.get(dinner_column, 0))
            counts = count_delivery_days(
                year,
                month,
                has_breakfast,
                has_dinner,
                False,
                note,
                excluded_dates=excluded_dates.copy() if excluded_dates else None,
            )

            lunch_count = 0
            lunch_dates: list[str] = []
            daily_lunch_dates: list[str] = []
            delivery_start = ""
            delivery_end = ""
            legacy_lunch_target = (
                not separate_lunch_roster
                and has_lunch_file
                and to_bool(person.get(vacation_column, 0))
            )
            if legacy_lunch_target:
                (
                    lunch_count,
                    lunch_dates,
                    daily_lunch_dates,
                    delivery_start,
                    delivery_end,
                ) = apply_lunch(canonical, person, col_mapping)

            canonical.update(
                {
                    "_breakfast": 1 if has_breakfast else 0,
                    "_dinner": 1 if has_dinner else 0,
                    "_lunch": 1 if lunch_count > 0 else 0,
                    "_note": note,
                    "_bf_cnt": counts["breakfast"],
                    "_dn_cnt": counts["dinner"],
                    "_lh_cnt": lunch_count,
                    "_lunch_dates": lunch_dates,
                    "_daily_lunch_dates": daily_lunch_dates,
                    "_delivery_start": delivery_start,
                    "_delivery_end": delivery_end,
                }
            )
            enriched_rows.append(canonical)

        if separate_lunch_roster:
            lunch_map = lunch_person_mapping or {}
            lunch_headers = lunch_person_headers or []
            for person in lunch_rows:
                if _is_stopped(person, lunch_headers):
                    continue

                canonical = _canonicalize_person(
                    person,
                    lunch_map,
                    template_columns + list(LUNCH_DAILY_MAPPING_FIELDS),
                )
                canonical[DAILY_ADDRESS_KEY] = canonical.get("주소", "")
                note = _mapped_note(person, lunch_map)
                (
                    lunch_count,
                    lunch_dates,
                    daily_lunch_dates,
                    delivery_start,
                    delivery_end,
                ) = apply_lunch(canonical, person, lunch_map)
                canonical.update(
                    {
                        "_breakfast": 0,
                        "_dinner": 0,
                        "_lunch": 1 if lunch_count > 0 else 0,
                        "_note": note,
                        "_bf_cnt": 0,
                        "_dn_cnt": 0,
                        "_lh_cnt": lunch_count,
                        "_lunch_dates": lunch_dates,
                        "_daily_lunch_dates": daily_lunch_dates,
                        "_delivery_start": delivery_start,
                        "_delivery_end": delivery_end,
                    }
                )
                enriched_rows.append(canonical)

        enriched = _merge_people_by_identity(enriched_rows)

        if missing_school_count:
            warnings.append(
                InvoiceWarning(
                    code="missing_school_name",
                    message=(
                        f"학교명이 비어 있는 중식 대상자가 {missing_school_count}명 있습니다.\n"
                        "해당 대상자의 중식 배송 건수는 0으로 처리됩니다."
                    ),
                    details={"count": missing_school_count},
                )
            )

        if lunch_warned and is_vacation_month:
            school_list = "\n".join(f"  • {name}" for name in sorted(lunch_warned))
            warnings.append(
                InvoiceWarning(
                    code="unmatched_school",
                    message=(
                        "다음 학교는 중식 명단에서 찾을 수 없습니다.\n"
                        "(학교별 방학 일정에 학교명이 없습니다.)\n"
                        "해당 학생들의 중식 배송일은 0으로 처리됩니다:\n\n"
                        f"{school_list}"
                    ),
                    details={"schools": sorted(lunch_warned)},
                )
            )

        if not write_workbook:
            return InvoiceResult(
                success=True,
                count=len(enriched),
                enriched=enriched,
                warnings=warnings,
            )

        if not os.path.exists(template_path):
            return InvoiceResult(
                success=False,
                error=f"템플릿 파일을 찾을 수 없습니다:\n{template_path}",
                warnings=warnings,
            )

        try:
            count, generated_people = make_invoice(
                enriched,
                col_mapping,
                output_path,
                template_path=template_path,
                year=year,
                month=month,
                unit_price_breakfast=breakfast_price,
                unit_price_dinner=dinner_price,
                unit_price_lunch=lunch_price,
                excluded_dates=excluded_dates.copy() if excluded_dates else None,
                is_vacation_month=is_vacation_month,
                provider_name=provider_name,
            )
        except Exception as error:  # noqa: BLE001 - UI 경계에 구조화된 오류 반환
            return InvoiceResult(
                success=False,
                error=f"청구서 생성 실패:\n{error}",
                warnings=warnings,
            )

        return InvoiceResult(
            success=True,
            count=count,
            enriched=generated_people,
            warnings=warnings,
        )


class InvoiceController:
    """UI-aware wrapper around InvoiceService. Renders dialogs from structured results."""

    def __init__(self, parent: QWidget, template_columns: list[str]):
        self._parent = parent
        self._service = InvoiceService()
        self.invoices: list[dict] = []
        self.output_dir: str | None = None
        self.roster_headers: list[str] = []
        self.roster_path: str | None = None
        self._template_columns = template_columns
        self._col_mapping: dict = {col: "" for col in template_columns}
        # 의미론적 컬럼 (auto-detect → GUI override 가능)
        _sem = auto_detect_semantic_columns(template_columns)
        self._col_mapping.update(_sem)
        # 배송 플래그
        self._col_mapping.update(
            {"__breakfast": "", "__dinner": "", "__vacation": "", "__note": ""}
        )
        self._col_mapping["__delivery_start"] = next(
            (column for column in template_columns if "배송시작" in column), ""
        )
        self._col_mapping["__delivery_end"] = next(
            (column for column in template_columns if "배송종료" in column), ""
        )
        self._excluded_dates: set[date] = set()
        self._year: int = 0
        self._month: int = 0
        # 학교별 방학 일정
        self._lunch_roster: LunchRoster | None = None
        self._lunch_mapping: dict = {}
        self._lunch_file_path: str = ""
        # 중식 대상자 명단
        self._lunch_person_data: list[dict] = []
        self._lunch_person_headers: list[str] = []
        self._lunch_person_mapping: dict = {}
        self._lunch_person_file_path: str = ""

    # ── UI-aware generate ──

    def generate(
        self,
        roster_data: list[dict],
        output_path: str,
        year: int,
        month: int,
        breakfast_price: int,
        dinner_price: int,
        lunch_price: int,
        template_path: str,
        has_lunch_file: bool = False,
        provider_name: str = "",
    ) -> tuple[bool, int, list[dict]]:
        """명단 데이터 → InvoiceService → (성공여부, 건수, 보강데이터).

        Wraps InvoiceService.generate() and renders warnings/errors as dialogs.
        Public API is preserved: same signature, same return type.
        """
        self._year = year
        self._month = month

        result = self._service.generate(
            roster_data=roster_data,
            output_path=output_path,
            year=year,
            month=month,
            breakfast_price=breakfast_price,
            dinner_price=dinner_price,
            lunch_price=lunch_price,
            template_path=template_path,
            col_mapping=self._col_mapping,
            excluded_dates=self._excluded_dates,
            roster_headers=self.roster_headers,
            template_columns=self._template_columns,
            has_lunch_file=has_lunch_file,
            lunch_roster=self._lunch_roster,
            lunch_mapping=self._lunch_mapping,
            lunch_person_data=self._lunch_person_data,
            lunch_person_headers=self._lunch_person_headers,
            lunch_person_mapping=self._lunch_person_mapping,
            provider_name=provider_name,
        )

        # Render warnings as dialogs
        for w in result.warnings:
            QMessageBox.warning(self._parent, w.code, w.message)

        # Render error as dialog
        if not result.success and result.error:
            QMessageBox.critical(self._parent, "오류", result.error)
            return False, 0, []

        self.invoices = result.enriched
        return result.success, result.count, result.enriched

    def prepare_daily_delivery_people(
        self,
        roster_data: list[dict],
        year: int,
        month: int,
        col_mapping: dict | None = None,
    ) -> InvoiceResult:
        """엑셀 청구서를 저장하지 않고 세 명단을 일일배송용으로 결합한다."""

        return self._service.generate(
            roster_data=roster_data,
            output_path="",
            year=year,
            month=month,
            breakfast_price=0,
            dinner_price=0,
            lunch_price=0,
            template_path="",
            col_mapping=col_mapping or self._col_mapping,
            excluded_dates=self._excluded_dates,
            roster_headers=self.roster_headers,
            template_columns=self._template_columns,
            has_lunch_file=bool(self._lunch_roster),
            lunch_roster=self._lunch_roster,
            lunch_mapping=self._lunch_mapping,
            lunch_person_data=self._lunch_person_data,
            lunch_person_headers=self._lunch_person_headers,
            lunch_person_mapping=self._lunch_person_mapping,
            write_workbook=False,
        )

    # ── Getter ──

    def get_invoices(self) -> list[dict]:
        return self.invoices

    def get_col_mapping(self) -> dict:
        return dict(self._col_mapping)

    def get_excluded_dates(self) -> set[date]:
        return self._excluded_dates.copy()

    def get_roster_headers(self) -> list[str]:
        return self.roster_headers

    def get_roster_path(self) -> str | None:
        return self.roster_path

    def set_lunch_roster(self, roster, mapping: dict, file_path: str = ""):
        """학교별 방학 일정 데이터 설정."""
        self._lunch_roster = roster
        self._lunch_mapping = mapping
        self._lunch_file_path = file_path

    def get_lunch_roster(self):
        return self._lunch_roster

    def get_lunch_mapping(self) -> dict:
        return dict(self._lunch_mapping)

    def set_lunch_people(
        self,
        data: list[dict],
        headers: list[str],
        mapping: dict,
        file_path: str = "",
    ):
        """중식 대상자 명단과 그 전용 컬럼 매핑을 설정한다."""

        self._lunch_person_data = list(data)
        self._lunch_person_headers = list(headers)
        self._lunch_person_mapping = dict(mapping)
        self._lunch_person_file_path = file_path

    def get_lunch_person_mapping(self) -> dict:
        return dict(self._lunch_person_mapping)

    # ── Setters ──

    def set_col_mapping(self, mapping: dict):
        """컬럼 매핑 설정."""
        updated = dict(self._col_mapping)
        updated.update(mapping)
        self._col_mapping = updated

    def set_excluded_dates(self, dates: set[date]):
        """배송 제외일 설정."""
        self._excluded_dates = set(dates)

    def add_excluded_date(self, d: date):
        """배송 제외일을 하나 추가."""
        self._excluded_dates.add(d)

    def clear_excluded_dates(self):
        """배송 제외일 전체 초기화."""
        self._excluded_dates.clear()

    def set_roster_headers(self, headers: list[str]):
        """명단 헤더 설정."""
        self.roster_headers = list(headers)

    def set_roster_path(self, path: str | None):
        """명단 파일 경로 설정."""
        self.roster_path = path

    def get_invoice_data(self):
        from ..core.invoice_data import InvoiceData

        return InvoiceData(
            invoices=self.get_invoices(),
            col_mapping=self.get_col_mapping(),
            year=self._year,
            month=self._month,
            excluded_dates=self.get_excluded_dates(),
            roster_headers=self.get_roster_headers(),
            roster_path=self.get_roster_path(),
        )
