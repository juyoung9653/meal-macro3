"""청구서 컨트롤러 — 데이터 보강, 배송 계산, 청구서 생성 오케스트레이션"""

import logging
import os
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from PyQt6.QtWidgets import QMessageBox, QWidget

from ..core.models import EnrichedPerson, RosterPerson, person_to_dict
from ..generators.invoice_generator import make_invoice
from ..templates.config import auto_detect_semantic_columns
from .delivery_calculator import (
    count_delivery_days,
    count_lunch_vacation_days_with_note,
    to_bool,
)
from .lunch_data import LunchRoster
from .lunch_parser import parse_lunch_note

log = logging.getLogger(__name__)


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
    ) -> InvoiceResult:
        """명단 데이터 → 보강 → make_invoice → InvoiceResult.

        has_lunch_file=True + lunch_roster 제공 → 방학 일정 기반 중식 배송일 계산.
        has_lunch_file=False → 중식 미사용 (flag_lunch=0).
        실패 시 error 필드에 메시지를 담아 반환 (UI 대화상자 없음).

        Returns:
            InvoiceResult(success=True, count=N, enriched=[...], warnings=[...])  성공
            InvoiceResult(success=False, error="...")  실패
        """
        warnings: list[InvoiceWarning] = []

        if not roster_data:
            return InvoiceResult(success=False, error="명단 데이터가 없습니다.")

        breakfast_col = col_mapping.get("__breakfast", "")
        dinner_col = col_mapping.get("__dinner", "")
        vacation_col = col_mapping.get("__vacation", "")
        note_col = col_mapping.get("__note", "")

        y_key = next((h for h in roster_headers if "중지" in h or "중단" in h), None)

        # ── 중식 명단: 학교명 집합 수집 + 미매칭 경고 ──
        lunch_available = has_lunch_file and lunch_roster is not None
        lunch_warned: set[str] = set()

        # ── 중식명단 기반 방학 월 판단 (경고 조건에 사용) ──
        is_vacation_month = bool(
            lunch_roster and lunch_roster.is_vacation_month(year, month)
        )

        if lunch_available and lunch_roster is not None and lunch_mapping is not None:
            roster_school_col = lunch_mapping.get("roster_school_col", "")
            roster_grade_col = lunch_mapping.get("roster_grade_col", "")
            # 명단의 고유 학교명 수집
            roster_schools: set[str] = set()
            for person in roster_data:
                school_name = str(person.get(roster_school_col, "")).strip()
                if school_name:
                    roster_schools.add(school_name)
            unmatched_schools = lunch_roster.get_unmatched_schools(roster_schools)

        enriched: list[dict] = []
        for person in roster_data:
            val_y = str(person.get(y_key, "")) if y_key else ""
            if "중지" in val_y or "중단" in val_y:
                continue
            note_val = str(person.get(note_col, ""))
            if not re.search(r"\d{1,2}[/.]\d{1,2}|매주", note_val):
                note_val = ""

            # ── 중식 배송일 계산 ──
            lunch_days = 0
            has_lunch_flag = False
            if (
                lunch_available
                and lunch_roster is not None
                and lunch_mapping is not None
            ):
                has_lunch_flag = to_bool(person.get(vacation_col, 0))
                if has_lunch_flag:
                    school_name = str(
                        person.get(lunch_mapping.get("roster_school_col", ""), "")
                    ).strip()
                    lunch_entry = lunch_roster.find_school(school_name)

                    if lunch_entry is None:
                        # 중식 명단에 학교 없음 → 경고 (1회만)
                        if school_name and school_name not in lunch_warned:
                            lunch_warned.add(school_name)
                            log.warning(
                                "[Lunch] 학교 '%s'가 중식 명단에 없습니다. 중식 배송일=0",
                                school_name,
                            )
                    else:
                        # 학년 추출
                        grade = 0
                        roster_grade_col = lunch_mapping.get("roster_grade_col", "")
                        if roster_grade_col:
                            grade_str = str(person.get(roster_grade_col, "0")).strip()
                            try:
                                grade = int(re.search(r"\d+", grade_str).group())
                            except (ValueError, AttributeError):
                                pass

                        lunch_days = count_lunch_vacation_days_with_note(
                            year,
                            month,
                            lunch_entry,
                            grade=grade,
                            excluded_dates=excluded_dates.copy()
                            if excluded_dates
                            else None,
                        )

            # ── 배송일 1회 계산 (조식/석식/중식) ──
            flag_bf = to_bool(person.get(breakfast_col, 0))
            flag_dn = to_bool(person.get(dinner_col, 0))
            flag_lh_for_calc = lunch_days > 0  # 방학 반영된 중식 플래그

            days = count_delivery_days(
                year,
                month,
                flag_bf,
                flag_dn,
                flag_lh_for_calc,
                note_val,
                excluded_dates=excluded_dates.copy() if excluded_dates else None,
            )

            # 방학/중식명단이 있는 경우 중식 배송일은 lunch_days 우선
            lh_cnt = lunch_days if lunch_available and has_lunch_flag else days["lunch"]

            rp = RosterPerson(
                raw=dict(person),
                name=str(person.get("성명", "")),
                dept=str(person.get("학교명", "")),
            )
            ep = EnrichedPerson(
                person=rp,
                flag_breakfast=flag_bf,
                flag_dinner=flag_dn,
                flag_lunch=lunch_days > 0,
                note=note_val,
            )
            ep_dict = person_to_dict(ep)
            # 사전 계산된 배송일 수 저장 (재계산 불필요)
            ep_dict["_bf_cnt"] = days["breakfast"]
            ep_dict["_dn_cnt"] = days["dinner"]
            ep_dict["_lh_cnt"] = lh_cnt
            enriched.append(ep_dict)

        # ── 중식 미매칭 학교 경고 (방학 기간에만 표시) ──
        if lunch_warned and is_vacation_month:
            school_list = "\n".join(f"  • {s}" for s in sorted(lunch_warned))
            warnings.append(
                InvoiceWarning(
                    code="unmatched_school",
                    message=(
                        f"다음 학교는 중식 명단에서 찾을 수 없습니다.\n"
                        f"해당 학교 학생들의 중식 배송일은 0으로 처리됩니다:\n\n"
                        f"{school_list}"
                    ),
                    details={"schools": sorted(lunch_warned)},
                )
            )

        if not os.path.exists(template_path):
            return InvoiceResult(
                success=False,
                error=f"템플릿 파일을 찾을 수 없습니다:\n{template_path}",
            )

        try:
            log.info(
                "[Invoice] lunch_available=%s is_vacation_month=%s",
                lunch_available,
                is_vacation_month,
            )

            # 중식명단 없는데 방중 플래그 있는 경우 경고
            if not lunch_available and vacation_col:
                has_vac_flag = any(
                    to_bool(person.get(vacation_col, 0)) for person in roster_data
                )
                if has_vac_flag:
                    log.warning(
                        "[Invoice] 방중 플래그 있으나 중식명단 미로드 → 학기_ 시트 사용"
                    )

            cnt, enriched = make_invoice(
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
            )
        except Exception as e:
            return InvoiceResult(
                success=False,
                error=f"청구서 생성 실패:\n{e}",
            )

        return InvoiceResult(
            success=True,
            count=cnt,
            enriched=enriched,
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
        self._excluded_dates: set[date] = set()
        self._year: int = 0
        self._month: int = 0
        # 중식 명단
        self._lunch_roster: LunchRoster | None = None
        self._lunch_mapping: dict = {}
        self._lunch_file_path: str = ""

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
        """중식 명단 데이터 설정."""
        self._lunch_roster = roster
        self._lunch_mapping = mapping
        self._lunch_file_path = file_path

    def get_lunch_roster(self):
        return self._lunch_roster

    def get_lunch_mapping(self) -> dict:
        return dict(self._lunch_mapping)

    # ── Setters ──

    def set_col_mapping(self, mapping: dict):
        """컬럼 매핑 설정."""
        self._col_mapping = dict(mapping)

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
