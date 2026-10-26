import json
from urllib.parse import parse_qs

import pytest

from src.services.juso_api import (
    AddressNotFoundError,
    AmbiguousAddressError,
    InvalidAddressError,
    JusoApiClient,
    JusoServiceError,
)


class FakeResponse:
    def __init__(self, payload):
        self._raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self._raw


class FakeOpener:
    def __init__(self, *payloads):
        self.payloads = list(payloads)
        self.requests = []

    def __call__(self, request, _timeout):
        self.requests.append(request)
        payload = self.payloads[min(len(self.requests) - 1, len(self.payloads) - 1)]
        return FakeResponse(payload)


def response(*candidates, total=None, code="0", message="정상"):
    return {
        "results": {
            "common": {
                "errorCode": code,
                "errorMessage": message,
                "totalCount": str(len(candidates) if total is None else total),
            },
            "juso": list(candidates),
        }
    }


def candidate(road, jibun, part1=None, *, road_name="", main="", sub=""):
    value = {
        "roadAddr": road,
        "roadAddrPart1": part1 or road,
        "jibunAddr": jibun,
    }
    if road_name:
        value["rn"] = road_name
    if main != "":
        value["buldMnnm"] = str(main)
    if sub != "":
        value["buldSlno"] = str(sub)
    return value


def client_for(opener, **kwargs):
    return JusoApiClient(
        "secret-key",
        opener=opener,
        minimum_interval=0,
        max_attempts=kwargs.pop("max_attempts", 1),
        sleeper=kwargs.pop("sleeper", lambda _seconds: None),
        **kwargs,
    )


def test_exact_road_part_selects_jibun_and_uses_post_body():
    opener = FakeOpener(
        response(
            candidate(
                "서울특별시 마포구 성암로 301(상암동)",
                "서울특별시 마포구 상암동 1595",
                "서울특별시 마포구 성암로 301",
            )
        )
    )
    result = client_for(opener).lookup("서울특별시 마포구 성암로 301")

    assert result.jibun_address == "서울특별시 마포구 상암동 1595"
    request = opener.requests[0]
    assert "secret-key" not in request.full_url
    body = parse_qs(request.data.decode("utf-8"))
    assert body["confmKey"] == ["secret-key"]
    assert body["resultType"] == ["json"]


def test_normalized_duplicate_address_uses_cache():
    opener = FakeOpener(
        response(candidate("세종특별자치시 도움6로 42", "세종특별자치시 어진동 1"))
    )
    client = client_for(opener)

    first = client.lookup("세종특별자치시  도움6로 42")
    second = client.lookup("세종특별자치시 도움6로 42")

    assert first == second
    assert len(opener.requests) == 1


def test_multiple_unmatched_results_are_not_silently_guessed():
    opener = FakeOpener(
        response(
            candidate("서울특별시 중구 중앙로 10", "서울특별시 중구 A동 1"),
            candidate("서울특별시 중구 중앙로 11", "서울특별시 중구 B동 2"),
        )
    )

    with pytest.raises(AmbiguousAddressError):
        client_for(opener).lookup("서울특별시 중구 중앙로")


def test_first_result_can_be_explicitly_allowed():
    opener = FakeOpener(
        response(
            candidate("서울특별시 중구 중앙로 10", "서울특별시 중구 A동 1"),
            candidate("서울특별시 중구 중앙로 11", "서울특별시 중구 B동 2"),
        )
    )

    result = client_for(opener, allow_first_match=True).lookup("서울특별시 중구 중앙로")

    assert result.jibun_address == "서울특별시 중구 A동 1"


def test_exact_building_number_beats_hyphenated_neighbor():
    opener = FakeOpener(
        response(
            candidate(
                "경기도 화성시 샘물로8번길 20(새솔동)",
                "경기도 화성시 새솔동 100",
                "경기도 화성시 샘물로8번길 20",
                road_name="샘물로8번길",
                main=20,
                sub=0,
            ),
            candidate(
                "경기도 화성시 샘물로8번길 20-1(새솔동)",
                "경기도 화성시 새솔동 100-1",
                "경기도 화성시 샘물로8번길 20-1",
                road_name="샘물로8번길",
                main=20,
                sub=1,
            ),
        )
    )

    result = client_for(opener).lookup("화성시 샘물로8번길 20")

    assert result.road_address.endswith("20(새솔동)")
    assert result.jibun_address == "경기도 화성시 새솔동 100"


def test_hyphenated_building_number_selects_matching_sub_number():
    opener = FakeOpener(
        response(
            candidate(
                "경기도 화성시 샘물로8번길 20(새솔동)",
                "경기도 화성시 새솔동 100",
                road_name="샘물로8번길",
                main=20,
                sub=0,
            ),
            candidate(
                "경기도 화성시 샘물로8번길 20-1(새솔동)",
                "경기도 화성시 새솔동 100-1",
                road_name="샘물로8번길",
                main=20,
                sub=1,
            ),
        )
    )

    result = client_for(opener).lookup("화성시 샘물로8번길 20-1")

    assert result.jibun_address == "경기도 화성시 새솔동 100-1"


def test_no_result_is_an_address_level_failure():
    opener = FakeOpener(response())
    with pytest.raises(AddressNotFoundError):
        client_for(opener).lookup("서울특별시 없는로 999")


def test_no_result_retries_before_comma_and_restores_detail_suffix():
    opener = FakeOpener(
        response(),
        response(
            candidate(
                "서울특별시 마포구 성암로 301(상암동)",
                "서울특별시 마포구 상암동 1595",
                "서울특별시 마포구 성암로 301",
                road_name="성암로",
                main=301,
                sub=0,
            )
        ),
    )

    result = client_for(opener).lookup(
        "서울특별시 마포구 성암로 301, 101동 202호"
    )

    assert result.jibun_address == "서울특별시 마포구 상암동 1595, 101동 202호"
    keywords = [
        parse_qs(request.data.decode("utf-8"))["keyword"][0]
        for request in opener.requests
    ]
    assert keywords == [
        "서울특별시 마포구 성암로 301, 101동 202호",
        "서울특별시 마포구 성암로 301",
    ]


def test_comma_retry_selects_exact_building_number_over_hyphenated_neighbor():
    opener = FakeOpener(
        response(),
        response(
            candidate(
                "경기도 화성시 샘물로8번길 20(새솔동)",
                "경기도 화성시 새솔동 100",
                "경기도 화성시 샘물로8번길 20",
                road_name="샘물로8번길",
                main=20,
                sub=0,
            ),
            candidate(
                "경기도 화성시 샘물로8번길 20-1(새솔동)",
                "경기도 화성시 새솔동 100-1",
                "경기도 화성시 샘물로8번길 20-1",
                road_name="샘물로8번길",
                main=20,
                sub=1,
            ),
        ),
    )

    result = client_for(opener).lookup(
        "경기도 화성시 샘물로8번길 20, 101동 202호"
    )

    assert result.road_address.endswith("20(새솔동)")
    assert result.jibun_address == "경기도 화성시 새솔동 100, 101동 202호"


def test_comma_retry_accepts_space_inside_numbered_road_name():
    opener = FakeOpener(
        response(),
        response(
            candidate(
                "경기도 성남시 수정구 남문로90번길 15 (태평동)",
                "정확 후보 구주소",
                "경기도 성남시 수정구 남문로90번길 15",
                road_name="남문로90번길",
                main=15,
                sub=0,
            ),
            candidate(
                "경기도 성남시 수정구 남문로90번길 15-1 (태평동)",
                "인접 후보 구주소",
                "경기도 성남시 수정구 남문로90번길 15-1",
                road_name="남문로90번길",
                main=15,
                sub=1,
            ),
        ),
    )

    result = client_for(opener).lookup("수정구 남문로 90번길 15, B02호")

    assert result.road_address.endswith("15 (태평동)")
    assert result.jibun_address == "정확 후보 구주소, B02호"


def test_invalid_key_is_a_fatal_service_error():
    opener = FakeOpener(
        response(code="E0001", message="승인되지 않은 KEY 입니다.")
    )
    with pytest.raises(JusoServiceError):
        client_for(opener).lookup("서울특별시 종로구 세종대로 209")


def test_api_echoed_key_is_redacted_and_malformed_response_is_observable():
    client = JusoApiClient("synthetic-secret", opener=FakeOpener(
        response(code="E0001", message="승인키 synthetic-secret 오류")), minimum_interval=0)
    with pytest.raises(JusoServiceError) as caught:
        client.lookup("가상로 1")
    assert "synthetic-secret" not in str(caught.value)
    client = JusoApiClient("synthetic-secret", opener=FakeOpener({"results": None}), minimum_interval=0)
    with pytest.raises(JusoServiceError, match="응답 형식"):
        client.lookup("가상로 1")


def test_address_specific_api_rejection_does_not_become_fatal():
    opener = FakeOpener(
        response(code="E0009", message="검색어가 너무 깁니다.")
    )
    with pytest.raises(InvalidAddressError):
        client_for(opener).lookup("서울특별시 종로구 세종대로 209")


def test_transient_system_error_retries():
    sleeps = []
    opener = FakeOpener(
        response(code="-999", message="시스템에러"),
        response(candidate("서울특별시 종로구 세종대로 209", "서울특별시 종로구 세종로 77-6")),
    )

    result = client_for(opener, max_attempts=2, sleeper=sleeps.append).lookup(
        "서울특별시 종로구 세종대로 209"
    )

    assert result.jibun_address == "서울특별시 종로구 세종로 77-6"
    assert sleeps == [1.0]
    assert len(opener.requests) == 2


@pytest.mark.parametrize(
    "value",
    ["", "   ", "서울 > 종로", "서울[종로]", "서울 SELECT 종로"],
)
def test_invalid_local_keyword_is_rejected_before_network(value):
    opener = FakeOpener(response())
    with pytest.raises(InvalidAddressError):
        client_for(opener).lookup(value)
    assert not opener.requests
