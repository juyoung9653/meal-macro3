def bgr(hex_rgb: str) -> int:
    hx = hex_rgb.strip().lstrip("#")
    r = int(hx[0:2], 16)
    g = int(hx[2:4], 16)
    b = int(hx[4:6], 16)
    return (b << 16) | (g << 8) | r


def set_fill(rng, color: int) -> None:
    rng.Interior.Pattern = 1
    rng.Interior.Color = color
