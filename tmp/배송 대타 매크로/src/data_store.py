import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from src.config import A4_PAGE, SPLIT_MODE


@dataclass(frozen=True)
class ColState:
    order: list[str]
    enabled: dict[str, bool]


@dataclass(frozen=True)
class Settings:
    db_path: str
    cols: ColState
    orientation: Literal["자동", "세로", "가로"] = A4_PAGE
    split_mode: Literal["사용 안함", "동기준", "행기준"] = SPLIT_MODE


def settings_path() -> Path:
    app_name = "ExcelDeliveryMacro"

    if os.name == "nt":  # Windows
        base_path = Path(os.environ.get("APPDATA", Path.home())) / app_name
    else:  # macOS / Linux
        base_path = Path.home() / f".{app_name}"

    # 폴더가 없으면 생성
    base_path.mkdir(parents=True, exist_ok=True)
    return base_path / "data.json"


def _default(pick_cols: list[str]) -> Settings:
    return Settings(
        db_path="",
        cols=ColState(order=pick_cols[:], enabled={c: True for c in pick_cols}),
    )


def _normalize(settings: Settings, pick_cols: list[str]) -> Settings:
    order = [c for c in settings.cols.order if c in pick_cols]
    for c in pick_cols:
        if c not in order:
            order.append(c)

    enabled = {c: bool(settings.cols.enabled.get(c, True)) for c in pick_cols}
    db_path = settings.db_path if isinstance(settings.db_path, str) else ""
    return Settings(db_path=db_path, cols=ColState(order=order, enabled=enabled))


def load_settings(pick_cols: list[str]) -> Settings:
    p = settings_path()
    if not p.exists():
        return _default(pick_cols)

    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        cols = raw.get("columns", {})
        st = Settings(
            db_path=str(raw.get("db_path", "")),
            cols=ColState(
                order=list(cols.get("order", [])),
                enabled=dict(cols.get("enabled", {})),
            ),
        )
        return _normalize(st, pick_cols)
    except Exception:
        return _default(pick_cols)


def save_settings(db_path: str, order: list[str], enabled: dict[str, bool]) -> None:
    p = settings_path()
    data: dict[str, Any] = {
        "db_path": db_path,
        "columns": {"order": order, "enabled": enabled},
    }
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
