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
        "report_path": "assets/report_template.xlsx",
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
        "password": "",
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
    # ── 배송 대타 매크로 설정 ────────────────────────────
    "delivery_substitute": {
        "sheet_name": "종합_jon(DB)",
        "header_row": 2,
        "data_row": 4,
        "col_meal": "식사 구분",
        "col_ship": "배송_new",
        "col_ord": "배송순서",
        "col_name": "이름",
        "col_sum": "요약",
        "col_addr": "신주소",
        "col_note": "비고2",
        "pick_cols": [
            "배송순서",
            "관할동",
            "이름",
            "상세주소",
            "신주소",
            "핸드폰",
            "비고2",
        ],
        "max_rows": 18,
        "a4_page": "세로",
        "page_h": 842,
        "page_w": 595,
        "split_mode": "동기준",
        "font_base_size": 8,
    },
    "delivery": {
        "db_path": "",
        "cols": {
            "order": [
                "배송순서",
                "관할동",
                "이름",
                "상세주소",
                "신주소",
                "핸드폰",
                "비고2",
            ],
            "enabled": {
                "배송순서": True,
                "관할동": True,
                "이름": True,
                "상세주소": True,
                "신주소": True,
                "핸드폰": True,
                "비고2": True,
            },
        },
        "orientation": "세로",
        "split_mode": "동기준",
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

    def get_invoice_template_path(self, meal_type: str | None = None) -> str:
        """Compute invoice template path from meal type."""
        mt = meal_type or self.get_meal_type()
        return f"assets/{mt}_청구서템플릿.xlsx"

    def get_confirm_template_path(self, meal_type: str | None = None) -> str:
        """Compute confirm template path from meal type."""
        mt = meal_type or self.get_meal_type()
        return f"assets/{mt}_확인서템플릿.xlsx"

    def set_meal_type(self, meal_type: str):
        self.set("app", "meal_type", value=meal_type)
        # Template paths are now resolved lazily via get_invoice_template_path()
        # and get_confirm_template_path() — no side effects

    # ── 배송 대타 설정 접근자 ────────────────────────────

    def get_delivery_config(self, key: str) -> Any:
        """delivery_substitute 설정에서 단일 값을 조회.

        Example: ``s.get_delivery_config("max_rows")`` → ``18``
        """
        return self.get("delivery_substitute", key)

    def get_delivery_settings(self) -> dict:
        """delivery 설정 전체 dict 반환."""
        return self.get("delivery", default={})

    def set_delivery_settings(
        self,
        db_path: str = "",
        order: list[str] | None = None,
        enabled: dict[str, bool] | None = None,
        orientation: str = "세로",
        split_mode: str = "동기준",
    ) -> None:
        """delivery 설정을 일괄 갱신."""
        if order is not None:
            self.set("delivery", "cols", "order", value=order)
        if enabled is not None:
            self.set("delivery", "cols", "enabled", value=enabled)
        self.set("delivery", "db_path", value=db_path)
        self.set("delivery", "orientation", value=orientation)
        self.set("delivery", "split_mode", value=split_mode)


def get_settings() -> SettingsManager:
    """싱글턴 SettingsManager 반환."""
    global SettingsManager
    if SettingsManager._instance is None:
        SettingsManager._instance = SettingsManager()
    return SettingsManager._instance
