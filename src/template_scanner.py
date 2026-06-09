"""템플릿 플레이스홀더 스캐너 — {key} 패턴 찾아 위치 캐싱.

캐시는 템플릿 파일 mtime 기준 무효화.
cache_path 지정 시 파일로 영속화 (프로그램 재시작 간 유지).
"""

import json
import logging
import os
import re
from collections import defaultdict

import openpyxl

log = logging.getLogger(__name__)

# ── 인메모리 캐시 (파일 캐시의 2차 캐시 역할) ──
_mem_cache: dict[str, tuple[float, dict[str, dict[str, list[tuple[int, int]]]]]] = {}
#           template_path -> (mtime, {sheet_name: {placeholder: [(row, col), ...]}})


def _get_mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _load_file_cache(cache_path: str) -> dict:
    """파일 캐시에서 로드. 없거나 깨졌으면 {}."""
    if not os.path.exists(cache_path):
        return {}
    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        return data
    except (json.JSONDecodeError, OSError) as e:
        log.warning("Failed to load cache file %s: %s", cache_path, e)
        return {}


def _save_file_cache(cache_path: str, data: dict):
    """파일 캐시 저장."""
    try:
        dname = os.path.dirname(cache_path)
        if dname:
            os.makedirs(dname, exist_ok=True)
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError as e:
        log.warning("Failed to save cache file %s: %s", cache_path, e)


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
    cache_path: str | None = None,
) -> dict[str, dict[str, list[tuple[int, int]]]]:
    """템플릿 파일의 지정 시트들에서 {key} 패턴 위치 스캔.

    동일 placeholder가 여러 셀에 있으면 리스트로 모두 보관.

    캐시 전략:
      1. 인메모리 캐시 확인 (mtime 비교)
      2. 파일 캐시 확인 (mtime 비교, cache_path 지정 시)
      3. 템플릿 직접 스캔 → 인메모리 + 파일 캐시 갱신

    Args:
        template_path: 엑셀 템플릿 경로
        sheet_names: 스캔할 시트명 목록. None이면 모든 시트.
        cache_path: 파일 캐시 경로. None이면 인메모리 전용.

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

    # ── 2. 파일 캐시 ──
    if cache_path:
        file_cache = _load_file_cache(cache_path)
        entry = file_cache.get(template_path)
        if entry and isinstance(entry, dict):
            cached_mtime = entry.get("_mtime", 0)
            if cached_mtime == mtime:
                data = entry.get("_data")
                if data:
                    _mem_cache[template_path] = (mtime, data)
                    log.info(
                        "File-cache hit for %s (%d sheets)", template_path, len(data)
                    )
                    return data

    # ── 3. 템플릿 직접 스캔 ──
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
    if cache_path:
        file_cache = _load_file_cache(cache_path)
        file_cache[template_path] = {"_mtime": mtime, "_data": result}
        _save_file_cache(cache_path, file_cache)

    return result


def clear_cache(template_path: str | None = None, cache_path: str | None = None):
    """캐시 초기화.

    Args:
        template_path: 특정 템플릿만 초기화. None이면 전체.
        cache_path: 파일 캐시도 초기화할 경로.
    """
    if template_path is None:
        _mem_cache.clear()
    else:
        _mem_cache.pop(template_path, None)

    if cache_path:
        if template_path is None:
            try:
                os.remove(cache_path)
            except OSError:
                pass
        else:
            file_cache = _load_file_cache(cache_path)
            file_cache.pop(template_path, None)
            _save_file_cache(cache_path, file_cache)


# ── 플레이스홀더 검증 ──────────────────────────────

# 필수 글로벌 플레이스홀더 (모든 시트)
_REQUIRED_GLOBAL = ["title", "delivery", "d_phone", "addr"]

# 사람별 필수 플레이스홀더 접두사
_REQUIRED_PERSON_PREFIXES = ["num", "child", "carer", "phone"]

# 주차별 필수 플레이스홀더 (1~5주, 모든 사람 공통)
_REQUIRED_WEEK_PREFIXES = ["week"]

# 사람×주차별 식종 플레이스홀더
_REQUIRED_MEAL_PREFIXES = ["sum_b", "sum_d", "sum_l"]


def validate_placeholders(
    placeholders: dict[str, dict[str, list[tuple[int, int]]]],
) -> list[str]:
    """플레이스홀더 맵 검증. 누락된 항목 있으면 경고 메시지 리스트 반환.

    Args:
        placeholders: scan_placeholders() 반환값.

    Returns:
        누락 경고 문자열 리스트. 비어있으면 검증 통과.
    """
    warnings: list[str] = []

    for sheet_name, ph_map in placeholders.items():
        # 시트명에서 인원수 추출 (예: "3명" → 3)
        m = re.match(r"(\d+)명", sheet_name)
        if not m:
            continue
        n_people = int(m.group(1))

        missing: list[str] = []

        # 글로벌
        for key in _REQUIRED_GLOBAL:
            if key not in ph_map:
                missing.append(key)

        # 사람별
        for p in range(1, n_people + 1):
            for prefix in _REQUIRED_PERSON_PREFIXES:
                key = f"{prefix}{p}"
                if key not in ph_map:
                    missing.append(key)
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
