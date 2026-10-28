"""Discover literal confirmation placeholders and keep legacy mappings readable."""
from __future__ import annotations

import re

from .template_scanner import scan_placeholders

AUTO_CONFIRMATION = "__auto_confirmation"


def placeholder_aliases(target: str) -> tuple[str, ...]:
    key = target.removeprefix("{").removesuffix("}")
    if re.fullmatch(r"child\d+", key):
        return ("성명", "이름")
    if re.fullmatch(r"carer\d+", key):
        return ("세대주 성명", "세대주\n성명", "세대주성명")
    if re.fullmatch(r"phone\d+", key):
        return ("핸드폰", "전화번호")
    return {"addr": ("도로명주소",), "dong": ("관할동", "동")}.get(key, ())


def automatic_placeholder(key: str) -> bool:
    return key in ("title", "delivery", "d_phone", "memo") or bool(
        re.fullmatch(r"(?:num\d+|week\d+|sum_[bdl]_\d+_\d+)", key))


def placeholder_person_index(key: str) -> int:
    if re.fullmatch(r"week\d+", key):
        return 0
    meal = re.fullmatch(r"sum_[bdl]_(\d+)_\d+", key)
    person = meal or re.fullmatch(r".*?([1-9]\d*)", key)
    return int(person.group(1)) - 1 if person else 0


def confirmation_fields(path, sheet_names, *, named_sources=None):
    """Union of placeholders; absent sheets are warnings, never exclusions."""
    sheets = tuple(sheet_names)
    found = scan_placeholders(str(path), list(sheets))
    keys = tuple(dict.fromkeys(key for sheet in sheets for key in found.get(sheet, {})))
    defaults, missing = {}, {}
    for key in keys:
        target = "{" + key + "}"
        aliases = placeholder_aliases(target)
        defaults[target] = (aliases[0] if aliases else AUTO_CONFIRMATION
                            if automatic_placeholder(key) else (named_sources or {}).get(key, ""))
        absent = tuple(sheet for sheet in sheets if key not in found.get(sheet, {}))
        if absent:
            missing[target] = absent
    return defaults, missing


def placeholder_mapping(defaults, stored):
    result = {}
    for target, default in defaults.items():
        result[target] = next((stored[key] for key in (target, *placeholder_aliases(target))
                               if key in stored), default)
    return result
