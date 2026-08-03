"""Durable user preferences backed by the application's existing QSettings keys."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

from PyQt6.QtCore import QSettings

from .settings import SettingsManager, get_settings, get_template_profile


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
