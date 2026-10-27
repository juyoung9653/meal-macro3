"""Persisted, key-based column layout support for roster tables."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from typing import Any

from PySide6.QtCore import QObject, QSettings, Qt
from PySide6.QtGui import QActionGroup
from PySide6.QtWidgets import QMenu, QTableView, QWidget


class RosterColumnLayout(QObject):
    """Apply and persist a roster table layout without depending on its model type."""

    SETTINGS_KEY = "roster/columns_v2"
    LEGACY_SETTINGS_KEY = "roster/columns_v1"
    _OLD_MEAL_ORDER = ("status:breakfast", "status:lunch", "status:dinner")
    _NEW_MEAL_ORDER = ("status:dinner", "status:breakfast", "status:lunch")
    MANDATORY_KEYS = frozenset(
        {"name", "status:breakfast", "status:lunch", "status:dinner", "birth_date"}
    )

    def __init__(
        self,
        table: QTableView,
        columns: Sequence[object],
        *,
        settings: QSettings | None = None,
        parent: QObject | None = None,
        settings_key: str | None = None,
        mandatory_keys: frozenset[str] | None = None,
        migrate_legacy: bool = True,
        preserve_temporary_keys: frozenset[str] = frozenset(),
        hide_new_custom_columns: bool = False,
    ) -> None:
        super().__init__(parent or table)
        self._table = table
        self._settings = (
            settings
            if settings is not None
            else QSettings("분당구", "급식 배송 운영 SQLite v2")
        )
        self._columns = tuple(columns)
        self._settings_key = settings_key or self.SETTINGS_KEY
        self._mandatory_keys = self.MANDATORY_KEYS if mandatory_keys is None else mandatory_keys
        self._migrate_legacy = migrate_legacy
        self._preserve_temporary_keys = preserve_temporary_keys
        self._hide_new_custom_columns = hide_new_custom_columns
        self._known_widths = {
            self._key(column): self._width(column) for column in self._columns
        }
        self._applying = False
        self._alignments = {}
        self._model_reset = False
        self._header = table.horizontalHeader()
        self._header.setSectionsMovable(True)
        self._header.sectionMoved.connect(self._save_from_header)
        self._header.sectionResized.connect(self._save_from_header)
        self._connect_model(table.model())
        self._apply_saved_layout()

    @property
    def columns(self) -> tuple[object, ...]:
        return self._columns

    def create_menu(
        self,
        parent: QWidget | None = None,
        *,
        column_key: str | None = None,
        rename_callback: Callable[[str], None] | None = None,
        include_reset: bool = True,
    ) -> QMenu:
        menu = QMenu(parent or self._table)
        target = self._column_for_key(column_key) if column_key is not None else None
        if target is not None and rename_callback is not None:
            rename_action = menu.addAction("칼럼 이름 바꾸기")
            rename_action.triggered.connect(
                lambda _checked=False, key=column_key: rename_callback(key)
            )
        if target is not None and column_key not in self._mandatory_keys:
            hide_action = menu.addAction("숨기기")
            hide_action.setEnabled(not self._is_hidden(column_key))
            hide_action.triggered.connect(
                lambda _checked=False, key=column_key: self._set_hidden(key, True)
            )

        if target is not None and callable(getattr(self._table.model(), "set_column_alignments", None)):
            alignment_menu = QMenu("정렬", menu)
            menu.addMenu(alignment_menu)
            menu._alignment_menu = alignment_menu
            alignment_menu._axis_menus = []
            current = self._table.model().column_alignment(column_key)
            for label, mask, choices in (
                ("가로정렬", Qt.AlignmentFlag.AlignHorizontal_Mask,
                 (("왼쪽", Qt.AlignmentFlag.AlignLeft), ("가운데", Qt.AlignmentFlag.AlignHCenter), ("오른쪽", Qt.AlignmentFlag.AlignRight))),
                ("세로정렬", Qt.AlignmentFlag.AlignVertical_Mask,
                 (("위", Qt.AlignmentFlag.AlignTop), ("가운데", Qt.AlignmentFlag.AlignVCenter), ("아래", Qt.AlignmentFlag.AlignBottom))),
            ):
                submenu = QMenu(label, alignment_menu)
                alignment_menu.addMenu(submenu)
                alignment_menu._axis_menus.append(submenu)
                group = QActionGroup(submenu)
                submenu._alignment_group = group
                group.setExclusive(True)
                for text, flag in choices:
                    action = submenu.addAction(text)
                    action.setCheckable(True)
                    action.setChecked(current & mask == flag)
                    group.addAction(action)
                    action.triggered.connect(lambda checked=False, key=column_key, mask=mask, flag=flag:
                                             self.set_alignment(key, mask, flag))

        hidden_menu = QMenu("숨기기 해제", menu)
        menu.addMenu(hidden_menu)
        hidden_columns = tuple(
            column for column in self._columns if self._is_hidden(self._key(column))
        )
        hidden_menu.setEnabled(bool(hidden_columns))
        for column in hidden_columns:
            key = self._key(column)
            action = hidden_menu.addAction(self._label(column))
            action.triggered.connect(
                lambda _checked=False, column_key=key: self._set_hidden(column_key, False)
            )
        if include_reset:
            menu.addSeparator()
            reset_action = menu.addAction("기본 열 구성으로 되돌리기")
            reset_action.triggered.connect(self.reset)
        return menu

    def replace_columns(self, columns: Sequence[object]) -> None:
        """Apply the saved layout after the table model has acquired *columns*."""
        self._columns = tuple(columns)
        self._model_reset = False
        self._apply_saved_layout()
        # Persist only after the intended layout is back on the header.  This prunes
        # deleted keys and gives newly introduced columns their default width.
        self.save()

    def switch_layout(
        self, settings_key: str, *, migrate_legacy: bool = False,
        hide_new_custom_columns: bool = False,
        save_current: bool = True,
    ) -> None:
        """Save the current sheet before applying another sheet to the same table."""
        if save_current:
            self.save()
        self._settings_key = settings_key
        self._migrate_legacy = migrate_legacy
        self._hide_new_custom_columns = hide_new_custom_columns
        self._known_widths = {self._key(column): self._width(column) for column in self._columns}
        self._apply_saved_layout()

    def set_visible_keys(self, visible_keys: set[str]) -> None:
        self._applying = True
        try:
            for index, key in enumerate(self._keys()):
                if key in self._mandatory_keys:
                    continue
                width = self._header.sectionSize(index)
                if width > 0:
                    self._known_widths[key] = width
                self._table.setColumnHidden(index, key not in visible_keys)
        finally:
            self._applying = False
        self.save()

    def layout_snapshot(self) -> dict[str, Any]:
        self.save()
        return self._read_layout() or self._default_layout()

    def visible_keys_for_layout(self, settings_key: str, *, hide_new_custom_columns=False) -> set[str]:
        """Inspect another sheet without switching tabs or changing its layout."""
        layout, _ = self._read_layout_at(settings_key)
        layout = layout or self._default_layout()
        hidden = set(layout["hidden"]) - self._mandatory_keys
        if "detail_address" not in layout["order"] and "address" in hidden:
            hidden.add("detail_address")
        if hide_new_custom_columns:
            hidden.update(key for key in self._keys()
                          if key.startswith("custom:") and key not in layout["order"])
        return set(self._keys()) - hidden

    def set_hide_new_custom_columns(self, hidden: bool) -> None:
        self._hide_new_custom_columns = hidden

    def reset(self) -> None:
        self._settings.remove(self._settings_key)
        self._apply_layout(self._default_layout())
        self.save()

    def save(self) -> None:
        """Capture the visible table state when its model still matches these columns."""
        if self._applying or self._model_reset or not self._header_matches_columns():
            return
        keys = self._keys()
        order = [keys[self._header.logicalIndex(visual)] for visual in range(len(keys))]
        hidden = [key for index, key in enumerate(keys) if self._table.isColumnHidden(index)]
        hidden = [key for key in hidden if key not in self._mandatory_keys]
        widths = {
            key: self._header.sectionSize(index)
            if self._header.sectionSize(index) > 0
            else self._known_widths.get(key, self._width(self._columns[index]))
            for index, key in enumerate(keys)
        }
        previous, _ = self._read_layout_at(self._settings_key)
        if previous is not None:
            for key in self._preserve_temporary_keys - set(keys):
                if key in previous["order"]:
                    position = previous["order"].index(key)
                    order.insert(min(position, len(order)), key)
                if key in previous["hidden"]:
                    hidden.append(key)
                if key in previous["widths"]:
                    widths[key] = previous["widths"][key]
        self._known_widths.update(widths)
        self._write_layout({"order": order, "hidden": hidden, "widths": widths,
                            **({"alignments": dict(self._alignments)} if self._alignments else {})})

    def set_alignment(self, key, mask, flag):
        model = self._table.model()
        current = int(model.column_alignment(key))
        self._alignments[key] = (current & ~int(mask)) | int(flag)
        model.set_column_alignments(self._alignments)
        self._table.viewport().update()
        self.save()

    def _connect_model(self, model: object | None) -> None:
        if model is None:
            return
        model.modelAboutToBeReset.connect(self._on_model_about_to_reset)
        model.modelReset.connect(self._on_model_reset)

    def _on_model_about_to_reset(self) -> None:
        self.save()
        self._model_reset = True

    def _on_model_reset(self) -> None:
        # The caller supplies the new stable column descriptions with replace_columns().
        # Header reset signals before that point must not overwrite the old layout.
        self._model_reset = True
        model = self._table.model()
        columns = getattr(model, "columns", None)
        if columns is not None:
            self.replace_columns(columns)

    def _save_from_header(self, *_: object) -> None:
        self.save()

    def _set_hidden(self, key: str, hidden: bool) -> None:
        if key in self._mandatory_keys:
            return
        index = self._index_for_key(key)
        if index is None:
            return
        size = self._header.sectionSize(index)
        if size > 0:
            self._known_widths[key] = size
        self._applying = True
        try:
            self._table.setColumnHidden(index, hidden)
        finally:
            self._applying = False
        self.save()

    def _is_hidden(self, key: str) -> bool:
        index = self._index_for_key(key)
        return False if index is None else self._table.isColumnHidden(index)

    def _apply_saved_layout(self) -> None:
        layout = self._read_layout() or self._default_layout()
        self._apply_layout(layout)

    def _apply_layout(self, layout: dict[str, Any]) -> None:
        if not self._header_matches_columns():
            return
        keys = self._keys()
        saved_order = [key for key in layout["order"] if key in keys]
        order = saved_order + [key for key in keys if key not in saved_order]
        if "detail_address" in keys and "detail_address" not in saved_order and "address" in order:
            order.remove("detail_address")
            order.insert(order.index("address") + 1, "detail_address")
        if "delivery_settings" in keys and "delivery_settings" not in saved_order and "name" in order:
            order.remove("delivery_settings")
            order.insert(order.index("name") + 1, "delivery_settings")
        hidden = set(layout["hidden"]) - self._mandatory_keys
        if "detail_address" not in saved_order and "address" in hidden:
            hidden.add("detail_address")
        if self._hide_new_custom_columns:
            hidden.update(key for key in keys if key.startswith("custom:") and key not in saved_order)
        widths = layout["widths"]
        self._alignments = dict(layout.get("alignments", {}))
        setter = getattr(self._table.model(), "set_column_alignments", None)
        if callable(setter):
            setter(self._alignments)
        self._applying = True
        try:
            for visual, key in enumerate(order):
                logical = self._index_for_key(key)
                if logical is not None:
                    current = self._header.visualIndex(logical)
                    if current != visual:
                        self._header.moveSection(current, visual)
            for index, key in enumerate(keys):
                width = int(widths.get(key, self._width(self._columns[index])))
                self._known_widths[key] = width
                self._header.resizeSection(index, width)
            for index, key in enumerate(keys):
                self._table.setColumnHidden(index, key in hidden)
        finally:
            self._applying = False

    def _default_layout(self) -> dict[str, Any]:
        return {
            "order": self._keys(),
            "hidden": [],
            "widths": {self._key(column): self._width(column) for column in self._columns},
        }

    def _read_layout(self) -> dict[str, Any] | None:
        layout, exists = self._read_layout_at(self._settings_key)
        if exists:
            return layout
        if not self._migrate_legacy:
            return None
        if self._settings_key != self.SETTINGS_KEY:
            current, current_exists = self._read_layout_at(self.SETTINGS_KEY)
            if current_exists and current is not None:
                self._write_layout(current)
                return current
        legacy, _ = self._read_layout_at(self.LEGACY_SETTINGS_KEY)
        if legacy is None:
            return None
        migrated = self._migrate_legacy_layout(legacy)
        self._write_layout(migrated)
        return migrated

    def _read_layout_at(self, key: str) -> tuple[dict[str, Any] | None, bool]:
        if not self._settings.contains(key):
            return None, False
        raw = self._settings.value(key)
        if not isinstance(raw, str):
            return None, True
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            return None, True
        if not isinstance(value, dict):
            return None, True
        order, hidden, widths = value.get("order"), value.get("hidden"), value.get("widths")
        if (
            not isinstance(order, list)
            or not all(isinstance(key, str) for key in order)
            or len(set(order)) != len(order)
            or not isinstance(hidden, list)
            or not all(isinstance(key, str) for key in hidden)
            or not isinstance(widths, dict)
            or not all(isinstance(key, str) and isinstance(width, int) and width > 0 for key, width in widths.items())
        ):
            return None, True
        keys = set(self._keys())
        raw_alignments = value.get("alignments", {})
        alignments = ({key: alignment for key, alignment in raw_alignments.items()
                       if (key in keys or key in self._preserve_temporary_keys)
                       and type(alignment) is int and alignment in {33, 65, 129, 34, 66, 130, 36, 68, 132}}
                      if isinstance(raw_alignments, dict) else {})
        return (
            {
                **({"alignments": alignments} if alignments else {}),
                "order": [item for item in order if item in keys or item in self._preserve_temporary_keys],
                "hidden": [item for item in hidden if item in keys or item in self._preserve_temporary_keys],
                "widths": {item: width for item, width in widths.items() if item in keys or item in self._preserve_temporary_keys},
            },
            True,
        )

    def _migrate_legacy_layout(self, layout: dict[str, Any]) -> dict[str, Any]:
        order = list(layout["order"])
        meal_positions = [index for index, key in enumerate(order) if key in self._OLD_MEAL_ORDER]
        if [order[index] for index in meal_positions] == list(self._OLD_MEAL_ORDER):
            for index, replacement in zip(meal_positions, self._NEW_MEAL_ORDER):
                order[index] = replacement
        return {**layout, "order": order}

    def _write_layout(self, layout: dict[str, Any]) -> None:
        payload = json.dumps(layout, ensure_ascii=False)
        if self._settings.value(self._settings_key) == payload:
            return
        self._settings.setValue(self._settings_key, payload)
        self._settings.sync()

    def _header_matches_columns(self) -> bool:
        model = self._table.model()
        return model is not None and model.columnCount() == len(self._columns)

    def _keys(self) -> list[str]:
        return [self._key(column) for column in self._columns]

    def _index_for_key(self, key: str) -> int | None:
        try:
            return self._keys().index(key)
        except ValueError:
            return None

    def _column_for_key(self, key: str | None) -> object | None:
        if key is None:
            return None
        return next((column for column in self._columns if self._key(column) == key), None)

    @staticmethod
    def _key(column: object) -> str:
        return str(getattr(column, "key"))

    @staticmethod
    def _label(column: object) -> str:
        return str(getattr(column, "label"))

    @staticmethod
    def _width(column: object) -> int:
        return int(getattr(column, "width"))
