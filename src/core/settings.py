"""중앙 설정 관리자 — JSON 파일 기반, 모든 하드코딩 상수를 대체.

"schedule" dict keys ("breakfast"/"dinner"/"lunch") correspond
to ``MealType`` enum values in ``src.meal_types``.

Persistence separation:
  - SettingsManager (.settings.json) → app config (paths, schedule, display)
  - QSettings (per-tab) → ephemeral UI state (last file, window position, col mapping)
  Use SettingsManager for anything that should survive a settings reset.
"""

from __future__ import annotations

import copy
import json
import os
import sys
from typing import Any

# ── 기본 설정값 ──────────────────────────────────────
DEFAULTS: dict[str, Any] = {
    "cache": {
        "placeholder_cache": ".cache.json",
    },
    "app": {
        "title": "급식비 정산 프로그램",
        "organization": "분당구",
        "window_width": 1000,
        "window_height": 700,
    },
    "templates": {
        "invoice_path": "assets/청구서템플릿.xlsx",
        "confirm_path": "assets/확인서템플릿.xlsx",
        "roster_example_path": "assets/명단예시.xlsx",
        "sheet_name": "학기_총괄",
        "data_start_row": 4,
        "confirm_sheet_names": ["1명", "2명", "3명", "4명", "5명"],
        "data_columns": 11,
    },
    "schedule": {
        "breakfast_days": [4, 5, 6],  # 금요일 (0=월, 6=일)
        "dinner_days": [0, 1, 2, 3],  # 월~목
        "lunch_days": [0, 1, 2, 3, 4],  # 월~금
    },
    "confirm": {
        "title_suffix": "분당구 급식확인서",
        "output_filename_format": "확인서_{year:04d}{month:02d}.xlsx",
        "col_width_g": 12.5,
        "col_width_h": 12.5,
        "col_width_i": 12.5,
        "col_width_j": 12.5,
    },
    "invoice": {
        "output_filename_format": "청구서_{year:04d}{month:02d}.xlsx",
    },
    "daily_delivery": {
        "output_filename_format": "일일배송_{start:%Y%m%d}_{end:%Y%m%d}.xlsx",
        "template_path": "assets/일일배송템플릿.xlsx",
        "lunch_template_path": "assets/중식일일배송템플릿.xlsx",
    },
    "roster": {
        "name_keywords": ["성명", "이름", "성함", "학생명", "학생", "name", "Name"],
        "preview_name_kw": ["성명", "이름", "성함"],
        "preview_dept_kw": ["학교명", "학교", "소속"],
        "preview_note_kw": ["학기중 비고", "비고", "특이사항"],
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
    "flag_fields": {
        "__breakfast": {
            "label": "조식 여부",
            "tooltip": "1=해당, 그 외/공란=미해당 (금요일 배송)",
        },
        "__dinner": {
            "label": "석식 여부",
            "tooltip": "1=해당, 그 외/공란=미해당 (월~목 배송)",
        },
        "__vacation": {
            "label": "방중 여부",
            "tooltip": "1=해당, 그 외/공란=미해당 (방학중 월~금 배송)",
        },
        "_daily_driver": {
            "label": "배송 담당",
            "tooltip": "중식 일일배송을 담당하는 기사 또는 담당자 컬럼",
        },
        "_daily_region_primary": {
            "label": "담당 구역 1순위",
            "tooltip": "값이 있으면 중식 일일배송 구역으로 먼저 사용",
        },
        "_daily_region_secondary": {
            "label": "담당 구역 2순위",
            "tooltip": "1순위 값이 비어 있을 때만 사용하는 선택 컬럼",
        },
        "__note": {"label": "비고 (일시중지 등)", "tooltip": ""},
    },
    "flag_search_terms": {
        "__breakfast": ["조식"],
        "__dinner": ["석식"],
        "__vacation": ["중식", "방중"],
        "_daily_driver": ["배송담당", "배송 담당", "담당기사", "배송기사", "기사"],
        "_daily_region_primary": [
            "담당 구역 1순위",
            "담당구역 1순위",
            "담당구역1순위",
            "배송구역",
        ],
        "_daily_region_secondary": [
            "담당 구역 2순위",
            "담당구역 2순위",
            "담당구역2순위",
            "관할동",
        ],
        "__note": ["비고"],
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
        self._path: str | None = None

    def _get_path(self) -> str:
        if self._path is None:
            if getattr(sys, "frozen", False):
                root = os.path.dirname(sys.executable)
            else:
                # settings.py → core/ → src/ → project root
                root = os.path.dirname(
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                )
            self._path = os.path.join(root, ".settings.json")
        return self._path

    def _migrate_legacy(self):
        """src/.settings.json → 루트 .settings.json 으로 마이그레이션 (1회 실행)."""
        legacy_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            ".settings.json",
        )
        target_path = self._get_path()
        if os.path.exists(legacy_path) and not os.path.exists(target_path):
            try:
                import shutil

                shutil.copy2(legacy_path, target_path)
                os.remove(legacy_path)
            except OSError:
                pass

    def load(self):
        """JSON 파일에서 설정 로드 (없으면 기본값 유지)."""
        self._migrate_legacy()
        path = self._get_path()
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                self._deep_merge(self._data, loaded)
        except (json.JSONDecodeError, OSError):
            pass

    def save(self):
        """현재 설정을 JSON 파일로 저장."""
        path = self._get_path()
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
        except OSError:
            pass

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

    # ── Typed accessors ──

    def get_app_title(self) -> str:
        return str(self.get("app", "title", default="급식비 정산 프로그램"))

    def get_app_organization(self) -> str:
        return str(self.get("app", "organization", default="분당구"))

    def get_app_window_size(self) -> tuple[int, int]:
        return (
            int(self.get("app", "window_width", default=1000)),
            int(self.get("app", "window_height", default=700)),
        )

    def get_invoice_template_path(self) -> str:
        return str(
            self.get("templates", "invoice_path", default="assets/청구서템플릿.xlsx")
        )

    def get_confirm_template_path(self) -> str:
        return str(
            self.get("templates", "confirm_path", default="assets/확인서템플릿.xlsx")
        )

    def get_template_sheet_name(self) -> str:
        return str(self.get("templates", "sheet_name", default="학기_총괄"))

    def get_data_start_row(self) -> int:
        return int(self.get("templates", "data_start_row", default=4))

    def get_data_columns(self) -> int:
        return int(self.get("templates", "data_columns", default=11))

    def get_confirm_sheet_names(self) -> list[str]:
        return list(
            self.get(
                "templates",
                "confirm_sheet_names",
                default=["1명", "2명", "3명", "4명", "5명"],
            )
        )

    def get_schedule_days(self, meal_type: str) -> set[int]:
        """배송 요일 설정 조회. meal_type: 'breakfast', 'dinner', 'lunch' (또는 'breakfast_days' 등)"""
        key = f"{meal_type}_days" if "_days" not in meal_type else meal_type
        return set(self.get("schedule", key, default=[]))

    def get_roster_name_keywords(self) -> list[str]:
        return list(
            self.get(
                "roster",
                "name_keywords",
                default=["성명", "이름", "성함", "학생명", "학생", "name", "Name"],
            )
        )

    def get_flag_fields(self) -> dict:
        return dict(self.get("flag_fields", default={}))

    def get_flag_search_terms(self) -> dict:
        return dict(self.get("flag_search_terms", default={}))

    def get_confirm_title_suffix(self) -> str:
        return str(self.get("confirm", "title_suffix", default="분당구 급식확인서"))

    def get_confirm_output_filename_format(self) -> str:
        return str(
            self.get(
                "confirm",
                "output_filename_format",
                default="확인서_{year:04d}{month:02d}.xlsx",
            )
        )

    def get_roster_example_path(self) -> str:
        return str(
            self.get("templates", "roster_example_path", default="assets/명단예시.xlsx")
        )

    def get_confirm_col_widths(self) -> dict[str, float]:
        return {
            "G": float(self.get("confirm", "col_width_g", default=12.5)),
            "H": float(self.get("confirm", "col_width_h", default=12.5)),
            "I": float(self.get("confirm", "col_width_i", default=12.5)),
            "J": float(self.get("confirm", "col_width_j", default=12.5)),
        }

    def get_invoice_output_filename_format(self) -> str:
        return str(
            self.get(
                "invoice",
                "output_filename_format",
                default="청구서_{year:04d}{month:02d}.xlsx",
            )
        )

    def get_daily_delivery_output_filename_format(self) -> str:
        return str(
            self.get(
                "daily_delivery",
                "output_filename_format",
                default="일일배송_{start:%Y%m%d}_{end:%Y%m%d}.xlsx",
            )
        )

    def get_daily_delivery_template_path(self) -> str:
        return str(
            self.get(
                "daily_delivery",
                "template_path",
                default="assets/일일배송템플릿.xlsx",
            )
        )

    def get_daily_delivery_lunch_template_path(self) -> str:
        return str(
            self.get(
                "daily_delivery",
                "lunch_template_path",
                default="assets/중식일일배송템플릿.xlsx",
            )
        )

    def get_semantic_columns(self) -> dict:
        return dict(self.get("semantic_columns", default={}))

    def get_all(self) -> dict:
        """전체 설정 dict 반환 (복사본)."""
        return copy.deepcopy(self._data)

    def reset_to_defaults(self):
        """모든 설정을 기본값으로 초기화."""
        self._data = copy.deepcopy(DEFAULTS)


def get_settings() -> SettingsManager:
    """싱글턴 SettingsManager 반환 (최초 호출 시 자동 로드)."""
    if SettingsManager._instance is None:
        SettingsManager._instance = SettingsManager()
        SettingsManager._instance.load()
    return SettingsManager._instance


def save_settings():
    """현재 설정을 파일로 저장."""
    get_settings().save()
