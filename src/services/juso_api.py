"""주소정보누리집 검색 API 클라이언트.

승인키는 POST 본문으로만 전송하며 로그나 예외 문자열에 넣지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
import time
import unicodedata
from typing import Any, Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


API_URL = "https://business.juso.go.kr/addrlink/addrLinkApi.do"
TEST_ADDRESS = "서울특별시 종로구 세종대로 209"
_FORBIDDEN_CHARACTERS = re.compile(r"[%=><\[\]]")
_FORBIDDEN_SQL_WORDS = re.compile(
    r"(?:^|\s)(OR|SELECT|INSERT|DELETE|UPDATE|CREATE|DROP|EXEC|UNION|FETCH|DECLARE|TRUNCATE)(?=\s|$)",
    re.IGNORECASE,
)
_COMPACT_CHARACTERS = re.compile(r"[\s,()\[\]{}·ㆍ]", re.UNICODE)
_NUMBERED_ROAD_NAME_SPACING = re.compile(
    r"(?P<prefix>[0-9A-Za-z가-힣·ㆍ.\-]+(?:대로|로|길))\s+"
    r"(?P<branch>\d+)\s*번\s*길",
    re.UNICODE,
)
_ROAD_BUILDING_PATTERN = re.compile(
    r"(?P<road>[0-9A-Za-z가-힣·ㆍ.\-]+(?:대로|로|길))\s*"
    r"(?P<main>\d+)(?:-(?P<sub>\d+))?(?=$|[\s,(])",
    re.UNICODE,
)


class JusoError(Exception):
    """주소 변환 오류의 최상위 예외."""


class AddressLookupError(JusoError):
    """특정 주소 한 건에만 해당하는 오류."""


class InvalidAddressError(AddressLookupError):
    """API에 보낼 수 없는 주소."""


class AddressNotFoundError(AddressLookupError):
    """검색 결과가 없는 주소."""


class AmbiguousAddressError(AddressLookupError):
    """안전하게 하나를 고를 수 없는 주소."""

    def __init__(self, total_count: int, previews: Iterable[str]) -> None:
        preview_text = " / ".join(item for item in previews if item)
        detail = f" ({preview_text})" if preview_text else ""
        super().__init__(f"검색 결과가 {total_count}건이라 하나를 확정할 수 없습니다{detail}")
        self.total_count = total_count


class JusoServiceError(JusoError):
    """키, 서버 응답 또는 통신처럼 전체 작업을 중단해야 하는 오류."""

    def __init__(self, message: str, *, code: str = "") -> None:
        super().__init__(message)
        self.code = code


class JusoNetworkError(JusoServiceError):
    """재시도 후에도 복구되지 않은 통신 오류."""


@dataclass(frozen=True)
class AddressCandidate:
    road_address: str
    road_address_part1: str
    jibun_address: str
    road_name: str = ""
    building_main_number: str = ""
    building_sub_number: str = ""


@dataclass(frozen=True)
class LookupResult:
    input_address: str
    road_address: str
    jibun_address: str
    total_count: int


def clean_keyword(value: object) -> str:
    """셀 값을 API 검색어로 안전하게 정리한다."""

    keyword = " ".join(str(value or "").split())
    if not keyword:
        raise InvalidAddressError("주소가 비어 있습니다")
    forbidden = _FORBIDDEN_CHARACTERS.search(keyword)
    if forbidden:
        raise InvalidAddressError(
            f"주소에 API가 허용하지 않는 문자({forbidden.group(0)})가 있습니다"
        )
    forbidden_word = _FORBIDDEN_SQL_WORDS.search(keyword)
    if forbidden_word:
        raise InvalidAddressError(
            f"주소에 API가 허용하지 않는 단어({forbidden_word.group(1)})가 있습니다"
        )
    if len(keyword) > 80:
        raise InvalidAddressError("주소가 80자를 넘습니다")
    return keyword


def compact_address(value: str) -> str:
    """후보 비교용으로 공백과 주소 표기 구두점을 제거한다."""

    normalized = unicodedata.normalize("NFKC", value).casefold()
    return _COMPACT_CHARACTERS.sub("", normalized)


def parse_road_building_key(value: str) -> tuple[str, int, int] | None:
    """주소에서 도로명과 건물 본번·부번을 추출한다."""

    normalized = unicodedata.normalize("NFKC", value)
    normalized = _NUMBERED_ROAD_NAME_SPACING.sub(
        lambda match: f"{match.group('prefix')}{match.group('branch')}번길",
        normalized,
    )
    matches = list(_ROAD_BUILDING_PATTERN.finditer(normalized))
    if not matches:
        return None
    match = matches[-1]
    return (
        compact_address(match.group("road")),
        int(match.group("main")),
        int(match.group("sub") or 0),
    )


def candidate_road_building_key(
    candidate: AddressCandidate,
) -> tuple[str, int, int] | None:
    """API 구조화 필드를 우선 사용하고 없으면 도로명주소를 분석한다."""

    if candidate.road_name and candidate.building_main_number:
        try:
            return (
                compact_address(candidate.road_name),
                int(candidate.building_main_number),
                int(candidate.building_sub_number or 0),
            )
        except ValueError:
            pass
    return parse_road_building_key(
        candidate.road_address_part1 or candidate.road_address
    )


def choose_candidate(
    keyword: str,
    candidates: list[AddressCandidate],
    *,
    total_count: int,
    allow_first_match: bool,
) -> AddressCandidate:
    """정확/접두 일치 후보를 고르고 불확실하면 명시적으로 실패한다."""

    if not candidates:
        raise AddressNotFoundError("검색 결과가 없습니다")

    query = compact_address(keyword)
    query_road_building = parse_road_building_key(keyword)
    if query_road_building is not None:
        road_building_matches = [
            candidate
            for candidate in candidates
            if candidate_road_building_key(candidate) == query_road_building
        ]
        if len(road_building_matches) == 1:
            return road_building_matches[0]

    exact: list[AddressCandidate] = []
    prefix_matches: list[tuple[int, AddressCandidate]] = []

    for candidate in candidates:
        forms = {
            compact_address(candidate.road_address),
            compact_address(candidate.road_address_part1),
        }
        forms.discard("")
        if query in forms:
            exact.append(candidate)
            continue

        matching_lengths = [
            len(form)
            for form in forms
            if query.startswith(form) or form.startswith(query)
        ]
        if matching_lengths:
            prefix_matches.append((max(matching_lengths), candidate))

    if len(exact) == 1:
        return exact[0]
    if not exact and prefix_matches:
        best_length = max(score for score, _ in prefix_matches)
        best = [item for score, item in prefix_matches if score == best_length]
        if len(best) == 1:
            return best[0]
    if len(candidates) == 1:
        return candidates[0]
    if allow_first_match:
        return candidates[0]

    previews = [item.road_address for item in candidates[:3]]
    raise AmbiguousAddressError(total_count or len(candidates), previews)


class JusoApiClient:
    """재시도, 호출 간격 제한, 중복 캐시를 포함한 API 클라이언트."""

    def __init__(
        self,
        api_key: str,
        *,
        allow_first_match: bool = False,
        timeout: float = 15.0,
        minimum_interval: float = 0.2,
        max_attempts: int = 4,
        opener: Callable[[Request, float], Any] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        api_key = api_key.strip()
        if not api_key:
            raise ValueError("API 승인키가 비어 있습니다")
        self._api_key = api_key
        self.allow_first_match = allow_first_match
        self.timeout = timeout
        self.minimum_interval = max(0.0, minimum_interval)
        self.max_attempts = max(1, max_attempts)
        self._opener = opener or self._default_open
        self._sleep = sleeper
        self._clock = clock
        self._last_request_at: float | None = None
        self._cache: dict[str, LookupResult] = {}

    @staticmethod
    def _default_open(request: Request, timeout: float) -> Any:
        return urlopen(request, timeout=timeout)

    def lookup(self, address: object) -> LookupResult:
        keyword = clean_keyword(address)
        cache_key = compact_address(keyword)
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        payload = self._request_with_retries(keyword)
        results = payload.get("results")
        if not isinstance(results, dict):
            raise JusoServiceError("주소 API 응답 형식이 올바르지 않습니다")

        common = results.get("common")
        if not isinstance(common, dict):
            raise JusoServiceError("주소 API 공통 응답이 없습니다")

        code = str(common.get("errorCode", ""))
        message = str(common.get("errorMessage", "알 수 없는 API 오류")).replace(self._api_key, "[승인키]")
        if code != "0":
            fatal_markers = ("승인되지 않은", "승인키", "KEY", "시스템에러", "기간이 만료")
            if code in {"-999", "E0001"} or any(
                marker.casefold() in message.casefold() for marker in fatal_markers
            ):
                raise JusoServiceError(f"주소 API 오류 {code}: {message}", code=code)
            raise InvalidAddressError(f"주소 API 검색 오류 {code}: {message}")

        try:
            total_count = int(common.get("totalCount") or 0)
        except (TypeError, ValueError):
            total_count = 0

        raw_candidates = results.get("juso")
        if isinstance(raw_candidates, dict):
            raw_candidates = [raw_candidates]
        if not isinstance(raw_candidates, list):
            raw_candidates = []

        candidates: list[AddressCandidate] = []
        for raw in raw_candidates:
            if not isinstance(raw, dict):
                continue
            jibun = str(raw.get("jibunAddr") or "").strip()
            if not jibun:
                continue
            candidates.append(
                AddressCandidate(
                    road_address=str(raw.get("roadAddr") or "").strip(),
                    road_address_part1=str(raw.get("roadAddrPart1") or "").strip(),
                    jibun_address=jibun,
                    road_name=str(raw.get("rn") or "").strip(),
                    building_main_number=str(raw.get("buldMnnm") or "").strip(),
                    building_sub_number=str(raw.get("buldSlno") or "").strip(),
                )
            )

        try:
            selected = choose_candidate(
                keyword,
                candidates,
                total_count=total_count,
                allow_first_match=self.allow_first_match,
            )
        except AddressNotFoundError:
            base_keyword, separator, detail = keyword.partition(",")
            detail = detail.strip()
            if not separator or not base_keyword.strip() or not detail:
                raise
            base_result = self.lookup(base_keyword.strip())
            result = LookupResult(
                input_address=keyword,
                road_address=base_result.road_address,
                jibun_address=f"{base_result.jibun_address}, {detail}",
                total_count=base_result.total_count,
            )
            self._cache[cache_key] = result
            return result
        result = LookupResult(
            input_address=keyword,
            road_address=selected.road_address,
            jibun_address=selected.jibun_address,
            total_count=total_count,
        )
        self._cache[cache_key] = result
        return result

    def _wait_for_rate_limit(self) -> None:
        if self._last_request_at is None:
            return
        elapsed = self._clock() - self._last_request_at
        remaining = self.minimum_interval - elapsed
        if remaining > 0:
            self._sleep(remaining)

    def _request_once(self, keyword: str) -> dict[str, Any]:
        self._wait_for_rate_limit()
        body = urlencode(
            {
                "confmKey": self._api_key,
                "currentPage": "1",
                "countPerPage": "10",
                "keyword": keyword,
                "resultType": "json",
            }
        ).encode("utf-8")
        request = Request(
            API_URL,
            data=body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Accept": "application/json",
                "User-Agent": "JusoOldAddressConverter/1.0",
            },
            method="POST",
        )
        try:
            with self._opener(request, self.timeout) as response:
                raw = response.read()
        finally:
            self._last_request_at = self._clock()

        try:
            decoded = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise JusoServiceError("주소 API가 올바른 JSON을 반환하지 않았습니다") from exc
        if not isinstance(decoded, dict):
            raise JusoServiceError("주소 API 응답 최상위 형식이 올바르지 않습니다")
        return decoded

    def _request_with_retries(self, keyword: str) -> dict[str, Any]:
        last_error: BaseException | None = None
        retry_delays = (1.0, 2.0, 4.0)

        for attempt in range(self.max_attempts):
            try:
                payload = self._request_once(keyword)
                results = payload.get("results")
                if not isinstance(results, dict) or not isinstance(results.get("common"), dict):
                    raise JusoServiceError("주소 API 응답 형식이 올바르지 않습니다")
                common = results["common"]
                code = str(common.get("errorCode", ""))
                message = str(common.get("errorMessage", "일시적인 API 오류")).replace(self._api_key, "[승인키]")
                rate_limited = code == "E0007" and any(
                    marker in message for marker in ("다량", "잠시 후", "짧은 시간", "짦은 시간")
                )
                if code != "-999" and not rate_limited:
                    return payload
                last_error = JusoServiceError(
                    f"주소 API 오류 {code}: {message}", code=code
                )
            except HTTPError as exc:
                last_error = exc
                if exc.code not in {429, 500, 502, 503, 504}:
                    raise JusoNetworkError(
                        f"주소 API HTTP 오류가 발생했습니다 ({exc.code})"
                    ) from exc
            except (URLError, TimeoutError, OSError) as exc:
                last_error = exc

            if attempt + 1 >= self.max_attempts:
                break
            delay = retry_delays[min(attempt, len(retry_delays) - 1)]
            self._sleep(delay)

        if isinstance(last_error, JusoServiceError):
            raise last_error
        raise JusoNetworkError(
            "주소 API에 연결하지 못했습니다. 인터넷 연결과 방화벽을 확인해 주세요"
        ) from last_error
