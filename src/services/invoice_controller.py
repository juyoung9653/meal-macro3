"""청구서 컨트롤러 — db.xlsx 기반 단순화. 배송전체 시트에서 직접 O/X 집계."""

import logging
import os
from dataclasses import dataclass, field
from datetime import date, timedelta

from ..core.date_utils import to_bool
from ..core.invoice_data import EnrichedPersonDict
from ..core.meal_types import MealType
from ..core.models import build_enriched_person_dict
from ..excel._parsing import _is_date_col
from ..excel.data_source import DataSource
from ..excel.db_reader import get_db_reader
from ..generators.invoice_generator import make_invoice

# ── 요일패턴 → 실제 날짜열로 변환 ──
_WEEKDAY_KR = {"월": 0, "화": 1, "수": 2, "목": 3, "금": 4, "토": 5, "일": 6}


def _expand_pattern_to_date_str(
    apply_date: date | None,
    end_date: date | None,
    pattern: str,
) -> str | None:
    """요일패턴(월수목) → 실제 날짜 문자열 '6/15, 6/17~6/18'.

    apply_date/end_date 둘 다 있어야 확장. 없으면 None.
    """
    if not apply_date or not end_date or not pattern:
        return None
    days = {_WEEKDAY_KR[ch] for ch in pattern if ch in _WEEKDAY_KR}
    if not days:
        return None

    # 범위 내 해당 요일만 추출
    dates: list[date] = []
    cur = apply_date
    while cur <= end_date:
        if cur.weekday() in days:
            dates.append(cur)
        cur += timedelta(days=1)

    if not dates:
        return None

    # 연속일끼리 묶음
    ranges: list[tuple[date, date]] = []
    s = e = dates[0]
    for d in dates[1:]:
        if d == e + timedelta(days=1):
            e = d
        else:
            ranges.append((s, e))
            s = e = d
    ranges.append((s, e))

    parts = []
    for s, e in ranges:
        if s == e:
            parts.append(f"{s.month}/{s.day}")
        else:
            parts.append(f"{s.month}/{s.day}~{e.month}/{e.day}")
    return ", ".join(parts)


# ── 접수장 전처리: 중지→배송 연속 쌍 → 일시중지 병합 ──
def _merge_stop_resume_to_tempsuspend(
    entries: list[dict],
) -> list[dict]:
    """같은 식종 내 중지→배송 순서쌍을 일시중지로 합침.

    중지(적용일=A) → 배송(적용일=B, B>=A) 이면
    → 일시중지(적용일=A, 종료일=B) 로 대체.
    """
    if not entries:
        return []

    # 식종별 그룹 → 정렬
    by_meal: dict[str, list[dict]] = {}
    for e in entries:
        meal = str(e.get("식종", "")).strip()
        by_meal.setdefault(meal, []).append(e)

    result = []
    for meal, meal_entries in by_meal.items():
        sorted_entries = sorted(
            meal_entries,
            key=lambda r: r.get("적용일") or date.min,
        )
        i = 0
        while i < len(sorted_entries):
            cur = sorted_entries[i]
            cur_status = str(cur.get("상태", "")).strip()

            if cur_status == "중지" and i + 1 < len(sorted_entries):
                nxt = sorted_entries[i + 1]
                nxt_status = str(nxt.get("상태", "")).strip()
                if nxt_status == "배송":
                    # 중지 → 배송 → 일시중지
                    merged = dict(cur)
                    merged["상태"] = "일시중지"
                    merged["종료일"] = nxt.get("적용일")
                    result.append(merged)
                    i += 2
                    continue

            result.append(cur)
            i += 1

    return result


log = logging.getLogger(__name__)


@dataclass
class InvoiceGenerationConfig:
    """Configuration for invoice generation."""

    roster_data: list[dict]
    template_path: str
    output_path: str
    year: int
    month: int
    col_mapping: dict = field(default_factory=dict)
    excluded_dates: set[date] = field(default_factory=set)
    roster_headers: list[str] = field(default_factory=list)
    template_columns: list[str] = field(default_factory=list)
    db_path: str | None = None
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
    enriched: list[EnrichedPersonDict] = field(default_factory=list)
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

    def __init__(self, data_source: DataSource | None = None):
        self._data_source = data_source

    def _get_reader(self, db_path: str | None) -> DataSource:
        if self._data_source is not None:
            return self._data_source
        return get_db_reader(db_path)

    def generate(self, config: InvoiceGenerationConfig) -> InvoiceResult:
        warnings: list[InvoiceWarning] = []

        if not config.roster_data:
            return InvoiceResult(success=False, error="명단 데이터가 없습니다.")

        reader = self._get_reader(config.db_path)
        order_map = reader.build_order_map()

        # 식종정보 테이블에서 단가 읽기
        prices = reader.read_meal_prices()
        breakfast_price = prices.get("조식", 9500)
        dinner_price = prices.get("석식", 9500)
        lunch_price = prices.get("중식", 9500)

        # 접수장 데이터 로드 (비고용)
        reception = reader.read_reception()

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
            config.roster_data[0] if config.roster_data else {},
            "조식 여부",
            "조식여부",
            "조식",
        )
        dinner_col = _find_col(
            config.roster_data[0] if config.roster_data else {},
            "석식 여부",
            "석식여부",
            "석식",
        )
        lunch_col = _find_col(
            config.roster_data[0] if config.roster_data else {},
            "중식 여부",
            "중식여부",
            "중식",
        )
        note_col = _find_col(
            config.roster_data[0] if config.roster_data else {}, "비고"
        )

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

        is_vacation_month = self._is_vacation_month(reader, config.year, config.month)

        enriched: list[EnrichedPersonDict] = []
        for person in config.roster_data:
            person_id = str(person.get("ID", "")).strip()
            if not person_id:
                person_id = str(person.get("id", "")).strip()

            flag_bf = to_bool(person.get(breakfast_col, 0))
            flag_dn = to_bool(person.get(dinner_col, 0))
            flag_lh = to_bool(person.get(lunch_col, 0))
            note_val = str(person.get(note_col, "")).strip()
            if note_val in ("비고", ""):
                note_val = ""

            # ── 접수장 기반 비고 메모 ──
            person_receptions = [
                r for r in reception if str(r.get("ID", "")).strip() == person_id
            ]
            # 중지→배송 쌍 → 일시중지 병합
            person_receptions = _merge_stop_resume_to_tempsuspend(person_receptions)

            # 식종이 모두 같은지 확인 (같으면 prefix 생략)
            meal_set = {
                str(r.get("식종", "")).strip()
                for r in person_receptions
                if str(r.get("식종", "")).strip()
            }
            show_meal_prefix = len(meal_set) > 1

            memo_lines = []
            for r in person_receptions:
                status = str(r.get("상태", "")).strip()
                if not status:
                    continue
                apply_date = r.get("적용일")
                end_date = r.get("종료일")
                meal = str(r.get("식종", "")).strip() if show_meal_prefix else ""
                pattern = str(r.get("요일패턴", "") or "").strip()

                if apply_date:
                    # 요일패턴 있으면 실제 날짜로 확장
                    expanded = _expand_pattern_to_date_str(
                        apply_date, end_date, pattern
                    )
                    if expanded:
                        ds = expanded
                    else:
                        ds = f"{apply_date.month}/{apply_date.day}"
                        if end_date and end_date != apply_date:
                            ds += f"~{end_date.month}/{end_date.day}"
                        elif not end_date and status in ("배송", "중지"):
                            ds += "~"

                    parts = []
                    if meal:
                        parts.append(f"{meal}:")
                    parts.append(f"{ds} {status}")
                    memo_lines.append(" ".join(parts))
                else:
                    parts = []
                    if meal:
                        parts.append(f"{meal}:")
                    parts.append(status)
                    memo_lines.append(" ".join(parts))
            reception_memo = "\n".join(memo_lines)

            # 배송전체에서 집계된 카운트 가져오기 (식종명 유연 매칭)
            counts = meal_counts_by_id.get(person_id, {})
            bf_cnt = dn_cnt = lh_cnt = 0
            if flag_bf:
                for meal, cnt in counts.items():
                    if MealType.from_name(meal) == MealType.BREAKFAST:
                        bf_cnt += cnt
            if flag_dn:
                for meal, cnt in counts.items():
                    if MealType.from_name(meal) == MealType.DINNER:
                        dn_cnt += cnt
            if flag_lh:
                for meal, cnt in counts.items():
                    if MealType.from_name(meal) == MealType.LUNCH:
                        lh_cnt += cnt

            # 배송전체는 Power Query가 이미 모든 휴무일/제외일을 반영한 최종 결과.
            # 추가 차감하지 않음. (사용자 수동 제외일만 적용)

            ep_dict = build_enriched_person_dict(
                raw=dict(person),
                name=str(person.get("성명", "")),
                dept=str(person.get("학교명", "")),
                flag_breakfast=flag_bf,
                flag_dinner=flag_dn,
                flag_lunch=flag_lh,
                note=note_val,
            )
            ep_dict["_reception_memo"] = reception_memo
            ep_dict["_bf_cnt"] = bf_cnt
            ep_dict["_dn_cnt"] = dn_cnt
            ep_dict["_lh_cnt"] = lh_cnt
            ep_dict["_delivery_start"] = ""
            ep_dict["_delivery_end"] = ""

            # 배송순서/배송담당 매핑 (핸드폰+도로명주소 기준 조인)
            phone = (
                str(person.get("핸드폰", "")).strip().replace("-", "").replace(" ", "")
            )
            addr = str(person.get("도로명주소", "")).strip()
            order_key = f"{phone}|{addr}"
            order = order_map.get(order_key, {})
            ep_dict["배송순서"] = order.get("배송순서", "")
            ep_dict["배송담당"] = order.get("배송담당", "")
            if "핸드폰" not in ep_dict or not ep_dict["핸드폰"]:
                ep_dict["핸드폰"] = order.get("핸드폰", phone)

            # 주차별 계산용 일별 raw 데이터 보존 (모든 row 저장, 식종별 멀티 행 대응)
            delivery_rows = [
                r
                for r in reader.read_delivery_matrix()
                if str(r.get("ID", "")).strip() == person_id
            ]
            if delivery_rows:
                ep_dict["_daily"] = {
                    c: v for c, v in delivery_rows[0].items() if _is_date_col(c)
                }
                ep_dict["_delivery_rows_raw"] = [
                    dict(r, _daily={c: v for c, v in r.items() if _is_date_col(c)})
                    for r in delivery_rows
                ]

            enriched.append(ep_dict)

        if not os.path.exists(config.template_path):
            return InvoiceResult(
                success=False,
                error=f"템플릿 파일 없음:\n{config.template_path}",
            )

        try:
            cnt, enriched = make_invoice(
                enriched,
                config.col_mapping,
                config.output_path,
                template_path=config.template_path,
                year=config.year,
                month=config.month,
                unit_price_breakfast=breakfast_price,
                unit_price_dinner=dinner_price,
                unit_price_lunch=lunch_price,
                excluded_dates=config.excluded_dates.copy()
                if config.excluded_dates
                else None,
                is_vacation_month=is_vacation_month,
                meal_type=config.meal_type,
            )
        except Exception as e:
            return InvoiceResult(success=False, error=f"청구서 생성 실패:\n{e}")

        return InvoiceResult(
            success=True, count=cnt, enriched=enriched, warnings=warnings
        )

    @staticmethod
    def _is_vacation_month(reader, year: int, month: int) -> bool:
        """배송전체에 중식/방중 건수가 1건이라도 있으면 방학월."""
        try:
            for row in reader.read_delivery_matrix():
                meal = str(row.get("식종", "")).strip()
                if MealType.from_name(meal) == MealType.LUNCH:
                    from ..excel._parsing import _is_date_col

                    if any(
                        str(v).strip() in ("1", "O")
                        for c, v in row.items()
                        if _is_date_col(c)
                    ):
                        return True
        except Exception:
            pass
        return False
