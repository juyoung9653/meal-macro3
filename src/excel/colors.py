"""Excel 색상 유틸리티 — openpyxl PatternFill 생성."""

from openpyxl.styles import PatternFill


def fill_color(hex_rgb: str) -> PatternFill:
    """HEX RGB 문자열 → openpyxl PatternFill (solid)."""
    hx = hex_rgb.strip().lstrip("#")
    if len(hx) < 6:
        raise ValueError(f"Invalid hex color: {hex_rgb!r}")
    r, g, b = int(hx[0:2], 16), int(hx[2:4], 16), int(hx[4:6], 16)
    argb = f"FF{r:02X}{g:02X}{b:02X}"
    return PatternFill(
        start_color=argb,
        end_color=argb,
        fill_type="solid",
    )


def group_fill(n: int) -> PatternFill | None:
    _FILL = {2: "E6B8B7", 3: "B8CCE4", 4: "D8E4BC", 5: "CCC0DA"}
    return fill_color(_FILL[n]) if n in _FILL else None
