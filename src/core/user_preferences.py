"""Durable user preferences backed by the application's existing QSettings keys."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

from PyQt6.QtCore import QSettings

from .settings import SettingsManager, get_settings, get_template_profile

_DB_IMPORT_CACHE_KEY = "annual_workbook/header_mapping_cache"
_DB_IMPORT_CACHE_LIMIT = 20


class UserPreferences:
    """Typed facade for user-managed state shared across application tabs."""

    def __init__(
        self,
        settings_manager: SettingsManager | None = None,
        qsettings: QSettings | None = None,
    ) -> None:
        manager = settings_manager or get_settings()
        self._settings = qsettings or QSettings(
            manager.get("app", "organization"),
            manager.get("app", "title"),
        )

    def get_meal_type(self) -> str:
        return get_template_profile(str(self._settings.value("meal_type", ""))).key

    def set_meal_type(self, meal_type: str) -> str:
        normalized = get_template_profile(meal_type).key
        self._settings.setValue("meal_type", normalized)
        self._settings.sync()
        return normalized

    def get_annual_workbook_path(self) -> str:
        return str(self._settings.value("annual_workbook/last_path", "") or "")

    def set_annual_workbook_path(self, path: str) -> None:
        self._settings.setValue("annual_workbook/last_path", path)
        self._settings.sync()

    def get_db_header_mapping(self) -> dict[str, str]:
        value = self._settings.value("annual_workbook/header_mapping", None)
        if not isinstance(value, Mapping):
            return {}
        return {
            str(role): str(header)
            for role, header in value.items()
            if isinstance(role, str) and isinstance(header, str)
        }

    def set_db_header_mapping(self, mapping: Mapping[str, str]) -> None:
        self._settings.setValue("annual_workbook/header_mapping", dict(mapping))
        self._settings.sync()

    def get_db_selected_year(self) -> int | None:
        value = self._settings.value("annual_workbook/selected_year", None)
        if isinstance(value, bool):
            return None
        try:
            year = int(value)
        except (TypeError, ValueError):
            return None
        return year if year >= 1 else None

    def set_db_selected_year(self, year: int) -> None:
        if isinstance(year, bool) or not isinstance(year, int) or year < 1:
            raise ValueError("DB 선택 연도는 1 이상의 정수여야 합니다.")
        self._settings.setValue("annual_workbook/selected_year", year)
        self._settings.sync()

    def get_cached_db_import(
        self,
        headers: Sequence[str],
    ) -> tuple[dict[str, str], int] | None:
        """같은 비날짜 헤더 구조에 저장된 DB 매핑과 연도를 반환한다."""
        cache = self._read_db_import_cache()
        entry = cache.get(_db_header_signature(headers))
        if entry is None:
            # 이전 버전의 순서 의존 키도 같은 헤더 집합이면 재사용한다.
            entry = next(
                (
                    candidate
                    for signature, candidate in cache.items()
                    if _db_header_signature_from_cache_key(signature)
                    == _db_header_signature(headers)
                ),
                None,
            )
        if not isinstance(entry, Mapping):
            return None
        raw_mapping = entry.get("mapping")
        raw_year = entry.get("year")
        if not isinstance(raw_mapping, Mapping) or isinstance(raw_year, bool):
            return None
        mapping = {
            str(role): str(header)
            for role, header in raw_mapping.items()
            if isinstance(role, str) and isinstance(header, str)
        }
        try:
            year = int(raw_year)
        except (TypeError, ValueError):
            return None
        if not mapping or year < 1:
            return None
        return mapping, year

    def set_cached_db_import(
        self,
        headers: Sequence[str],
        mapping: Mapping[str, str],
        year: int,
    ) -> None:
        """DB 헤더 구조별 설정을 최근 20개까지 영구 캐시한다."""
        if isinstance(year, bool) or not isinstance(year, int) or year < 1:
            raise ValueError("DB 선택 연도는 1 이상의 정수여야 합니다.")
        signature = _db_header_signature(headers)
        cache = self._read_db_import_cache()
        cache.pop(signature, None)
        cache[signature] = {
            "mapping": dict(mapping),
            "year": year,
        }
        while len(cache) > _DB_IMPORT_CACHE_LIMIT:
            del cache[next(iter(cache))]
        self._settings.setValue(
            _DB_IMPORT_CACHE_KEY,
            json.dumps(cache, ensure_ascii=False),
        )
        self._settings.sync()

    def _read_db_import_cache(self) -> dict[str, object]:
        value = self._settings.value(_DB_IMPORT_CACHE_KEY, "")
        if not isinstance(value, str) or not value:
            return {}
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        if not isinstance(parsed, dict):
            return {}
        return {
            key: entry
            for key, entry in parsed.items()
            if isinstance(key, str)
        }

    def get_output_dir(self) -> str:
        return str(self._settings.value("output_dir", "") or "")

    def set_output_dir(self, path: str) -> None:
        self._settings.setValue("output_dir", path)
        self._settings.sync()

    def get_invoice_column_mapping(self) -> dict[str, str]:
        value = self._settings.value("invoice/col_mapping", None)
        if not isinstance(value, Mapping):
            return {}
        return {
            str(key): str(mapped)
            for key, mapped in value.items()
            if isinstance(key, str) and isinstance(mapped, str)
        }

    def set_invoice_column_mapping(self, mapping: Mapping[str, str | None]) -> None:
        self._settings.setValue("invoice/col_mapping", dict(mapping))
        self._settings.sync()

    def get_staff_order(self, meal: str) -> list[str]:
        value = self._settings.value(f"report/staff_order_{meal}", None)
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                return []
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
            return []
        return [item for item in value if isinstance(item, str)]

    def set_staff_order(self, meal: str, names: Sequence[str]) -> None:
        value = [name for name in names if isinstance(name, str)]
        self._settings.setValue(
            f"report/staff_order_{meal}", json.dumps(value, ensure_ascii=False)
        )
        self._settings.sync()


def _db_header_signature(headers: Sequence[str]) -> str:
    # 매핑은 헤더 이름을 가리키므로 물리적 열 순서는 캐시 키가 아니다.
    normalized = sorted(str(header) for header in headers)
    return json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))


def _db_header_signature_from_cache_key(value: str) -> str | None:
    try:
        headers = json.loads(value)
    except json.JSONDecodeError:
        return None
    if not isinstance(headers, list) or not all(
        isinstance(header, str) for header in headers
    ):
        return None
    return _db_header_signature(headers)
