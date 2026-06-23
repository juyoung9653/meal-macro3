"""템플릿 플레이스홀더 스캐너 — {key} 패턴 찾아 위치 캐싱.

인메모리 캐시 (mtime 기준 무효화). 같은 세션 내에서만 유지.
"""

import logging
import os
import re
from collections import defaultdict

import openpyxl

log = logging.getLogger(__name__)

# ── 인메모리 캐시 ──
_mem_cache: dict[str, tuple[float, dict[str, dict[str, list[tuple[int, int]]]]]] = {}
#           template_path -> (mtime, {sheet_name: {placeholder: [(row, col), ...]}})


def _get_mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _scan_sheets(
    wb, sheet_names: list[str]
) -> dict[str, dict[str, list[tuple[int, int]]]]:
    """워크북에서 지정 시트들 스캔해 플레이스홀더 위치 추출."""
    result: dict[str, dict[str, list[tuple[int, int]]]] = {}
    placeholder_re = re.compile(r"\{(\w+)\}")

    for sn in sheet_names:
        if sn not in wb.sheetnames:
            log.warning("Sheet %r not found in template, skipping", sn)
            continue
        ws = wb[sn]
        sheet_map: dict[str, list[tuple[int, int]]] = defaultdict(list)

        for row in range(1, ws.max_row + 1):
            for col in range(1, ws.max_column + 1):
                cell = ws.cell(row=row, column=col)
                val = cell.value
                if val is None or not isinstance(val, str):
                    continue
                m = placeholder_re.fullmatch(val.strip())
                if m:
                    key = m.group(1)
                    sheet_map[key].append((row, col))

        if sheet_map:
            result[sn] = dict(sheet_map)

    return result


def scan_placeholders(
    template_path: str,
    sheet_names: list[str] | None = None,
) -> dict[str, dict[str, list[tuple[int, int]]]]:
    """템플릿 파일의 지정 시트들에서 {key} 패턴 위치 스캔.

    동일 placeholder가 여러 셀에 있으면 리스트로 모두 보관.

    캐시 전략:
      1. 인메모리 캐시 확인 (mtime 비교)
      2. 템플릿 직접 스캔 → 인메모리 캐시 갱신

    Args:
        template_path: 엑셀 템플릿 경로
        sheet_names: 스캔할 시트명 목록. None이면 모든 시트.

    Returns:
        {sheet_name: {placeholder: [(row, col), ...]}}  — row/col은 1-based.
    """
    mtime = _get_mtime(template_path)

    # ── 1. 인메모리 캐시 ──
    if template_path in _mem_cache:
        cached_mtime, cached_data = _mem_cache[template_path]
        if cached_mtime == mtime:
            log.debug("Mem-cache hit for %s", template_path)
            return cached_data
        log.info("Template mtime changed (mem), re-scanning: %s", template_path)

    # ── 2. 템플릿 직접 스캔 ──
    log.info("Scanning placeholders in %s", template_path)
    wb = openpyxl.load_workbook(template_path, data_only=True)
    target_sheets = sheet_names or wb.sheetnames
    result = _scan_sheets(wb, target_sheets)
    wb.close()

    total_ph = sum(len(m) for m in result.values())
    log.info(
        "Found %d placeholders across %d sheets in %s",
        total_ph,
        len(result),
        template_path,
    )

    # 캐시 저장
    _mem_cache[template_path] = (mtime, result)

    return result


def clear_cache(template_path: str | None = None):
    """인메모리 캐시 초기화.

    Args:
        template_path: 특정 템플릿만 초기화. None이면 전체.
    """
    if template_path is None:
        _mem_cache.clear()
    else:
        _mem_cache.pop(template_path, None)


# ── 플레이스홀더 검증 ──────────────────────────────

# 필수 글로벌 플레이스홀더 (meal_type 별)
_REQUIRED_GLOBAL_BY_TYPE = {
    "급식": ["title", "delivery", "addr", "dong"],
    "행복도시락": ["title", "delivery", "d_phone", "addr", "memo"],
}

# 사람별 필수 플레이스홀더 접두사 (meal_type 별)
# 급식: num은 가구단위 1개 (num1만), 행복도시락: 사람별 (num1~numN)
_REQUIRED_PERSON_PREFIXES_BY_TYPE = {
    "급식": ["child", "carer", "phone"],
    "행복도시락": ["num", "child", "carer", "phone"],
}

# 주차별 필수 플레이스홀더 (1~5주, 모든 사람 공통)
_REQUIRED_WEEK_PREFIXES = ["week"]

# 사람×주차별 식종 플레이스홀더
_REQUIRED_MEAL_PREFIXES = ["sum_b", "sum_d", "sum_l"]


def validate_placeholders(
    placeholders: dict[str, dict[str, list[tuple[int, int]]]],
    meal_type: str = "급식",
) -> list[str]:
    """플레이스홀더 맵 검증. 누락된 항목 있으면 경고 메시지 리스트 반환.

    Args:
        placeholders: scan_placeholders() 반환값.
        meal_type: "급식" 또는 "행복도시락" — 템플릿별 필수 플레이스홀더 상이.

    Returns:
        누락 경고 문자열 리스트. 비어있으면 검증 통과.
    """
    global_keys = _REQUIRED_GLOBAL_BY_TYPE.get(
        meal_type, _REQUIRED_GLOBAL_BY_TYPE["급식"]
    )
    person_prefixes = _REQUIRED_PERSON_PREFIXES_BY_TYPE.get(
        meal_type, _REQUIRED_PERSON_PREFIXES_BY_TYPE["급식"]
    )

    warnings: list[str] = []

    for sheet_name, ph_map in placeholders.items():
        # 시트명에서 인원수 추출 (예: "3명" → 3)
        m = re.match(r"(\d+)명", sheet_name)
        if not m:
            continue
        n_people = int(m.group(1))

        missing: list[str] = []

        # 글로벌
        for key in global_keys:
            if key not in ph_map:
                missing.append(key)

        # 사람별
        for p in range(1, n_people + 1):
            for prefix in person_prefixes:
                key = f"{prefix}{p}"
                if key not in ph_map:
                    missing.append(key)
            # 급식: num은 가구단위 1개만 (num1)
            if meal_type == "급식" and p == 1 and "num1" not in ph_map:
                missing.append("num1")
            # 주차별 식종
            for w in range(1, 6):
                for prefix in _REQUIRED_MEAL_PREFIXES:
                    key = f"{prefix}_{p}_{w}"
                    if key not in ph_map:
                        missing.append(key)

        # 주차 (모든 사람 공통)
        for w in range(1, 6):
            for prefix in _REQUIRED_WEEK_PREFIXES:
                key = f"{prefix}{w}"
                if key not in ph_map:
                    missing.append(key)

        # 행복도시락만: num 접두사는 사람별 person_prefixes에 포함되므로
        # 여기서는 급식의 num1 추가만 처리하고 넘어감

        if missing:
            suffix = ""
            if len(missing) > 10:
                suffix = f" 외 {len(missing) - 10}개"
            warnings.append(
                f"시트 '{sheet_name}': 누락된 플레이스홀더 {len(missing)}개 — "
                + ", ".join(missing[:10])
                + suffix
            )

    return warnings
