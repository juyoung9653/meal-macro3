"""공휴일 정보 페처 — Naver → month2k → holidays fallback 체인"""

import logging
import re
import urllib.parse
import urllib.request
from datetime import date

import holidays

log = logging.getLogger(__name__)

# ── 공휴일 캐시 (연도별) ──
_HOLIDAY_CACHE: dict[int, set[date]] = {}


def get_kr_holidays(year: int) -> set[date]:
    """연도별 한국 공휴일 반환 (캐시 O). fallback 체인 사용."""
    if year in _HOLIDAY_CACHE:
        return _HOLIDAY_CACHE[year]

    result = _fetch_naver_search(year)
    if result is not None:
        _HOLIDAY_CACHE[year] = result
        log.info(f"Naver: {len(result)} holidays for {year}")
        return result

    result = _fetch_month2k(year)
    if result is not None:
        _HOLIDAY_CACHE[year] = result
        log.info(f"month2k: {len(result)} holidays for {year}")
        return result

    # 최종 fallback: holidays 패키지
    result = set(holidays.country_holidays("KR", years=year))
    _HOLIDAY_CACHE[year] = result
    log.info(f"holidays pkg: {len(result)} holidays for {year}")
    return result


# ── 1순위: Naver 검색 ──────────────────────────────


def _fetch_naver_search(year: int) -> set[date] | None:
    """Naver search 'YYYY년 공휴일' 결과 HTML 파싱"""
    try:
        url = (
            "https://search.naver.com/search.naver?where=nexearch"
            "&sm=top_hty&fbm=0&ie=utf8&query=" + urllib.parse.quote(f"{year}년 공휴일")
        )
        if (html := _fetch_url(url)) is None:
            return None

        # holiday_list div 찾기
        if not (
            m := re.search(
                r'<div class="holiday_list">(.+?)</div>\s*</div>', html, re.DOTALL
            )
        ):
            return None
        table_html = m.group(1)

        dates: set[date] = set()
        rows = re.findall(r"<tr>(.*?)</tr>", table_html, re.DOTALL)
        for row in rows[1:]:  # skip header
            cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.DOTALL)
            if len(cells) < 2:
                continue
            date_str = re.sub(r"<[^>]+>", "", cells[1]).strip()
            parsed = _parse_naver_date_str(date_str, year)
            dates.update(parsed)

        return dates if dates else None
    except Exception as e:
        log.debug(f"Naver fetch failed: {e}")
        return None


_MONTH_DAY_RE = re.compile(r"(\d+)\s*월\s*(\d+)\s*일")
_DAY_ONLY_RE = re.compile(r"~\s*(\d+)\s*일")


def _parse_naver_date_str(s: str, year: int) -> list[date]:
    """
    Naver 날짜 문자열 파싱.
    "1월 1일" → [1/1]
    "2월 16일 ~ 18일" → [2/16, 2/17, 2/18]
    "3월 1일3월 2일" → [3/1, 3/2]
    "9월 24일 ~ 26일" → [9/24, 9/25, 9/26]
    "10월 3일10월 5일" → [10/3, 10/5]
    """
    # 1. "M월 D일" 쌍 전부 추출
    full_matches = _MONTH_DAY_RE.findall(s)
    if not full_matches:
        return []

    results: list[date] = []
    for i, (m_str, d_str) in enumerate(full_matches):
        month = int(m_str)
        day = int(d_str)
        results.append(date(year, month, day))

        # 다음 full match 사이에 "~ D일" 패턴이 있으면 범위
        end = s.find(f"{month}월 {day}일") + len(f"{month}월 {day}일")
        if i + 1 < len(full_matches):
            next_m = int(full_matches[i + 1][0])
            next_d = int(full_matches[i + 1][1])
            next_marker = f"{next_m}월 {next_d}일"
            next_pos = s.find(next_marker, end)
            segment = s[end:next_pos] if next_pos >= 0 else ""
        else:
            segment = s[end:]

        # segment에서 "~ D일" 찾기
        for dm in _DAY_ONLY_RE.finditer(segment):
            range_end = int(dm.group(1))
            if range_end > day:
                for mid in range(day + 1, range_end + 1):
                    results.append(date(year, month, mid))

    return results


# ── 2순위: month2k.com ─────────────────────────────


def _fetch_month2k(year: int) -> set[date] | None:
    """month2k.com 달력 페이지 파싱 (월별 순회)"""
    try:
        result: set[date] = set()
        for month in range(1, 13):
            url = f"https://www.month2k.com/lunar/{year}-{month:02d}"
            html = _fetch_url(url)
            if html is None:
                return None

            # 각 날짜 h3 헤딩 찾기
            day_entries = re.findall(
                r"<h3>\s*(\d+)\s*월\s*(\d+)\s*일.*?</h3>(.*?)(?=<h3|\Z)",
                html,
                re.DOTALL,
            )
            for m_str, d_str, body in day_entries:
                if "공휴일" in body:
                    day = int(d_str)
                    result.add(date(year, month, day))
        return result if result else None
    except Exception as e:
        log.debug(f"month2k fetch failed: {e}")
        return None


# ── 유틸 ────────────────────────────────────────────


def _fetch_url(url: str) -> str | None:
    """urllib로 URL 읽기"""
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                )
            },
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.read().decode("utf-8", errors="replace")
    except Exception as e:
        log.debug(f"URL fetch failed: {url} → {e}")
        return None


def clear_cache():
    """캐시 초기화 (년도 변경 시)"""
    _HOLIDAY_CACHE.clear()
