"""Shared-column exception-only filtering without extra columns or cell links."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from dataclasses import replace
from uuid import uuid4

import pytest
from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QApplication, QCheckBox

from src.desktop_v2.roster_window import RosterWindow
from src.desktop_v2.column_search import ColumnValuePicker, SequenceValuePicker
from tests.test_household_shared import roster, DAY, CustomFieldDefinition, CustomFieldType


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def make_window(tmp_path, r=None):
    r = r or roster()
    window = RosterWindow(r, column_settings=QSettings(str(tmp_path / "view.ini"), QSettings.Format.IniFormat))
    window._set_day(DAY)
    window.table_model.set_archive_filter(frozenset(), False)
    return window


def picker_for(window, key):
    window._column_search_header.open_value_filter(key)
    return window._column_search_header._menu.findChild(ColumnValuePicker)


def ids(model):
    return {model.person_at(i).person_id for i in range(model.rowCount())}


@pytest.mark.parametrize("key", ["mobile", "address", "detail_address", "sequence", "status:dinner"])
def test_checkbox_only_on_shared_apply_cancel_reset(app, tmp_path, key):
    window = make_window(tmp_path); model = window.table_model
    r = window.roster; first = r.persons[0].person_id
    normal = picker_for(window, key)
    assert normal.findChild(QCheckBox) is None
    normal.cancel.click()
    window.set_household_shared_state(frozenset({key}), frozenset({(first,key)}))
    picker = picker_for(window,key)
    if key == "sequence":
        assert isinstance(picker,SequenceValuePicker)
    checkbox = picker.findChild(QCheckBox)
    assert checkbox.text() == "예외만 보기"
    checkbox.setChecked(True)
    picker.cancel.click()
    assert ids(model) == {p.person_id for p in r.persons}
    picker = picker_for(window,key)
    assert not picker.findChild(QCheckBox).isChecked()
    picker.findChild(QCheckBox).setChecked(True)
    picker.apply.click()
    assert ids(model) == {first}
    assert model.exception_filter_state(key) is True
    assert window._column_search_header.buttons[key].property("filterActive") is True
    assert not window._column_filter_strip.isHidden()
    picker = picker_for(window,key)
    assert picker.findChild(QCheckBox).isChecked()
    if key == "status:dinner":
        assert not hasattr(picker, "reset")
        picker.findChild(QCheckBox).setChecked(False)
        picker.apply.click()
    else:
        picker.reset.click()
    assert ids(model) == {p.person_id for p in r.persons}
    assert model.exception_filter_state(key) is False
    assert not window._column_search_header.buttons[key].property("filterActive")
    window.close()


def test_intersection_with_other_filters_live_exceptions_and_disable(app,tmp_path):
    window = make_window(tmp_path); model = window.table_model
    a,b,c = window.roster.persons
    pairs = frozenset({(a.person_id,"mobile"),(b.person_id,"mobile"),(b.person_id,"note")})
    window.set_household_shared_state(frozenset({"mobile","note"}), pairs)
    window._on_exception_filter_changed("mobile",True)
    assert ids(model) == {a.person_id,b.person_id}
    window._on_column_search_changed("name",b.profile.name)
    assert ids(model) == {b.person_id}
    window._on_column_values_changed("mobile",frozenset({"no match"}))
    assert model.rowCount() == 0
    window._on_column_values_changed("mobile",None)
    window._on_column_search_changed("name","")
    window._on_exception_filter_changed("note",True)
    assert ids(model) == {b.person_id}
    # Removing the only matching exception refreshes without changing filters.
    window.set_household_shared_state(frozenset({"mobile","note"}),frozenset({(a.person_id,"mobile")}))
    assert model.rowCount() == 0
    window.set_household_shared_state(frozenset({"mobile"}),pairs)
    assert model.exception_filter_state("note") is None
    assert "note" not in model._exception_only_columns
    assert ids(model) == {a.person_id,b.person_id}
    window._clear_column_filters()
    assert ids(model) == {a.person_id,b.person_id,c.person_id}
    assert not model._exception_only_columns
    window.close()


def test_saved_filter_reopens_after_shared_state_load_and_export(app,tmp_path):
    r = roster(); first = r.persons[0]
    window = make_window(tmp_path,r)
    pairs = frozenset({(first.person_id,"mobile")})
    window.set_household_shared_state(frozenset({"mobile"}),pairs)
    window._on_exception_filter_changed("mobile",True)
    export = window.current_sheet_snapshot()
    name = export.headers.index("성명")
    assert [row[name] for row in export.rows] == [first.profile.name]
    window.close()
    reopened = make_window(tmp_path,r)
    assert reopened._pending_exception_filters == {"mobile"}
    reopened.set_household_shared_state(frozenset({"mobile"}),pairs)
    assert ids(reopened.table_model) == {first.person_id}
    assert not reopened._pending_exception_filters
    reopened.set_household_shared_state(frozenset(),pairs)
    assert ids(reopened.table_model) == {p.person_id for p in r.persons}
    reopened.close()
    again=make_window(tmp_path,r)
    again.set_household_shared_state(frozenset({"mobile"}),pairs)
    assert not again.table_model._exception_only_columns
    again.close()


def test_custom_column_deletion_and_hidden_delivery_filter_clear(app,tmp_path):
    field = CustomFieldDefinition(uuid4(),"추가",CustomFieldType.TEXT,0)
    r = replace(roster(),custom_fields=(field,))
    key = f"custom:{field.definition_id}"
    window = make_window(tmp_path,r); first=r.persons[0].person_id
    window.set_household_shared_state(frozenset({key,"delivery_settings"}),
        frozenset({(first,key),(first,"delivery_settings")}))
    picker=picker_for(window,key)
    picker.findChild(QCheckBox).setChecked(True); picker.apply.click()
    assert ids(window.table_model)=={first}
    window.replace_roster(replace(r,custom_fields=()))
    assert key not in window.table_model._exception_only_columns
    window.table_model.set_special_only(True)
    window.table_model.set_exception_filter("delivery_settings",True)
    assert "delivery_settings" in window.table_model._exception_only_columns
    window.table_model.set_special_only(False)
    assert "delivery_settings" not in window.table_model._exception_only_columns
    window.close()
