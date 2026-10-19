"""Split addresses retain dated values, layout identity and output contracts."""
import json
from dataclasses import replace

import pytest
from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QApplication, QMenu

from src.core.models import FULL_ADDRESS_FIELD
from src.desktop_v2.delivery_routing import RoutingChangeDialog
from src.desktop_v2.roster_window import RosterWindow
from src.domain.delivery_routing import RoutingState
from src.services.document_column_mappings import mapping_editor_spec
from tests.test_desktop_v2_roster import _roster, SERVICE_DAY


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


def roster():
    original = _roster()
    first = original.persons[0]
    return replace(original, persons=(
        replace(first, profile=replace(first.profile, road_address="가상로 1", detail_address="101호")),
        *original.persons[1:],
    ))


def test_split_filters_and_current_sheet_export(app, tmp_path):
    window = RosterWindow(roster(), column_settings=QSettings(str(tmp_path / "view.ini"), QSettings.IniFormat))
    try:
        window._set_day(SERVICE_DAY)
        window.table_model.set_archive_filter(frozenset(), False)
        window._add_sheet("주소", {"address", "detail_address"})
        snapshot = window.current_sheet_snapshot()
        assert snapshot.headers == ("도로명주소", "상세주소")
        assert ("가상로 1", "101호") in snapshot.rows
        model = window.table_model
        model.set_column_search("address", "101호")
        assert model.rowCount() == 0
        model.set_column_search("address", "")
        model.set_column_values("detail_address", frozenset({"101호"}))
        assert model.rowCount() == 1
        assert window.current_sheet_snapshot().rows == (("가상로 1", "101호"),)
        model.set_column_values("detail_address", None)
        model.set_search("101호")
        assert model.rowCount() == 1
    finally:
        window.close()


@pytest.mark.parametrize("hide_address", [False, True])
def test_old_layout_aliases_and_filters_migrate_once(app, tmp_path, hide_address):
    store = QSettings(str(tmp_path / "legacy.ini"), QSettings.IniFormat)
    window = RosterWindow(roster(), column_settings=store)
    layout_key = window._column_layout._settings_key
    search_key = window._search_settings_key
    window._rename_column("address", "배송지")
    window.close()
    store.setValue(layout_key, json.dumps({
        "order": ["address", "name", "school"],
        "hidden": ["school", "address"] if hide_address else ["school"],
        "widths": {"address": 277},
    }))
    store.setValue(search_key, json.dumps({
        "columns": {"address": "101호", "name": "홍"},
        "values": {"address": ["가상로 1 101호"], "school": ["샘플초"]},
    }))
    restored = RosterWindow(roster(), column_settings=store)
    try:
        columns = {c.key: i for i, c in enumerate(restored.table_model.columns)}
        header = restored._table.horizontalHeader()
        assert header.visualIndex(columns["address"]) == 0
        assert header.visualIndex(columns["detail_address"]) == 1
        assert restored._column_layout._known_widths["address"] == 277
        assert restored.table_model.columns[columns["address"]].label == "배송지"
        assert restored._table.isColumnHidden(columns["school"])
        assert restored._table.isColumnHidden(columns["detail_address"]) is hide_address
        assert restored.table_model.selected_column_search("address") == ""
        assert restored.table_model.selected_column_search("name") == "홍"
        assert "address" not in restored.table_model._column_values
        assert restored.table_model._column_values["school"] == frozenset({"샘플초"})
        restored._on_column_search_changed("address", "가상로")
    finally:
        restored.close()
    reopened = RosterWindow(roster(), column_settings=store)
    try:
        assert reopened.table_model.selected_column_search("address") == "가상로"
    finally:
        reopened.close()


def test_shared_address_columns_toggle_and_mark_independently(app, tmp_path):
    window = RosterWindow(roster(), column_settings=QSettings(str(tmp_path / "shared.ini"), QSettings.IniFormat))
    try:
        window.set_household_shared_columns(frozenset({"address"}))
        requested = []
        window.household_shared_column_requested.connect(requested.append)
        for key in ("address", "detail_address"):
            index, column = next((i, c) for i, c in enumerate(window.table_model.columns) if c.key == key)
            assert (window.table_model.headerData(index, Qt.Horizontal, Qt.DecorationRole) is not None) == (key == "address")
            menu = QMenu()
            window._add_household_shared_column_actions(menu, column)
            action = menu.actions()[0]
            assert action.text() == "가구 공용 설정"
            assert action.isChecked() == (key == "address")
            action.trigger()
        assert requested == ["address", "detail_address"]
        window.set_household_shared_columns(frozenset({"detail_address"}))
        road, detail = (
            next(i for i, column in enumerate(window.table_model.columns) if column.key == key)
            for key in ("address", "detail_address")
        )
        assert window.table_model.headerData(road, Qt.Horizontal, Qt.DecorationRole) is None
        assert window.table_model.headerData(detail, Qt.Horizontal, Qt.DecorationRole) is not None
    finally:
        window.close()


@pytest.mark.parametrize("component,value", [("road", "다른로 2"), ("detail", "202호")])
def test_relocation_preserves_unedited_address_component(app, component, value):
    data = roster()
    dialog = RoutingChangeDialog(data, RoutingState(), data.persons[0].person_id, SERVICE_DAY)
    try:
        assert dialog.road.text() == "가상로 1"
        assert dialog.detail.text() == "101호"
        getattr(dialog, component).setText(value)
        dialog._save()
        request = dialog.request()
        assert request.road_address == (value if component == "road" else "가상로 1")
        assert request.detail_address == (value if component == "detail" else "101호")
    finally:
        dialog.close()


def test_document_source_aliases_do_not_relabel_full_address():
    spec = mapping_editor_spec("급식", "confirmation", column_labels={
        "address": "배송 도로", "detail_address": "동호수",
    })
    assert spec.source_labels["도로명주소"] == "배송 도로"
    assert spec.source_labels["상세주소"] == "동호수"
    assert spec.source_labels[FULL_ADDRESS_FIELD] == "전체 주소 (도로명주소 + 상세주소)"
    assert {"도로명주소", "상세주소", FULL_ADDRESS_FIELD} <= set(spec.selectable_source_options)
