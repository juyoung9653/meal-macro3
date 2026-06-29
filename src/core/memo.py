"""메모 문자열 파싱: 날짜 기반 액션(일시중지/배송/IN/OUT 등) 추출."""

import re
from datetime import date, timedelta

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

    dates_list: list[date] = []
    cur = apply_date
    while cur <= end_date:
        if cur.weekday() in days:
            dates_list.append(cur)
        cur += timedelta(days=1)

    if not dates_list:
        return None

    ranges: list[tuple[date, date]] = []
    s = e = dates_list[0]
    for d in dates_list[1:]:
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


# 정규식 (성능 위해 모듈 레벨에서 한 번만 컴파일)
RE_ACT = re.compile(r"(일시중지|배송|신규|중지|원복)")
RE_INOUT = re.compile(r"(?<![a-zA-Z])(IN|OUT)(?![a-zA-Z])", re.IGNORECASE)
RE_ANY_MD = re.compile(r"(?<!\d)(\d{1,2})/(\d{1,2})(?!\d)")
RE_RANGE = re.compile(r"(\d{1,2}/\d{1,2})\s*~\s*(\d{1,2}/\d{1,2})")
RE_MD = re.compile(r"(\d{1,2})/(\d{1,2})")
RE_YMD = re.compile(r"(\d{4})(\d{2})(\d{2})")

# 전각→반각 변환표
TRANS_TABLE = str.maketrans("０１２３４５６７８９／：～（）　", "0123456789/:~() ")

# 오타/이형 보정 맵
REPLACE_MAP = {
    "추가": "배송",
    "재개": "원복",
    "일지중지": "일시중지",
    "제외": "중지",
}

# 이 키워드 포함 시 메모 무시
SKIP_KEYWORDS = ("이곳으로", "재확인", "기사요청")

# 정규화 결과 캐시 (동일 메모 문자열 중복 처리 방지)
_memo_norm_cache: dict[str, str] = {}
_MAX_CACHE_SIZE = 1000


def _trim_cache() -> None:
    if len(_memo_norm_cache) > _MAX_CACHE_SIZE:
        keys = list(_memo_norm_cache.keys())
        for k in keys[: len(keys) * 2 // 10]:
            del _memo_norm_cache[k]


def get_active_memo(txt: str | None, target_dt: date) -> str:
    """메모에서 target_dt에 해당하는 액션 문자열을 반환."""
    if not txt:
        return ""

    s = _normalize_memo(str(txt))
    if not s or any(k in s for k in SKIP_KEYWORDS):
        return ""

    seen: set[str] = set()
    out: list[str] = []
    month, day = target_dt.month, target_dt.day

    for seg in (x.strip() for x in s.split(";") if x.strip()):
        # IN/OUT 우선 처리
        if _process_inout_seg(seg, month, day, seen, out):
            continue
        # 액션 키워드 처리
        _process_action_seg(seg, month, day, seen, out)

    return "\n".join(out)


def build_reception_memo(
    person_id: str,
    reception_entries: list[dict],
    show_meal_prefix: bool = False,
) -> str:
    """접수장 데이터 → 통합 비고 문자열 생성.

    중지→배송 연속 쌍을 일시중지로 병합하고,
    각 항목을 "날짜 상태" 또는 "식종: 날짜 상태" 형식으로 포맷.
    요일패턴(월수목)이 있으면 실제 날짜로 확장.

    Args:
        person_id: 대상자 ID
        reception_entries: 전체 접수장 데이터 (person_id로 필터링)
        show_meal_prefix: True면 각 줄에 "식종:" 접두사 추가

    Returns:
        멀티라인 문자열, 항목 없으면 빈 문자열
    """
    person_entries = [
        r for r in reception_entries if str(r.get("ID", "")).strip() == person_id
    ]
    if not person_entries:
        return ""

    merged = _merge_stop_resume(person_entries)

    lines = []
    for r in merged:
        status = str(r.get("상태", "")).strip()
        if not status:
            continue

        apply_date = r.get("적용일")
        end_date = r.get("종료일")
        meal = str(r.get("식종", "")).strip() if show_meal_prefix else ""
        pattern = str(r.get("요일패턴", "") or "").strip()

        if apply_date:
            # 요일패턴 있으면 실제 날짜로 확장
            expanded = _expand_pattern_to_date_str(apply_date, end_date, pattern)
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
            lines.append(" ".join(parts))
        else:
            parts = []
            if meal:
                parts.append(f"{meal}:")
            parts.append(status)
            lines.append(" ".join(parts))

    return "\n".join(lines)


def _merge_stop_resume(entries: list[dict]) -> list[dict]:
    """중지→배송 연속 쌍을 일시중지로 병합."""
    if not entries:
        return []

    by_meal: dict[str, list[dict]] = {}
    for e in entries:
        meal = str(e.get("식종", "")).strip()
        by_meal.setdefault(meal, []).append(e)

    result = []
    for _meal, meal_entries in by_meal.items():
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
                    merged = dict(cur)
                    merged["상태"] = "일시중지"
                    merged["종료일"] = nxt.get("적용일")
                    result.append(merged)
                    i += 2
                    continue

            result.append(cur)
            i += 1

    return result


def _normalize_memo(raw: str) -> str:
    """전각→반각, 주석 제거, 오타 보정. 결과 캐시."""
    cached = _memo_norm_cache.get(raw)
    if cached is not None:
        return cached
    s = raw.translate(TRANS_TABLE)
    lines = [l for l in s.splitlines() if "//" not in l and "#" not in l]
    s = ";".join(lines)
    for k, v in REPLACE_MAP.items():
        s = s.replace(k, v)
    _memo_norm_cache[raw] = s
    _trim_cache()
    return s


def _process_inout_seg(
    seg: str, month: int, day: int, seen: set[str], out: list[str]
) -> bool:
    """IN/OUT 세그 처리. 처리했으면 True 반환."""
    m = RE_INOUT.search(seg)
    if not m:
        return False
    md_match = RE_ANY_MD.search(seg)
    if not md_match:
        return True  # consumed but no date → skip
    mm, dd = int(md_match.group(1)), int(md_match.group(2))
    if mm == month and dd == day:
        key = f"배송조정 {m.group(1).upper()} - {mm}/{dd}~"
        if key not in seen:
            seen.add(key)
            out.append(key)
    return True


def _process_action_seg(
    seg: str, month: int, day: int, seen: set[str], out: list[str]
) -> None:
    """일시중지/배송/신규/중지/원복 액션 처리."""
    m_act = RE_ACT.search(seg)
    if not m_act:
        return

    seg2 = seg.split(")")[-1] if ")" in seg else seg
    action = m_act.group(1)

    # 기간 매칭 우선
    if _try_range(seg2, action, month, day, seen, out):
        return
    # 단일 일자 매칭
    _try_single_date(seg2, action, month, day, seen, out)


def _try_range(
    seg2: str, action: str, month: int, day: int, seen: set[str], out: list[str]
) -> bool:
    """'5/1~5/9' 기간 매칭. 매칭되면 True."""
    m_rng = RE_RANGE.search(seg2)
    if not m_rng:
        return False
    sm, sd = map(int, m_rng.group(1).split("/"))
    em, ed = map(int, m_rng.group(2).split("/"))
    target_md = month * 100 + day
    if (sm * 100 + sd) <= target_md <= (em * 100 + ed):
        key = f"{action} {m_rng.group(1)}~{m_rng.group(2)}"
        if key not in seen:
            seen.add(key)
            out.append(key)
    return True


def _try_single_date(
    seg2: str, action: str, month: int, day: int, seen: set[str], out: list[str]
) -> None:
    """월/일 또는 연월일 단일 날짜 매칭."""
    suffix = "" if action == "일시중지" else "~"

    if m_md := RE_MD.search(seg2):
        mm, dd = int(m_md.group(1)), int(m_md.group(2))
        if mm == month and dd == day:
            key = f"{action} {mm}/{dd}{suffix}"
            if key not in seen:
                seen.add(key)
                out.append(key)
        return

    if m_ymd := RE_YMD.search(seg2):
        mm, dd = int(m_ymd.group(2)), int(m_ymd.group(3))
        if mm == month and dd == day:
            key = f"{action} {mm}/{dd}{suffix}"
            if key not in seen:
                seen.add(key)
                out.append(key)
