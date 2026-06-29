"""db.xlsx 기반 통합 데이터 리더.

db.xlsx를 유일한 데이터 소스로 사용. 시트를 읽기만 하고 계산은 하지 않음.
배송전체 시트의 날짜 컬럼에서 해당 월을 자동 감지.
기타 시트 안의 방학일정/배송휴무/식종정보 테이블 파싱 지원.

Usage::
    from src.excel.db_reader import DbReader
    reader = DbReader("assets/db.xlsx")
    roster = reader.read_roster()
    delivery = reader.read_delivery_matrix()
    y, m = reader.detect_month()
"""

from __future__ import annotations

import logging
import os
import tempfile
from datetime import date, datetime
from typing import Any

import msoffcrypto
import openpyxl

log = logging.getLogger(__name__)


# ── 헬퍼 (순수 함수, _parsing으로 분리) ──
from ._parsing import (
    _is_date_col,
    _normalize_headers,
    _parse_date_col,
    _rows_to_dicts,
    _to_date,
    _to_str,
)
from .data_source import DataSource

# ── DbReader ────────────────────────────────────────


class DbReader(DataSource):
    """db.xlsx 단일 데이터 소스 리더. data_only=True, read_only=True."""

    def __init__(self, db_path: str | None = None, password: str | None = None):
        self._db_path = db_path or "assets/db.xlsx"
        self._password = password or ""
        self._wb: openpyxl.Workbook | None = None
        self._cache: dict[str, Any] = {}

    @property
    def db_path(self) -> str:
        return self._db_path

    @db_path.setter
    def db_path(self, path: str):
        self._db_path = path
        self._invalidate_cache()

    def _invalidate_cache(self):
        self._cache.clear()
        if self._wb:
            self._wb.close()
            self._wb = None

    # ── 시트명 → 헤더 기반 시트 찾기 ──

    def _resolve_sheet(
        self, name_hint: str, required_headers: set[str] | None = None
    ) -> str:
        """시트 찾기: ①헤더매칭 → ②정확한시트명 → ③부분일치(name_hint 포함)."""
        wb = self._get_wb()

        if required_headers:
            for sn in wb.sheetnames:
                ws = wb[sn]
                try:
                    first_row = next(ws.iter_rows(values_only=True))
                except StopIteration:
                    continue
                headers = {_to_str(c).strip() for c in first_row if c is not None}
                if required_headers.issubset(headers):
                    log.debug(
                        "[DbReader] 헤더매칭 → '%s' (name_hint=%s)", sn, name_hint
                    )
                    return sn

        if name_hint in wb.sheetnames:
            return name_hint

        for sn in wb.sheetnames:
            if name_hint in sn:
                log.debug("[DbReader] 부분일치 → '%s' (name_hint=%s)", sn, name_hint)
                return sn

        log.warning("[DbReader] 시트 '%s' 못 찾음. 그대로 사용.", name_hint)
        return name_hint

    def _get_wb(self) -> openpyxl.Workbook:
        if self._wb is None:
            if not os.path.exists(self._db_path):
                raise FileNotFoundError(f"db.xlsx 없음: {self._db_path}")

            load_path = self._db_path
            cleanup = None

            if self._password:
                try:
                    with open(self._db_path, "rb") as f:
                        file = msoffcrypto.File(f)
                        file.load_key(password=self._password)
                        tmp = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
                        file.decrypt(tmp)
                        tmp.close()
                        load_path = tmp.name
                        cleanup = tmp.name
                except Exception as e:
                    log.warning(
                        "[DbReader] 복호화 실패 (비번 오류?): %s. 원본 시도.", e
                    )

            try:
                self._wb = openpyxl.load_workbook(load_path, data_only=True)
            finally:
                if cleanup:
                    try:
                        os.unlink(cleanup)
                    except OSError:
                        pass

        return self._wb

    def _resolve_table(
        self, table_name: str, fallback: str | None = None
    ) -> str | None:
        """Excel 테이블(ListObject) 이름으로 시트 찾기."""
        wb = self._get_wb()
        for ws in wb.worksheets:
            if table_name in ws.tables:
                log.debug("[DbReader] 테이블 '%s' → 시트 '%s'", table_name, ws.title)
                return ws.title
        return fallback

    def _read_sheet(self, sheet_name: str) -> list[dict]:
        """시트 -> dict 리스트 (첫 행 = 헤더)."""
        wb = self._get_wb()
        log.debug(
            "[DbReader] _read_sheet('%s') 시작 — 전체 시트: %s",
            sheet_name,
            wb.sheetnames,
        )
        if sheet_name not in wb.sheetnames:
            log.debug("[DbReader] 시트 '%s' 없음 (무시)", sheet_name)
            return []

        ws = wb[sheet_name]
        rows = list(ws.iter_rows(values_only=True))
        log.debug("[DbReader] 시트 '%s' raw 행 수: %d", sheet_name, len(rows))
        if not rows:
            return []

        raw_headers = [_to_str(c) for c in rows[0]]
        log.debug("[DbReader] 시트 '%s' raw 헤더: %s", sheet_name, raw_headers)
        headers = _normalize_headers(raw_headers)
        return _rows_to_dicts(headers, rows[1:])

    def _read_sheet_raw(self, sheet_name: str) -> list[list]:
        wb = self._get_wb()
        if sheet_name not in wb.sheetnames:
            return []
        ws = wb[sheet_name]
        return [list(row) for row in ws.iter_rows(values_only=True)]

    def _read_table(self, sheet_name: str, header_marker: str) -> list[dict]:
        """시트 안에서 특정 테이블 찾기. header_marker 포함 행=헤더, 다음 빈행까지."""
        raw = self._read_sheet_raw(sheet_name)
        if not raw:
            return []
        hi = -1
        for i, row in enumerate(raw):
            if any(str(header_marker) in _to_str(c) for c in row):
                hi = i
                break
        if hi < 0:
            return []
        rh = [_to_str(c) for c in raw[hi]]
        headers = _normalize_headers(rh)
        # Find table end: stop at first all-empty row
        data_rows = []
        for row in raw[hi + 1 :]:
            if all(_to_str(c) == "" for c in row):
                break
            data_rows.append(row)
        return _rows_to_dicts(headers, data_rows)

    # ── Public: 시트 읽기 ──

    def read_roster(self) -> list[dict]:
        """명단 시트."""
        if "roster" not in self._cache:
            sn = self._resolve_table("명단") or self._resolve_sheet(
                "명단", {"ID", "성명"}
            )
            self._cache["roster"] = self._read_sheet(sn)
        return self._cache["roster"]

    def read_delivery_matrix(self) -> list[dict]:
        """배송전체 시트 (Power Query 출력).

        배송전체 시트는 상단 2행이 요약(연도/월/전체합계, 일자별합계)이고
        실제 데이터 헤더는 3행부터 시작. 이를 자동 감지하여 읽는다.
        """
        if "delivery_matrix" not in self._cache:
            raw = self._read_sheet_raw("배송전체")
            if not raw:
                self._cache["delivery_matrix"] = []
                return self._cache["delivery_matrix"]

            # 실제 헤더 행 찾기: "ID" 포함 + 날짜패턴 컬럼 존재
            header_row_idx = -1
            for i, row in enumerate(raw):
                strs = [_to_str(c) for c in row]
                has_id = any("ID" == s for s in strs)
                has_date = any(_is_date_col(s) for s in strs)
                if has_id and has_date:
                    header_row_idx = i
                    break

            if header_row_idx < 0:
                # fallback: 3행부터 (0-based index 2)
                header_row_idx = 2 if len(raw) > 2 else 0

            # 헤더 처리
            raw_headers = [_to_str(c) for c in raw[header_row_idx]]
            headers = _normalize_headers(raw_headers)

            # 데이터 행 (헤더 다음 행부터)
            data = _rows_to_dicts(headers, raw[header_row_idx + 1 :])

            self._cache["delivery_matrix"] = data
        return self._cache["delivery_matrix"]

    def read_delivery_order(self) -> list[dict]:
        """배송정보 시트 (배송순서/배송담당)."""
        if "delivery_order" not in self._cache:
            sn = self._resolve_table("배송순서") or self._resolve_sheet(
                "배송정보", {"핸드폰", "도로명주소"}
            )
            data = self._read_sheet(sn)
            if not data:
                data = self._read_sheet("배송순서")  # fallback for legacy
            self._cache["delivery_order"] = data
        return self._cache["delivery_order"]

    # ── 기타 시트 내 테이블 ──

    def _read_from_gita(self, table_marker: str) -> list[dict]:
        """기타 시트에서 특정 테이블 읽기 (캐시 O)."""
        cache_key = f"gita_{table_marker}"
        if cache_key not in self._cache:
            self._cache[cache_key] = self._read_table("기타", table_marker)
        return self._cache[cache_key]

    def read_vacation_schedule(self) -> list[dict]:
        """방학일정 (기타 시트 내 테이블)."""
        return self._read_from_gita("방학")

    def read_holidays(self) -> list[date]:
        """배송휴무 (기타 시트 내 테이블) -> date 리스트."""
        if "holidays" not in self._cache:
            raw = self._read_sheet_raw("기타")
            holidays: list[date] = []
            in_table = False
            for row in raw:
                row_strs = [_to_str(c) for c in row]
                if any("휴무" in s or "공휴일" in s for s in row_strs):
                    in_table = True
                    continue
                if in_table:
                    if all(v == "" for v in row_strs):
                        break
                    d = _to_date(row[0]) if row else None
                    if d:
                        holidays.append(d)
            self._cache["holidays"] = holidays
        return self._cache["holidays"]

    def read_meal_prices(self) -> dict[str, int]:
        """식종정보 (기타 시트 내 테이블) -> {식종: 단가}."""
        if "meal_prices" not in self._cache:
            data = self._read_from_gita("식종")
            result: dict[str, int] = {}
            for row in data:
                meal = row.get("식종", "")
                price_str = row.get("단가", "0")
                if meal:
                    try:
                        result[meal] = int(float(price_str))
                    except (ValueError, TypeError):
                        result[meal] = 0
            self._cache["meal_prices"] = result
        return self._cache["meal_prices"]

    # ── 월 자동 감지 ──

    def detect_month(self) -> tuple[int, int]:
        """배송전체 컬럼에서 (year, month) 감지."""
        matrix = self.read_delivery_matrix()
        if not matrix:
            today = date.today()
            return today.year, today.month

        month_counts: dict[int, int] = {}
        for col_name in matrix[0].keys():
            parsed = _parse_date_col(col_name)
            if parsed:
                m, _ = parsed
                month_counts[m] = month_counts.get(m, 0) + 1

        detected_month = (
            max(month_counts, key=month_counts.get)
            if month_counts
            else date.today().month
        )

        year = date.today().year
        try:
            vac = self.read_vacation_schedule()
            for row in vac:
                for key in row:
                    d = _to_date(row[key])
                    if d:
                        year = d.year
                        break
                if year != date.today().year:
                    break
        except Exception:
            pass

        return year, detected_month

    # ── 편의 조회 ──

    def get_holiday_set(self) -> set[date]:
        return set(self.read_holidays())

    def build_delivery_map(self) -> dict[str, dict]:
        """배송전체 -> {ID: row}."""
        result: dict[str, dict] = {}
        for row in self.read_delivery_matrix():
            pid = row.get("ID", "").strip()
            if pid:
                result[pid] = row
        return result

    def build_order_map(self) -> dict[str, dict]:
        """배송정보 -> {핸드폰+도로명주소: row} (조인키 = 핸드폰+주소)."""
        result: dict[str, dict] = {}
        for row in self.read_delivery_order():
            phone = row.get("핸드폰", "").strip()
            addr = row.get("도로명주소", "").strip()
            if not phone and not addr:
                continue
            key = f"{phone}|{addr}"
            # 같은 키면 첫 번째 row 우선
            if key not in result:
                result[key] = row
        return result

    def read_reception(self) -> list[dict]:
        """접수장 시트. 날짜 컬럼은 date 객체로 변환."""
        if "reception" not in self._cache:
            sn = self._resolve_table("접수장") or self._resolve_sheet(
                "접수장", {"접수일"}
            )
            raw = self._read_sheet_raw(sn)
            if not raw:
                self._cache["reception"] = []
                return self._cache["reception"]

            headers = [_to_str(c) for c in raw[0]]
            cleaned = _normalize_headers(headers)

            DATE_COLS = {"접수일", "적용일", "종료일"}

            data = []
            for row in raw[1:]:
                vals = list(row)
                entry = {}
                for i, h in enumerate(cleaned):
                    val = vals[i] if i < len(vals) else None
                    if h in DATE_COLS:
                        if isinstance(val, datetime):
                            entry[h] = val.date()
                        elif isinstance(val, date):
                            entry[h] = val
                        else:
                            entry[h] = None
                    else:
                        entry[h] = _to_str(val) if val is not None else ""
                if all(v == "" or v is None for v in entry.values()):
                    continue
                data.append(entry)

            self._cache["reception"] = data
        return self._cache["reception"]

    def close(self):
        if self._wb:
            self._wb.close()
            self._wb = None
        self._cache.clear()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


# ── 싱글턴 ──

_reader: DbReader | None = None


def get_db_reader(db_path: str | None = None) -> DataSource:
    global _reader
    if _reader is None:
        from ..core.settings import get_settings

        s = get_settings()
        path = db_path or s.get("db", "path", default="assets/db.xlsx")
        pw = s.get("db", "password", default="")
        _reader = DbReader(path, password=pw)
    elif db_path and db_path != _reader.db_path:
        _reader.db_path = db_path
    return _reader
