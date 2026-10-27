"""Validate portable roster appearance without including roster data or filters."""
from uuid import UUID


def validate_view_layout(value):
    if not isinstance(value, dict) or value.get("version") != 1:
        raise ValueError("지원하지 않는 화면 설정입니다.")
    sheets = value.get("sheets")
    if not isinstance(sheets, list) or not sheets:
        raise ValueError("가져올 시트가 없습니다.")
    ids, names, cleaned = set(), set(), []
    for sheet in sheets:
        if not isinstance(sheet, dict):
            raise ValueError("시트 설정이 올바르지 않습니다.")
        key, name, layout = sheet.get("id"), sheet.get("name"), sheet.get("layout")
        if (not isinstance(key, str) or not isinstance(name, str) or not name.strip()
                or name != name.strip() or len(name) > 40):
            raise ValueError("시트 이름이 올바르지 않습니다.")
        if key != "main":
            UUID(key)
        if key in ids or name.casefold() in names:
            raise ValueError("시트가 중복되어 있습니다.")
        ids.add(key)
        names.add(name.casefold())
        if not isinstance(layout, dict):
            raise ValueError("칼럼 설정이 올바르지 않습니다.")
        order, hidden, widths = layout.get("order"), layout.get("hidden"), layout.get("widths")
        if (not isinstance(order, list) or not all(isinstance(k, str) for k in order)
                or len(order) != len(set(order)) or not isinstance(hidden, list)
                or not all(isinstance(k, str) for k in hidden) or not isinstance(widths, dict)
                or not all(isinstance(k, str) and type(w) is int and 0 < w <= 100000
                           for k, w in widths.items())):
            raise ValueError("칼럼 순서·너비·숨김 설정이 올바르지 않습니다.")
        alignments = layout.get("alignments", {})
        if not isinstance(alignments, dict) or any(
            not isinstance(k, str) or type(v) is not int or v not in {33, 65, 129, 34, 66, 130, 36, 68, 132}
            for k, v in alignments.items()
        ):
            raise ValueError("칼럼 가로·세로 정렬 설정이 올바르지 않습니다.")
        cleaned.append({"id": key, "name": name, "layout": {
            **({"alignments": dict(alignments)} if alignments else {}),
            "order": list(order), "hidden": list(hidden), "widths": dict(widths)}})
    active = value.get("active")
    if active not in ids:
        raise ValueError("선택한 시트가 목록에 없습니다.")
    aliases = value.get("aliases", {})
    if not isinstance(aliases, dict) or not all(
        isinstance(k, str) and not k.startswith("custom:") and isinstance(v, str)
        and v.strip() and len(v) <= 40 for k, v in aliases.items()
    ):
        raise ValueError("칼럼 표시 이름이 올바르지 않습니다.")
    return {"version": 1, "sheets": cleaned, "active": active, "aliases": dict(aliases)}
