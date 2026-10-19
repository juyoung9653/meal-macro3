"""Resolve one document value without conflating missing values with zero."""
from collections.abc import Mapping
from ..core.models import FULL_ADDRESS_FIELD, combine_address


def _source_value(source: Mapping[str, object], key: str) -> object:
    if key == FULL_ADDRESS_FIELD:
        return combine_address(source.get("도로명주소"), source.get("상세주소"))
    return source.get(key) if key else None


def _missing(value: object) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def resolve_mapping_value(source: Mapping[str, object], primary: str, secondary: str = "") -> object:
    value = _source_value(source, primary)
    if _missing(value):
        value = _source_value(source, secondary)
    return "" if _missing(value) else value
