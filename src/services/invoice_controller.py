"""청구서 컨트롤러 — 데이터 보강, 배송 계산, 청구서 생성 오케스트레이션"""

import os
import re
from datetime import date

from PyQt6.QtWidgets import QMessageBox, QWidget

from ..core.models import EnrichedPerson, RosterPerson, person_to_dict
from ..generators.invoice_generator import make_invoice
from ..templates.config import auto_detect_semantic_columns
from .delivery_calculator import to_bool


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
    ) -> tuple[bool, int, list[dict]]:
        """명단 데이터 → 보강 → make_invoice → (성공여부, 건수, 보강데이터).

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

        enriched: list[dict] = []
        for person in roster_data:
            val_y = str(person.get(y_key, "")) if y_key else ""
            if "중지" in val_y or "중단" in val_y:
                continue
            note_val = str(person.get(note_col, ""))
            if not re.search(r"\d{1,2}[/.]\d{1,2}|매주", note_val):
                note_val = ""

            rp = RosterPerson(
                raw=dict(person),
                name=str(person.get("성명", "")),
                dept=str(person.get("학교명", "")),
            )
            ep = EnrichedPerson(
                person=rp,
                flag_breakfast=to_bool(person.get(breakfast_col, 0)),
                flag_dinner=to_bool(person.get(dinner_col, 0)),
                flag_lunch=to_bool(person.get(vacation_col, 0)),
                note=note_val,
            )
            enriched.append(person_to_dict(ep))

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
