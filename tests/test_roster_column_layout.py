from __future__ import annotations

import json
from dataclasses import dataclass

import pytest
from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSettings, Qt
from PySide6.QtWidgets import QApplication, QTableView

from src.desktop_v2.column_layout import RosterColumnLayout


@dataclass(frozen=True)
class Column:
    key: str
    label: str
    width: int


BASE_COLUMNS = (
    Column("name", "이름", 120),
    Column("status:dinner", "저녁", 100),
    Column("status:breakfast", "아침", 80),
    Column("status:lunch", "점심", 90),
    Column("birth_date", "생년월일", 110),
    Column("address", "주소", 140),
)


class DynamicModel(QAbstractTableModel):
    def __init__(self, columns: tuple[Column, ...]) -> None:
        super().__init__()
        self.columns = columns

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 1

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return len(self.columns)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        return None

    def replace(self, columns: tuple[Column, ...]) -> None:
        self.beginResetModel()
        self.columns = columns
        self.endResetModel()


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def settings(tmp_path):
    ini_path = tmp_path / "layout.ini"
    value = QSettings(str(ini_path), QSettings.Format.IniFormat)
    value.clear()
    return value


def make_view(columns=BASE_COLUMNS):
    view = QTableView()
    model = DynamicModel(columns)
    view.setModel(model)
    return view, model


def visual_keys(view, columns):
    header = view.horizontalHeader()
    return [columns[header.logicalIndex(index)].key for index in range(len(columns))]


def test_defaults_and_menu_enforce_mandatory_columns(app, settings):
    view, _ = make_view()
    layout = RosterColumnLayout(view, BASE_COLUMNS, settings=settings)
    menu = layout.create_menu()
    actions = {action.text(): action for action in menu.actions() if action.isCheckable()}

    assert visual_keys(view, BASE_COLUMNS) == [column.key for column in BASE_COLUMNS]
    assert all(view.columnWidth(index) == column.width for index, column in enumerate(BASE_COLUMNS))
    assert actions["이름 (필수)"].isChecked() and not actions["이름 (필수)"].isEnabled()
    assert actions["생년월일 (필수)"].isChecked() and not actions["생년월일 (필수)"].isEnabled()
    actions["주소"].trigger()
    assert view.isColumnHidden(5)
    view.close()


def test_move_resize_hide_and_round_trip(app, settings):
    view, _ = make_view()
    layout = RosterColumnLayout(view, BASE_COLUMNS, settings=settings)
    header = view.horizontalHeader()
    header.moveSection(header.visualIndex(5), 0)
    header.resizeSection(5, 211)
    layout.create_menu().actions()[5].trigger()
    layout.save()
    view.close()

    restored, _ = make_view()
    restored_layout = RosterColumnLayout(restored, BASE_COLUMNS, settings=settings)
    assert visual_keys(restored, BASE_COLUMNS)[0] == "address"
    assert restored.isColumnHidden(5)
    restored_layout._set_hidden("address", False)
    assert restored.columnWidth(5) == 211
    restored.close()


def test_model_reset_preserves_old_layout_and_prunes_removed_columns(app, settings):
    view, model = make_view()
    layout = RosterColumnLayout(view, BASE_COLUMNS, settings=settings)
    header = view.horizontalHeader()
    header.moveSection(header.visualIndex(5), 0)
    layout.save()
    reduced = BASE_COLUMNS[:-1] + (Column("note", "비고", 160),)
    model.replace(reduced)
    layout.replace_columns(reduced)
    stored = json.loads(settings.value(RosterColumnLayout.SETTINGS_KEY))

    assert visual_keys(view, reduced) == ["name", "status:dinner", "status:breakfast", "status:lunch", "birth_date", "note"]
    assert "address" not in stored["order"]
    assert view.columnWidth(5) == 160
    view.close()


def test_same_schema_model_reset_resumes_header_persistence(app, settings):
    view, model = make_view()
    layout = RosterColumnLayout(view, BASE_COLUMNS, settings=settings)
    model.replace(BASE_COLUMNS)
    header = view.horizontalHeader()
    header.moveSection(header.visualIndex(5), 0)
    layout._set_hidden("address", True)
    layout.save()

    stored = json.loads(settings.value(RosterColumnLayout.SETTINGS_KEY))
    assert stored["order"][0] == "address"
    assert stored["hidden"] == ["address"]
    view.close()


def test_corrupt_settings_fall_back_to_defaults(app, settings):
    settings.setValue(RosterColumnLayout.SETTINGS_KEY, "{not-json")
    view, _ = make_view()
    RosterColumnLayout(view, BASE_COLUMNS, settings=settings)

    assert visual_keys(view, BASE_COLUMNS) == [column.key for column in BASE_COLUMNS]
    assert not any(view.isColumnHidden(index) for index in range(len(BASE_COLUMNS)))
    view.close()


def test_reset_restores_defaults_and_persists_them(app, settings):
    view, _ = make_view()
    layout = RosterColumnLayout(view, BASE_COLUMNS, settings=settings)
    view.horizontalHeader().moveSection(5, 0)
    layout._set_hidden("address", True)
    layout.reset()

    assert visual_keys(view, BASE_COLUMNS) == [column.key for column in BASE_COLUMNS]
    assert not view.isColumnHidden(5)
    assert json.loads(settings.value(RosterColumnLayout.SETTINGS_KEY))["hidden"] == []
    view.close()


def test_migrates_original_v1_meal_order_without_moving_non_meal_columns(app, settings):
    v1_order = [
        "address", "status:breakfast", "name", "status:lunch", "status:dinner", "birth_date"
    ]
    settings.setValue(
        RosterColumnLayout.LEGACY_SETTINGS_KEY,
        json.dumps({
            "order": v1_order,
            "hidden": ["address"],
            "widths": {column.key: column.width + 7 for column in BASE_COLUMNS},
        }),
    )
    view, _ = make_view()
    RosterColumnLayout(view, BASE_COLUMNS, settings=settings)
    snapshot = json.loads(settings.value(RosterColumnLayout.SETTINGS_KEY))

    assert visual_keys(view, BASE_COLUMNS) == [
        "address", "status:dinner", "name", "status:breakfast", "status:lunch", "birth_date"
    ]
    assert view.isColumnHidden(5)
    assert snapshot["order"] == [
        "address", "status:dinner", "name", "status:breakfast", "status:lunch", "birth_date"
    ]
    assert snapshot["hidden"] == ["address"]
    assert snapshot["widths"]["address"] == 147
    view.close()


def test_migration_preserves_a_user_custom_meal_order(app, settings):
    custom_order = [
        "name", "status:lunch", "address", "status:breakfast", "status:dinner", "birth_date"
    ]
    settings.setValue(
        RosterColumnLayout.LEGACY_SETTINGS_KEY,
        json.dumps({
            "order": custom_order,
            "hidden": [],
            "widths": {column.key: column.width for column in BASE_COLUMNS},
        }),
    )
    view, _ = make_view()
    RosterColumnLayout(view, BASE_COLUMNS, settings=settings)
    assert visual_keys(view, BASE_COLUMNS) == custom_order
    view.close()


def test_migration_leaves_partial_meal_slots_unchanged(app, settings):
    partial_order = ["name", "status:breakfast", "address", "status:dinner", "birth_date"]
    settings.setValue(
        RosterColumnLayout.LEGACY_SETTINGS_KEY,
        json.dumps({
            "order": partial_order,
            "hidden": [],
            "widths": {column.key: column.width for column in BASE_COLUMNS},
        }),
    )
    view, _ = make_view()
    RosterColumnLayout(view, BASE_COLUMNS, settings=settings)
    snapshot = json.loads(settings.value(RosterColumnLayout.SETTINGS_KEY))

    assert snapshot["order"] == partial_order
    assert visual_keys(view, BASE_COLUMNS)[:5] == partial_order
    view.close()
