"""The stored standard note and a custom second note remain distinct."""

import json
import os
from uuid import uuid4

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from src.desktop_v2.roster_window import RosterTableModel, RosterWindow
from src.domain.roster import CustomFieldDefinition, CustomFieldType, Person, PersonProfile, RosterSnapshot


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _roster():
    field = CustomFieldDefinition(uuid4(), "비고2", CustomFieldType.TEXT, 0)
    person = Person(uuid4(), PersonProfile("예시", note="기본 메모\n둘째 줄",
                                          custom_values={field.definition_id: "추가 메모"}))
    return RosterSnapshot(uuid4(), persons=(person,), custom_fields=(field,))


def test_standard_note_and_custom_note_display_and_filter_independently(app):
    model = RosterTableModel(_roster())
    columns = {column.label: index for index, column in enumerate(model.columns)}
    assert model.data(model.index(0, columns["비고"])) == "기본 메모\n둘째 줄"
    assert model.data(model.index(0, columns["비고2"])) == "추가 메모"
    model.set_search("둘째 줄")
    assert model.rowCount() == 1
    model.set_column_search("note", "없는 내용")
    assert model.rowCount() == 0
    model.set_column_search("note", "기본 메모")
    assert model.rowCount() == 1


def test_standard_note_appears_with_preexisting_column_layout(app, tmp_path):
    settings = QSettings(str(tmp_path / "note.ini"), QSettings.Format.IniFormat)
    settings.setValue("roster/columns_v2", json.dumps({
        "order": ["name", "sequence"], "hidden": [], "widths": {"name": 140},
    }))
    window = RosterWindow(_roster(), column_settings=settings, inactive_settings=settings)
    index = next(i for i, column in enumerate(window.table_model.columns) if column.key == "note")
    assert not window._table.isColumnHidden(index)
    assert window._table.columnWidth(index) >= 160
    window.close()
