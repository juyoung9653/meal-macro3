"""중앙 설정 관리자 — DEFAULTS dict 기반, 모든 하드코딩 상수를 대체."""

from __future__ import annotations

import copy
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class TemplateProfile:
    """Vendor-specific workbook assets identified by the persisted meal type."""

    key: str
    label: str
    invoice_template_path: str
    confirm_template_path: str


TEMPLATE_PROFILES: dict[str, TemplateProfile] = {
    "급식": TemplateProfile(
        key="급식",
        label="급식",
        invoice_template_path="assets/급식/급식_청구서템플릿.xlsx",
        confirm_template_path="assets/급식/급식_확인서템플릿.xlsx",
    ),
    "행복도시락": TemplateProfile(
        key="행복도시락",
        label="행복도시락",
        invoice_template_path="assets/행복도시락/행복도시락_청구서템플릿.xlsx",
        confirm_template_path="assets/행복도시락/행복도시락_확인서템플릿.xlsx",
    ),
}
DEFAULT_TEMPLATE_PROFILE_KEY = "급식"


def get_template_profile(meal_type: str | None = None) -> TemplateProfile:
    """Return the requested profile, falling back to the legacy default."""
    return TEMPLATE_PROFILES.get(meal_type or "", TEMPLATE_PROFILES[DEFAULT_TEMPLATE_PROFILE_KEY])


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
        "invoice_path": "assets/급식/급식_청구서템플릿.xlsx",
        "confirm_path": "assets/급식/급식_확인서템플릿.xlsx",
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
        "unit_prices": {
            "조식": 9500,
            "중식": 9500,
            "석식": 9500,
        },
    },
    "annual_workbook": {
        "path": "",
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
        "col_ord": "배송순서",
        "col_name": "이름",
        "col_sum": "요약",
        "col_addr": "신주소",
        "col_note": "비고2",
        "col_phone": "핸드폰",
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
        "page_h": 842,
        "page_w": 595,
        "font_base_size": 8,
    },
    "delivery": {
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

    _instance: SettingsManager | None = None

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
        meal_type = self.get("app", "meal_type", default=DEFAULT_TEMPLATE_PROFILE_KEY)
        return get_template_profile(str(meal_type)).key

    @staticmethod
    def _resource_path(relative_path: str) -> str:
        """PyInstaller 번들에서는 임시 리소스 루트를 기준으로 경로를 만든다."""
        bundle_root = getattr(sys, "_MEIPASS", None)
        if bundle_root:
            return str(Path(bundle_root) / relative_path)
        return relative_path

    def get_template_profile(self, meal_type: str | None = None) -> TemplateProfile:
        return get_template_profile(meal_type or self.get_meal_type())

    def get_invoice_template_path(self, meal_type: str | None = None) -> str:
        """Return the selected profile's invoice template path."""
        return self._resource_path(
            self.get_template_profile(meal_type).invoice_template_path
        )

    def get_confirm_template_path(self, meal_type: str | None = None) -> str:
        """Return the selected profile's confirm template path."""
        return self._resource_path(
            self.get_template_profile(meal_type).confirm_template_path
        )

    def get_substitute_template_path(self) -> str:
        """배송대타의 편집용 기본 Excel 양식 경로."""
        return self._resource_path("assets/substitute_template.xlsx")

    def get_report_template_path(self) -> str:
        """배송리포트 템플릿의 실행 환경별 실제 경로를 반환한다."""
        configured = self.get(
            "templates", "report_path", default="assets/report_template.xlsx"
        )
        return self._resource_path(str(configured))

    def set_meal_type(self, meal_type: str):
        self.set("app", "meal_type", value=get_template_profile(meal_type).key)
        # Template paths are resolved lazily via the selected profile.

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
        self.set("delivery", "orientation", value=orientation)
        self.set("delivery", "split_mode", value=split_mode)


def get_settings() -> SettingsManager:
    """싱글턴 SettingsManager 반환."""
    if SettingsManager._instance is None:
        SettingsManager._instance = SettingsManager()
    return SettingsManager._instance
