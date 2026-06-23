"""중앙 설정 관리자 — DEFAULTS dict 기반, 모든 하드코딩 상수를 대체."""

from __future__ import annotations

import copy
from typing import Any

# ── 기본 설정값 ──────────────────────────────────────
DEFAULTS: dict[str, Any] = {
    "app": {
        "title": "급식비 정산 프로그램",
        "organization": "분당구",
        "window_width": 1000,
        "window_height": 700,
        "meal_type": "급식",
    },
    "templates": {
        "invoice_path": "assets/급식_청구서템플릿.xlsx",
        "confirm_path": "assets/급식_확인서템플릿.xlsx",
        "sheet_name": "학기_총괄",
        "data_start_row": 4,
        "confirm_sheet_names": ["1명", "2명", "3명", "4명", "5명"],
        "data_columns": 11,
    },
    "confirm": {
        "title_suffix": "분당구 급식확인서",
        "output_filename_format": "확인서_{year:04d}{month:02d}.xlsx",
    },
    "invoice": {
        "output_filename_format": "청구서_{year:04d}{month:02d}.xlsx",
    },
    "db": {
        "path": "assets/db.xlsx",
    },
    "semantic_columns": {
        "__calc_breakfast": {
            "label": "조식 건수 입력",
            "patterns": ["조식"],
            "default": "조식",
        },
        "__calc_dinner": {
            "label": "석식 건수 입력",
            "patterns": ["석식"],
            "default": "석식",
        },
        "__calc_lunch": {
            "label": "중식 건수 입력",
            "patterns": ["중식"],
            "default": "중식",
        },
        "__calc_total": {
            "label": "합계(계) 수식",
            "patterns": ["계"],
            "default": "계",
        },
        "__calc_price": {
            "label": "청구금액 수식",
            "patterns": ["청구금액"],
            "default": "청구금액",
        },
    },
}


class SettingsManager:
    """중앙 설정 관리자 싱글턴.

    Usage::

        from src.core.settings import get_settings
        s = get_settings()
        title = s.get("app", "title")
        schedule = s.get("schedule")  # 전체 dict
    """

    _instance: "SettingsManager | None" = None

    def __init__(self):
        self._data: dict = copy.deepcopy(DEFAULTS)

    def _deep_merge(self, base: dict, override: dict):
        """중첩 dict 재귀 병합."""
        for k, v in override.items():
            if k in base and isinstance(base[k], dict) and isinstance(v, dict):
                self._deep_merge(base[k], v)
            else:
                base[k] = v

    def get(self, *keys: str, default: Any = None) -> Any:
        """중첩 키로 설정값 조회.

        Example: ``s.get("app", "title")`` → ``"급식비 정산 프로그램"``
        """
        from functools import reduce

        try:
            return reduce(lambda d, k: d[k], keys, self._data)
        except (KeyError, TypeError):
            return default

    def set(self, *keys: str, value: Any):
        """중첩 키로 설정값 변경.

        Example: ``s.set("app", "title", value="")``
        """
        node = self._data
        for k in keys[:-1]:
            if k not in node or not isinstance(node[k], dict):
                node[k] = {}
            node = node[k]
        node[keys[-1]] = value

    # ── Typed accessors (only those with logic beyond simple get) ──

    def get_app_window_size(self) -> tuple[int, int]:
        return (
            int(self.get("app", "window_width", default=1000)),
            int(self.get("app", "window_height", default=700)),
        )

    def get_meal_type(self) -> str:
        return self.get("app", "meal_type", default="급식")

    def set_meal_type(self, meal_type: str):
        self.set("app", "meal_type", value=meal_type)
        # Sync template paths
        self.set(
            "templates", "invoice_path", value=f"assets/{meal_type}_청구서템플릿.xlsx"
        )
        self.set(
            "templates", "confirm_path", value=f"assets/{meal_type}_확인서템플릿.xlsx"
        )

    def get_all(self) -> dict:
        """전체 설정 dict 반환 (복사본)."""
        return copy.deepcopy(self._data)

    def reset_to_defaults(self):
        """모든 설정을 기본값으로 초기화."""
        self._data = copy.deepcopy(DEFAULTS)


def get_settings() -> SettingsManager:
    """싱글턴 SettingsManager 반환."""
    global SettingsManager
    if SettingsManager._instance is None:
        SettingsManager._instance = SettingsManager()
    return SettingsManager._instance
