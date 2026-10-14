"""청구서·확인서에서 공유하는 표시 주소 형식."""

FULL_ADDRESS_FIELD = "전체주소"


def combine_address(road_address: object, detail_address: object) -> str:
    """도로명주소와 상세주소를 공백 하나로 연결한 표시 주소를 만든다."""
    parts: list[str] = []
    for value in (road_address, detail_address):
        if value is None:
            continue
        text = " ".join(str(value).split())
        if text:
            parts.append(text)
    return " ".join(parts)
