"""배송 리포트 집계 코어 — DataSource만 의존, pandas/numpy 불필요."""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from ..core.diff_format import fmt_cell
from ..core.meal_types import ERRORS, MEAL_LABELS, MEAL_RULES, MealType
from ..excel._parsing import _is_date_col, _parse_date_col
from ..excel.data_source import DataSource


@dataclass
class StaffMemo:
    """Aggregated memo for a delivery staff member."""

    memo_text: str
    area_groups: dict[str, set[str]] = field(
        default_factory=dict
    )  # area_group -> {names}


log = logging.getLogger(__name__)


def _date_to_str(d: date) -> str:
    return d.strftime("%Y%m%d")


def _str_to_date(s: str) -> date | None:
    try:
        return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    except (ValueError, IndexError):
        return None


def _date_to_md(d: date) -> str:
    return f"{d.month}/{d.day}"


class DeliveryService:
    """배송 리포트 집계 서비스. DataSource 인터페이스에만 의존."""

    def __init__(self, data_source: DataSource, staff_sort: dict | None = None):
        self._ds = data_source
        self._staff_sort = staff_sort or {}

    def build_report(self, target_day: date, meal: str) -> list[list]:
        """단일 날짜 배송 리포트 생성.

        Returns:
            [헤더행, 데이터행..., 합계행]. 오류 시 [[오류메시지]].
        """
        if meal not in MEAL_RULES:
            return [[ERRORS["식종불일치"]]]

        dow = target_day.weekday()
        if dow not in MEAL_RULES[meal]:
            day_name = "월화수목금토일"[dow]
            return [
                [
                    ERRORS["배송일아님"].format(
                        target_day.strftime("%m/%d"), day_name, meal
                    )
                ]
            ]

        # ── 데이터 수집 ──
        delivery = self._ds.read_delivery_matrix()
        roster = self._ds.read_roster()
        order_map = self._ds.build_order_map()
        holiday_set = self._ds.get_holiday_set()
        reception = self._ds.read_reception()

        if not delivery:
            return [[ERRORS["날짜없음"].format(_date_to_str(target_day))]]

        # ── 날짜 컬럼명 찾기 ──
        target_md = _date_to_md(target_day)
        date_col = None
        for col in delivery[0].keys():
            if _is_date_col(col):
                parsed = _parse_date_col(col)
                if parsed and parsed == (target_day.month, target_day.day):
                    date_col = col
                    break
        if date_col is None:
            return [[ERRORS["날짜없음"].format(_date_to_str(target_day))]]

        # ── 명단 인덱스 (ID → person info) ──
        roster_by_id: dict[str, dict] = {}
        for p in roster:
            pid = str(p.get("ID", "")).strip()
            if pid:
                roster_by_id[pid] = p

        # ── 타겟 식종 라벨 ──
        target_label = MEAL_LABELS[meal]

        # ── 배송일 목록 찾기 ──
        delivery_dates = self._get_delivery_dates(
            delivery, target_day, meal, target_label, holiday_set
        )
        if not delivery_dates:
            return [[ERRORS["배송안함"]]]

        today_date = delivery_dates[-1]
        prev_date = delivery_dates[-2] if len(delivery_dates) > 1 else None

        today_md = _date_to_md(today_date)
        prev_md = _date_to_md(prev_date) if prev_date else None

        # ── 오늘 날짜 컬럼 찾기 ──
        today_col = self._find_date_col(delivery, today_date)
        prev_col = self._find_date_col(delivery, prev_date) if prev_date else None

        # ── 식종 필터링된 행 수집 ──
        meal_rows = [
            r for r in delivery if str(r.get("식종", "")).strip().startswith(meal)
        ]

        # ── 전화번호별 오늘/이전 배송 건수 집계 ──
        today_by_phone: Counter[str] = Counter()
        prev_by_phone: Counter[str] = Counter()

        # 사람별 데이터 수집
        person_today: dict[str, bool] = {}  # phone → is_today_delivery
        person_prev: dict[str, bool] = {}  # phone → is_prev_delivery
        person_staff: dict[str, str] = {}  # phone → staff_name
        person_name: dict[str, str] = {}  # phone → name
        person_dong: dict[str, str] = {}  # phone → dong
        person_addr: dict[str, str] = {}  # phone → address
        phone_to_id: dict[str, str] = {}  # phone → person_id

        for row in meal_rows:
            pid = str(row.get("ID", "")).strip()
            if not pid:
                continue

            rp = roster_by_id.get(pid, {})
            phone = str(rp.get("핸드폰", "")).strip().replace("-", "").replace(" ", "")
            if not phone:
                continue

            addr = str(rp.get("도로명주소", "")).strip()
            order_key = f"{phone}|{addr}"
            order_info = order_map.get(order_key, {})

            staff = str(order_info.get("배송담당", "")).strip()
            if not staff:
                staff = str(rp.get("배송담당", "")).strip()

            name = str(rp.get("성명", "")).strip()
            dong = str(rp.get("관할동", "")).strip()
            addr_val = str(rp.get("도로명주소", "")).strip()

            # 오늘 배송 체크
            if today_col:
                val = str(row.get(today_col, "")).strip().upper()
                if val in ("1", "1.0", "O"):
                    today_by_phone[phone] += 1
                    person_today[phone] = True

            # 이전 배송 체크
            if prev_col:
                val = str(row.get(prev_col, "")).strip().upper()
                if val in ("1", "1.0", "O"):
                    prev_by_phone[phone] += 1
                    person_prev[phone] = True

            # 사람 정보 저장 (첫 번째 항목 우선)
            if phone not in person_staff:
                person_staff[phone] = staff
                person_name[phone] = name
                person_dong[phone] = dong
                person_addr[phone] = addr_val
                phone_to_id[phone] = pid

        # ── 담당자별 집계 ──
        staff_counts: dict[str, list[int]] = defaultdict(list)
        staff_memos: dict[str, list[StaffMemo]] = defaultdict(list)
        staff_areas: dict[str, set[str]] = defaultdict(set)
        prev_buckets: dict[str, list[int]] = defaultdict(lambda: [0] * 5)

        seen_today: dict[str, set[str]] = defaultdict(set)
        seen_prev: dict[str, set[str]] = defaultdict(set)

        for phone, is_today in person_today.items():
            staff = person_staff.get(phone, "")
            if not staff:
                continue

            name = person_name.get(phone, "")
            dong = person_dong.get(phone, "")
            addr = person_addr.get(phone, "")

            # 지역 수집
            if dong and dong not in ("", "nan"):
                staff_areas[staff].add(dong)

            # 오늘 배송 count (중복 전화번호 제외)
            if is_today and phone not in seen_today[staff]:
                seen_today[staff].add(phone)
                staff_counts[staff].append(today_by_phone.get(phone, 0))

            # 이전 배송 count (중복 전화번호 제외)
            if person_prev.get(phone) and phone not in seen_prev[staff]:
                seen_prev[staff].add(phone)
                q = prev_by_phone.get(phone, 0)
                if 1 <= q <= 5:
                    prev_buckets[staff][q - 1] += 1

            # ── 접수장 기반 메모 처리 ──
            pid = phone_to_id.get(phone, "")
            if pid:
                display_memo = self._build_reception_memo(
                    reception, pid, meal, today_date, delivery_dates
                )
                if display_memo:
                    # 지역 그룹 키
                    if dong == "위례동" and addr:
                        gk = _extract_addr_key(addr) or "위례동"
                    else:
                        gk = dong if dong else "미지정"
                    # Find or create StaffMemo with matching memo_text
                    memo_list = staff_memos[staff]
                    found = False
                    for sm in memo_list:
                        if sm.memo_text == display_memo:
                            sm.area_groups.setdefault(gk, set()).add(name)
                            found = True
                            break
                    if not found:
                        memo_list.append(
                            StaffMemo(memo_text=display_memo, area_groups={gk: {name}})
                        )

        # ── 출력 테이블 생성 ──
        return self._build_output_table(
            staff_counts, staff_memos, staff_areas, prev_buckets, meal
        )

    def build_all(
        self, start: date, end: date, meal_filter: str | None = None
    ) -> list[tuple[str, str, list[list]]]:
        """날짜 범위 리포트 생성. 주말/휴무 자동 제외.

        Returns:
            [(date_str, meal, table), ...]
        """
        results: list[tuple[str, str, list[list]]] = []
        holiday_set = self._ds.get_holiday_set()
        current = start
        while current <= end:
            if current in holiday_set:
                current += timedelta(days=1)
                continue

            dow = current.weekday()
            if meal_filter:
                meals_to_check = [meal_filter]
            else:
                meals_to_check = [m for m, dows in MEAL_RULES.items() if dow in dows]

            for meal in meals_to_check:
                table = self.build_report(current, meal)
                results.append((_date_to_str(current), meal, table))

            current += timedelta(days=1)

        return results

    # ── 내부 헬퍼 ──

    @staticmethod
    def _find_date_col(delivery: list[dict], d: date) -> str | None:
        md = _date_to_md(d)
        for col in delivery[0].keys():
            if _is_date_col(col):
                parsed = _parse_date_col(col)
                if parsed and parsed == (d.month, d.day):
                    return col
        return None

    @staticmethod
    def _build_reception_memo(
        reception: list[dict],
        pid: str,
        meal: str,
        target_date: date,
        delivery_dates: list[date],
    ) -> str:
        """접수장 기반 비고 문자열 생성.

        기준 (우선순위):
        ① 기간 — 적용일이 금주 배송일 범위(delivery_dates) 내인 항목만
        ② 최신 — 같은 날짜에 여러 항목 활성이면 적용일 가장 최신만 표시
        ③ 복원 — 최신 활성='배송'이고 이전에 중지/일시중지 있었다면 '복원'
        """
        relevant = [
            r
            for r in reception
            if str(r.get("ID", "")).strip() == pid
            and str(r.get("식종", "")).strip().startswith(meal)
        ]
        if not relevant:
            return ""

        period_start = delivery_dates[0] if delivery_dates else target_date

        # ① 기간 기준: 적용일이 이번 배송일 범위 내인 항목
        period_entries = [
            r
            for r in relevant
            if r.get("적용일") is not None
            and period_start <= r["적용일"] <= target_date
        ]

        # ② 활성 + target_date에 활성인 항목 중 최신 1개만
        active_in_period = [
            r for r in period_entries if _is_reception_active(r, target_date)
        ]
        if active_in_period:
            latest = max(active_in_period, key=lambda r: r["적용일"])
            return _format_latest_memo(latest, relevant)

        # ③ 복원 fallback: 기간 내 '일시중지'가 끝났고 활성 없으면
        for r in relevant:
            if (
                str(r.get("상태", "")).strip() == "일시중지"
                and r.get("종료일") is not None
                and r.get("적용일") is not None
                and r["종료일"] < target_date
                and r["종료일"] >= period_start
            ):
                return "복원"

        return ""

    def _get_delivery_dates(
        self,
        delivery: list[dict],
        target_dt: date,
        meal: str,
        meal_label: str,
        holiday_set: set[date],
    ) -> list[date]:
        """주간 배송일 + 직전 배송일 목록 반환."""
        monday = target_dt - timedelta(days=target_dt.weekday())

        def _is_active(d: date) -> bool:
            col = self._find_date_col(delivery, d)
            if col is None:
                return False
            for row in delivery:
                meal_val = str(row.get("식종", "")).strip()
                if not meal_val.startswith(meal):
                    continue
                val = str(row.get(col, "")).strip().upper()
                if val in ("1", "1.0", "O"):
                    return True
            return False

        # 직전 배송일 찾기 (최대 23일 전까지)
        recent = None
        for k in range(1, 23):
            d = monday - timedelta(days=k)
            if d.weekday() < 5 and d not in holiday_set and _is_active(d):
                recent = d
                break

        dates: set[date] = set()
        if recent:
            dates.add(recent)

        if MealType.from_name(meal) == MealType.BREAKFAST:
            if _is_active(target_dt):
                dates.add(target_dt)
        else:
            for i in range(target_dt.weekday() + 1):
                d = monday + timedelta(days=i)
                if d.weekday() < 5 and d not in holiday_set and _is_active(d):
                    dates.add(d)

        return sorted(dates)

    def _build_output_table(
        self,
        staff_counts: dict[str, list[int]],
        staff_memos: dict[str, list[StaffMemo]],
        staff_areas: dict[str, set[str]],
        prev_buckets: dict[str, list[int]],
        meal: str,
    ) -> list[list]:
        """집계 결과로 최종 출력 테이블 생성."""
        all_staff = (
            set(staff_counts) | set(staff_memos) | set(prev_buckets) | set(staff_areas)
        )
        staff_order = self._staff_sort.get(meal, [])

        order_map = {name: i for i, name in enumerate(staff_order)}
        sorted_staffs = sorted(
            all_staff,
            key=lambda s: order_map.get(s.strip(), float("inf")),
        )

        table = [["배송담당", "1", "2", "3", "4", "5", "Total", "비고", "참고"]]
        totals = [0] * 6

        for st in sorted_staffs:
            qtys = staff_counts.get(st, [])
            qty_counter = Counter(qtys)
            counts = [qty_counter.get(i, 0) for i in range(1, 6)]
            memos = staff_memos.get(st, [])
            if not any(counts) and not memos:
                continue

            memo_parts = []
            for sm in sorted(memos, key=lambda x: x.memo_text):
                area_parts = [
                    f"{a}({', '.join(sorted(ns))})"
                    for a, ns in sorted(sm.area_groups.items())
                ]
                memo_parts.append(f"{sm.memo_text}\n{', '.join(area_parts)}")
            memo_text = "\n".join(memo_parts)

            areas = sorted(staff_areas.get(st, []))
            area_text = "\n".join(
                ", ".join(areas[i : i + 2]) for i in range(0, len(areas), 2)
            )

            weighted = sum(i * c for i, c in enumerate(counts, 1))
            row = [
                f"{st}\n({sum(counts)})",
                *[
                    fmt_cell(c, p)
                    for c, p in zip(counts, prev_buckets.get(st, [0] * 5))
                ],
                weighted,
                memo_text,
                area_text,
            ]
            table.append(row)

            for j, c in enumerate(counts + [weighted]):
                totals[j] += c

        if len(table) > 1:
            table.append(["합계", *totals[:5], totals[5], "", ""])

        return table


# ── 접수장 요일 매핑 ────────────────────────────────
_WEEKDAY_MAP = {"월": 0, "화": 1, "수": 2, "목": 3, "금": 4, "토": 5, "일": 6}


def _is_reception_active(row: dict, target_date: date) -> bool:
    """접수장 row가 특정 날짜에 활성 상태인지 확인.

    적용일·종료일 범위 내에 있고, 요일패턴(월수목 등)이 있으면 해당 요일만 활성.
    """
    apply_date = row.get("적용일")
    end_date = row.get("종료일")

    if apply_date is not None and apply_date > target_date:
        return False
    if end_date is not None and end_date < target_date:
        return False

    pattern = str(row.get("요일패턴", "") or "").strip()
    if pattern:
        return target_date.weekday() in {
            _WEEKDAY_MAP[ch] for ch in pattern if ch in _WEEKDAY_MAP
        }

    return True


def _format_reception_memo(row: dict) -> str:
    """접수장 row → 표시용 메모 문자열 (예: '일시중지 6/13~6/19')."""
    status = str(row.get("상태", "")).strip()
    apply_date = row.get("적용일")
    end_date = row.get("종료일")

    parts = [status]
    if apply_date:
        date_str = f"{apply_date.month}/{apply_date.day}"
        if end_date:
            date_str += f"~{end_date.month}/{end_date.day}"
        else:
            date_str += "~"
        parts.append(date_str)

    # 요일패턴 있으면 추가 (예: '일시중지 6/13~6/19 (월수목)')
    pattern = str(row.get("요일패턴", "") or "").strip()
    if pattern:
        parts.append(f"({pattern})")

    return " ".join(parts)


_REGEN_STATUSES = frozenset({"일시중지", "중지"})


def _format_latest_memo(latest: dict, all_relevant: list[dict]) -> str:
    """최신 활성 항목 → 표시 문자열. 이전에 중지/일시중지 있었다면 '복원'."""
    status = str(latest.get("상태", "")).strip()
    apply_date = latest.get("적용일")

    is_restoration = (
        status == "배송"
        and apply_date is not None
        and any(
            str(r.get("상태", "")).strip() in _REGEN_STATUSES
            and r.get("적용일") is not None
            and r["적용일"] < apply_date
            for r in all_relevant
        )
    )

    if is_restoration:
        return f"복원 {apply_date.month}/{apply_date.day}~"

    return _format_reception_memo(latest)


def _extract_addr_key(addr: str) -> str:
    """신주소에서 그룹핑 키 추출."""
    if not addr or addr in ("", "nan", "None", "-1"):
        return ""
    import re

    m = re.search(r"\b(\d{3,4}-\d{3,4})\b", addr)
    if m:
        return m.group(1)
    m = re.search(r"(\d+)\s*동\s*(\d+)\s*호", addr)
    if m:
        return f"{m.group(1)}동 {m.group(2)}호"
    m = re.search(r"(\d+)\s*호", addr)
    if m:
        return f"{m.group(1)}호"
    return ""
