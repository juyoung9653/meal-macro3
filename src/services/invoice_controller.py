"""청구서 컨트롤러 — db.xlsx 기반 단순화. 배송전체 시트에서 직접 O/X 집계."""

import logging
import os
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from PyQt6.QtWidgets import QMessageBox, QWidget

from ..core.models import EnrichedPerson, RosterPerson, person_to_dict
from ..core.settings import get_settings
from ..excel.db_reader import _is_date_col, get_db_reader
from ..generators.invoice_generator import make_invoice
from ..templates.config import auto_detect_semantic_columns

log = logging.getLogger(__name__)


@dataclass
class InvoiceWarning:
    code: str
    message: str
    details: dict = field(default_factory=dict)


@dataclass
class InvoiceResult:
    success: bool
    count: int = 0
    enriched: list[dict] = field(default_factory=list)
    warnings: list[InvoiceWarning] = field(default_factory=list)
    error: str | None = None


def _count_delivery_from_row(row: dict) -> int:
    """배송전체 row에서 날짜 컬럼의 1/O 개수 합산 (식종 구분 없이)."""
    count = 0
    for col_name, val in row.items():
        if _is_date_col(col_name):
            v = str(val).strip().upper()
            if v in ("O", "1", "1.0"):
                count += 1
    return count


class InvoiceService:
    """Pure business logic. db.xlsx 배송전체 시트에서 직접 O/X 집계."""

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
        db_path: str | None = None,
    ) -> InvoiceResult:
        warnings: list[InvoiceWarning] = []

        if not roster_data:
            return InvoiceResult(success=False, error="명단 데이터가 없습니다.")

        reader = get_db_reader(db_path)
        order_map = reader.build_order_map()

        # 명단 시트 기준 고정 컬럼명 (공백/개행 정규화)
        def _find_col(person: dict, *candidates: str) -> str:
            for c in candidates:
                if c in person:
                    return c
            # 공백 무시 매칭
            normalized = {k.replace(" ", ""): k for k in person}
            for c in candidates:
                nc = c.replace(" ", "")
                if nc in normalized:
                    return normalized[nc]
            return candidates[0] if candidates else ""

        breakfast_col = _find_col(
            roster_data[0] if roster_data else {}, "조식 여부", "조식여부", "조식"
        )
        dinner_col = _find_col(
            roster_data[0] if roster_data else {}, "석식 여부", "석식여부", "석식"
        )
        lunch_col = _find_col(
            roster_data[0] if roster_data else {}, "중식 여부", "중식여부", "중식"
        )
        note_col = _find_col(roster_data[0] if roster_data else {}, "비고")

        # 배송전체 식종별 카운트 미리 집계: {ID: {"조식": cnt, "중식": cnt, "석식": cnt}}
        meal_counts_by_id: dict[str, dict[str, int]] = {}
        for row_data in reader.read_delivery_matrix():
            pid = str(row_data.get("ID", "")).strip()
            if not pid:
                continue
            meal = str(row_data.get("식종", "")).strip()
            cnt = _count_delivery_from_row(row_data)
            if pid not in meal_counts_by_id:
                meal_counts_by_id[pid] = {}
            # 같은 ID+식종이 여러 행이면 최대값 사용
            prev = meal_counts_by_id[pid].get(meal, 0)
            meal_counts_by_id[pid][meal] = max(prev, cnt)

        is_vacation_month = self._is_vacation_month(reader, year, month)

        enriched: list[dict] = []
        for person in roster_data:
            person_id = str(person.get("ID", "")).strip()
            if not person_id:
                person_id = str(person.get("id", "")).strip()

            flag_bf = self._to_bool(person.get(breakfast_col, 0))
            flag_dn = self._to_bool(person.get(dinner_col, 0))
            flag_lh = self._to_bool(person.get(lunch_col, 0))
            note_val = str(person.get(note_col, "")).strip()
            if note_val in ("비고", ""):
                note_val = ""

            # 배송전체에서 집계된 카운트 가져오기 (식종명 유연 매칭)
            counts = meal_counts_by_id.get(person_id, {})
            bf_cnt = dn_cnt = lh_cnt = 0
            if flag_bf:
                for meal, cnt in counts.items():
                    if "조식" in meal:
                        bf_cnt += cnt
            if flag_dn:
                for meal, cnt in counts.items():
                    if "석식" in meal:
                        dn_cnt += cnt
            if flag_lh:
                for meal, cnt in counts.items():
                    if "중식" in meal or "방중" in meal:
                        lh_cnt += cnt

            # 배송전체는 Power Query가 이미 모든 휴무일/제외일을 반영한 최종 결과.
            # 추가 차감하지 않음. (사용자 수동 제외일만 적용)

            rp = RosterPerson(
                raw=dict(person),
                name=str(person.get("성명", "")),
                dept=str(person.get("학교명", "")),
            )
            ep = EnrichedPerson(
                person=rp,
                flag_breakfast=flag_bf,
                flag_dinner=flag_dn,
                flag_lunch=flag_lh,
                note=note_val,
            )
            ep_dict = person_to_dict(ep)
            ep_dict["_bf_cnt"] = bf_cnt
            ep_dict["_dn_cnt"] = dn_cnt
            ep_dict["_lh_cnt"] = lh_cnt
            ep_dict["_delivery_start"] = ""
            ep_dict["_delivery_end"] = ""

            # 배송순서/배송담당 매핑 (핸드폰+도로명주소 기준 조인)
            phone = str(person.get("핸드폰", "")).strip()
            addr = str(person.get("도로명주소", "")).strip()
            order_key = f"{phone}|{addr}"
            order = order_map.get(order_key, {})
            ep_dict["배송순서"] = order.get("배송순서", "")
            ep_dict["배송담당"] = order.get("배송담당", "")
            if "핸드폰" not in ep_dict or not ep_dict["핸드폰"]:
                ep_dict["핸드폰"] = order.get("핸드폰", phone)

            # 주차별 계산용 일별 raw 데이터 보존 (첫 번째 row 사용)
            delivery_rows = [
                r
                for r in reader.read_delivery_matrix()
                if str(r.get("ID", "")).strip() == person_id
            ]
            if delivery_rows:
                ep_dict["_daily"] = {
                    c: v for c, v in delivery_rows[0].items() if _is_date_col(c)
                }

            enriched.append(ep_dict)

        if not os.path.exists(template_path):
            return InvoiceResult(
                success=False,
                error=f"템플릿 파일 없음:\n{template_path}",
            )

        try:
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
            return InvoiceResult(success=False, error=f"청구서 생성 실패:\n{e}")

        return InvoiceResult(
            success=True, count=cnt, enriched=enriched, warnings=warnings
        )

    @staticmethod
    def _to_bool(val: Any) -> bool:
        if isinstance(val, bool):
            return val
        if isinstance(val, (int, float)):
            return val > 0
        if isinstance(val, str):
            v = val.strip().upper()
            return v in ("O", "Y", "1", "1.0", "예", "있음", "TRUE", "급식")
        return bool(val)

    @staticmethod
    def _is_vacation_month(reader, year: int, month: int) -> bool:
        """배송전체에 중식/방중 건수가 1건이라도 있으면 방학월."""
        try:
            for row in reader.read_delivery_matrix():
                meal = str(row.get("식종", "")).strip()
                if "중식" in meal or "방중" in meal:
                    from ..excel.db_reader import _is_date_col

                    if any(
                        str(v).strip() in ("1", "O")
                        for c, v in row.items()
                        if _is_date_col(c)
                    ):
                        return True
        except Exception:
            pass
        return False


class InvoiceController:
    """UI-aware wrapper."""

    def __init__(self, parent: QWidget, template_columns: list[str]):
        self._parent = parent
        self._service = InvoiceService()
        self.invoices: list[dict] = []
        self.output_dir: str | None = None
        self.roster_headers: list[str] = []
        self.roster_path: str | None = None
        self._template_columns = template_columns
        self._col_mapping: dict = {col: "" for col in template_columns}
        _sem = auto_detect_semantic_columns(template_columns)
        self._col_mapping.update(_sem)
        self._col_mapping.update(
            {"__breakfast": "", "__dinner": "", "__vacation": "", "__note": ""}
        )
        self._excluded_dates: set[date] = set()
        self._year: int = 0
        self._month: int = 0
        self._db_path: str = get_settings().get_db_path()

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
            db_path=self._db_path,
        )

        for w in result.warnings:
            QMessageBox.warning(self._parent, w.code, w.message)

        if not result.success and result.error:
            QMessageBox.critical(self._parent, "오류", result.error)
            return False, 0, []

        self.invoices = result.enriched
        return result.success, result.count, result.enriched

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

    def set_db_path(self, path: str):
        self._db_path = path
        get_db_reader(path)

    def get_db_path(self) -> str:
        return self._db_path

    def set_col_mapping(self, mapping: dict):
        self._col_mapping = dict(mapping)

    def set_excluded_dates(self, dates: set[date]):
        self._excluded_dates = set(dates)

    def add_excluded_date(self, d: date):
        self._excluded_dates.add(d)

    def clear_excluded_dates(self):
        self._excluded_dates.clear()

    def set_roster_headers(self, headers: list[str]):
        self.roster_headers = list(headers)
        self._auto_map_columns()

    def _auto_map_columns(self):
        """템플릿 컬럼 ↔ 명단 컬럼 자동 매칭 (공백/개행 무시)."""
        if not self.roster_headers:
            return
        roster_normalized = {
            h.replace(" ", "").replace("\n", ""): h for h in self.roster_headers
        }
        for tcol in list(self._col_mapping.keys()):
            if tcol.startswith("__"):
                continue  # semantic keys are handled separately
            tnorm = tcol.replace(" ", "").replace("\n", "")
            if tnorm in roster_normalized:
                self._col_mapping[tcol] = roster_normalized[tnorm]

    def set_roster_path(self, path: str | None):
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
