"""청구서 컨트롤러 — 데이터 보강, 배송 계산, 청구서 생성 오케스트레이션"""

import logging
import os
import re
from datetime import date

from PyQt6.QtWidgets import QMessageBox, QWidget

from ..core.models import EnrichedPerson, RosterPerson, person_to_dict
from ..generators.invoice_generator import make_invoice
from ..templates.config import auto_detect_semantic_columns
from .delivery_calculator import count_lunch_vacation_days_with_note, to_bool
from .lunch_data import LunchRoster
from .lunch_parser import parse_lunch_note

log = logging.getLogger(__name__)


class InvoiceController:
    """청구서 생성 비즈니스 로직. UI 없이 테스트 가능."""

    def __init__(self, parent: QWidget, template_columns: list[str]):
        self._parent = parent
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

    # ── 비즈니스 로직 ──

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
        """명단 데이터 → 보강 → make_invoice → (성공여부, 건수, 보강데이터).

        has_lunch_file=True + 중식 명단 로드됨 → 방학 일정 기반 중식 배송일 계산.
        has_lunch_file=False → 중식 미사용 (flag_lunch=0).
        실패 시 에러 대화상자를 표시하고 (False, 0, []) 반환.
        """
        self._year = year
        self._month = month
        if not roster_data:
            QMessageBox.warning(self._parent, "알림", "명단 데이터가 없습니다.")
            return False, 0, []

        breakfast_col = self._col_mapping.get("__breakfast", "")
        dinner_col = self._col_mapping.get("__dinner", "")
        vacation_col = self._col_mapping.get("__vacation", "")
        note_col = self._col_mapping.get("__note", "")

        y_key = next(
            (h for h in self.roster_headers if "중지" in h or "중단" in h), None
        )

        # ── 중식 명단: 학교명 집합 수집 + 미매칭 경고 ──
        lunch_available = has_lunch_file and self._lunch_roster is not None
        unmatched_schools: set[str] = set()
        lunch_warned: set[str] = set()

        if lunch_available:
            roster_school_col = self._lunch_mapping.get("roster_school_col", "")
            roster_grade_col = self._lunch_mapping.get("roster_grade_col", "")
            # 명단의 고유 학교명 수집
            roster_schools: set[str] = set()
            for person in roster_data:
                school_name = str(person.get(roster_school_col, "")).strip()
                if school_name:
                    roster_schools.add(school_name)
            unmatched_schools = self._lunch_roster.get_unmatched_schools(roster_schools)

            # ── 중식명단 기반 방학 월 판단 (경고 조건에 사용) ──
            is_vacation_month = self._lunch_roster.is_vacation_month(year, month)
        else:
            is_vacation_month = False

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
            if lunch_available:
                has_lunch_flag = to_bool(person.get(vacation_col, 0))
                if has_lunch_flag:
                    school_name = str(person.get(roster_school_col, "")).strip()
                    lunch_entry = self._lunch_roster.find_school(school_name)

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
                            excluded_dates=self._excluded_dates.copy()
                            if self._excluded_dates
                            else None,
                        )
                else:
                    # 방중 플래그 꺼짐 → 중식 0
                    pass
            else:
                # 중식 미사용
                pass

            rp = RosterPerson(
                raw=dict(person),
                name=str(person.get("성명", "")),
                dept=str(person.get("학교명", "")),
            )
            ep = EnrichedPerson(
                person=rp,
                flag_breakfast=to_bool(person.get(breakfast_col, 0)),
                flag_dinner=to_bool(person.get(dinner_col, 0)),
                flag_lunch=lunch_days > 0,
                note=note_val,
            )
            # 중식 배송일 수를 직접 저장 (count_delivery_days에서 재계산하지 않도록)
            ep_dict = person_to_dict(ep)
            ep_dict["_lunch_days"] = lunch_days
            enriched.append(ep_dict)

        # ── 중식 미매칭 학교 경고 (방학 기간에만 표시) ──
        if lunch_warned and is_vacation_month:
            school_list = "\n".join(f"  • {s}" for s in sorted(lunch_warned))
            QMessageBox.warning(
                self._parent,
                "중식 명단 미매칭",
                f"다음 학교는 중식 명단에서 찾을 수 없습니다.\n"
                f"해당 학교 학생들의 중식 배송일은 0으로 처리됩니다:\n\n"
                f"{school_list}",
            )

        if not os.path.exists(template_path):
            QMessageBox.critical(
                self._parent,
                "파일 없음",
                f"템플릿 파일을 찾을 수 없습니다:\n{template_path}",
            )
            return False, 0, []

        try:
            # 계산값 입력 대상은 항상 auto-detect (GUI 불필요)
            _sem = auto_detect_semantic_columns(self._template_columns)
            col_mapping = {**self._col_mapping, **_sem}

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
                excluded_dates=self._excluded_dates.copy()
                if self._excluded_dates
                else None,
                is_vacation_month=is_vacation_month,
            )
        except Exception as e:
            QMessageBox.critical(self._parent, "생성 오류", f"청구서 생성 실패:\n{e}")
            return False, 0, []

        self.invoices = enriched
        return True, cnt, enriched

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
