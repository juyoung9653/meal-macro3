"""Visible-across-sheets selection, local preferences and direct fallback output."""

from dataclasses import replace
from io import BytesIO

import msoffcrypto
import openpyxl
import pytest
from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QDialog

from src.desktop_v2.delivery_substitute import DeliverySubstituteDialog
from src.desktop_v2.substitute_preferences import SubstituteColumnVisibilityDialog, preferences, exclude_hidden_columns
from src.domain.delivery_substitute import SubstituteColumn, DELIVERY_SOURCES
from src.domain.projections import roster_entity_states
from src.domain.revisions import HistoricalSnapshot
from src.domain.roster import CustomFieldType, Meal
from src.services.sqlite_delivery_substitute_service import prepare_delivery_substitute_data
from src.services.sqlite_delivery_substitute_service import substitute_columns_from_roster
from src.generators.substitute_generator import _build_sheet
from src.excel.publication import publish_workbook
from src.desktop_v2.document_generation import DocumentGenerationController
from tests.test_delivery_substitute_v2 import app, session, source, selection_for, DAY, CUSTOM, ACTOR, Messages
from tests.test_substitute_column_mappings import mapped


def make_dialog(session, tmp_path):
    selection = selection_for(session, tmp_path)
    data = prepare_delivery_substitute_data(session.database.snapshot(selection.document.period.source.revision_id),
                                            DAY, Meal.BREAKFAST, fallbacks={})
    return DeliverySubstituteDialog(selection.document, data), data


def test_visibility_setting_defaults_on_and_saves_only_on_accept(app, tmp_path):
    store = QSettings(str(tmp_path / "visibility.ini"), QSettings.Format.IniFormat)
    dialog = SubstituteColumnVisibilityDialog("db-a", settings=store)
    assert dialog.exclude.isChecked()
    dialog.exclude.setChecked(False)
    dialog.reject()
    _, key = preferences("db-a", store)
    assert exclude_hidden_columns(store, key)
    dialog = SubstituteColumnVisibilityDialog("db-a", settings=store)
    dialog.exclude.setChecked(False)
    dialog.accept()
    assert not SubstituteColumnVisibilityDialog("db-a", settings=store).exclude.isChecked()
    assert SubstituteColumnVisibilityDialog("db-b", settings=store).exclude.isChecked()


def test_hidden_rows_not_printed_and_fallbacks_remembered_without_losing_hidden_selection(app, session, tmp_path):
    store = QSettings(str(tmp_path / "selection.ini"), QSettings.Format.IniFormat)
    dialog, data = make_dialog(session, tmp_path)
    dialog.set_preferences_scope("db-a", settings=store)
    combo = dialog._fallback_combos["road_address"]
    combo.setCurrentIndex(combo.findData("detail_address"))
    visible = {column.key for column in data.columns} - {"detail_address", "mobile_phone"}
    dialog.set_visible_columns(visible)
    assert "mobile_phone" not in dialog.options().columns
    assert "detail_address" not in dialog.options().columns
    assert dialog.options().fallbacks["road_address"] == "detail_address"
    assert "detail_address" not in [dialog._fallback_combos["name"].itemData(i)
                                   for i in range(dialog._fallback_combos["name"].count())]
    dialog.accept()
    restored, _ = make_dialog(session, tmp_path)
    restored.set_preferences_scope("db-a", settings=store)
    assert "mobile_phone" in restored.options().columns
    assert restored.options().fallbacks["road_address"] == "detail_address"
    restored._fallback_combos["road_address"].setCurrentIndex(0)
    restored.reject()
    again, _ = make_dialog(session, tmp_path)
    again.set_preferences_scope("db-a", settings=store)
    assert again.options().fallbacks["road_address"] == "detail_address"


def test_no_visible_columns_blocks_output_and_moving_skips_hidden(app, session, tmp_path):
    dialog, data = make_dialog(session, tmp_path)
    dialog.set_visible_columns({"sequence", "name"})
    dialog.columns.setCurrentItem(dialog.columns.topLevelItem(0))
    dialog._move(1)
    assert dialog.options().columns == ("name", "sequence")
    dialog.set_visible_columns(set())
    assert not dialog.accept_button.isEnabled()
    assert "칼럼" in dialog.error.text()
    dialog.reject()


@pytest.mark.parametrize("value,note,expected", [
    ("사용자 비고", "표준 비고", "표준 비고"),
    (None, "표준 비고", "표준 비고"),
    ("사용자 비고", None, "사용자 비고"),
    ("사용자 비고", " \n ", "사용자 비고"),
    (None, "", ""),
    (0, "", "0"),
    (0, "표준 비고", "표준 비고"),
])
def test_side_selection_has_priority_and_uses_raw_source(value, note, expected):
    snapshot, roster = source(sizes=(1,))
    roster = replace(roster, persons=tuple(replace(person, profile=replace(person.profile,
        note=note, custom_values={CUSTOM: value})) for person in roster.persons),
        custom_fields=tuple(replace(field, field_type=CustomFieldType.NUMBER if type(value) is int else CustomFieldType.TEXT)
                            for field in roster.custom_fields))
    snapshot = HistoricalSnapshot(snapshot.revision_id, roster_entity_states(roster))
    custom_key = f"custom:{CUSTOM}"
    columns = (SubstituteColumn(custom_key, "비고2"), SubstituteColumn("note", "비고"),
               SubstituteColumn("road_address", "내 주소"))
    data = prepare_delivery_substitute_data(snapshot, DAY, Meal.BREAKFAST, columns=columns,
                                            fallbacks={custom_key: "note", "note": "road_address"})
    assert data.rows[0].values[custom_key] == expected
    assert data.rows[0].values["note"] == "테스트로 1"
    assert data.columns == columns + DELIVERY_SOURCES



@pytest.mark.parametrize("side_selection,expected", [(True, "0"), (False, "표준 비고")])
def test_numeric_zero_priority_and_no_side_selection(side_selection, expected):
    snapshot, roster = source(sizes=(1,))
    roster = replace(roster,
        persons=tuple(replace(person, profile=replace(person.profile,
            custom_values={CUSTOM: 0})) for person in roster.persons),
        custom_fields=tuple(replace(field, field_type=CustomFieldType.NUMBER)
                            for field in roster.custom_fields))
    snapshot = HistoricalSnapshot(snapshot.revision_id, roster_entity_states(roster))
    data = prepare_delivery_substitute_data(snapshot, DAY, Meal.BREAKFAST,
        fallbacks={"note": f"custom:{CUSTOM}"} if side_selection else {})
    assert data.rows[0].values["note"] == expected


def test_historical_source_omits_columns_created_later():
    snapshot, _ = source(sizes=(1,))
    columns = (SubstituteColumn("address", "새 주소 이름"),
               SubstituteColumn(f"custom:{CUSTOM}", "비고2"),
               SubstituteColumn("custom:newer-field", "나중에 만든 칼럼"))
    assert substitute_columns_from_roster(columns, source_snapshot=snapshot) == (
        SubstituteColumn("road_address", "새 주소 이름"),
        SubstituteColumn(f"custom:{CUSTOM}", "비고2"),
    )


def test_new_selection_ignores_old_primary_mapping_and_generates_named_fallback_column(app, session, tmp_path):
    selection = selection_for(session, tmp_path)
    snapshot = session.database.snapshot(selection.document.period.source.revision_id)
    _, revision = mapped(snapshot, session.database.current_roster(), {"조식:mobile_phone": "성명"})
    session.append_revision(revision.record)
    options = replace(selection.document.substitute_options, columns=("mobile_phone",),
                      fallbacks={"mobile_phone": "detail_address"},
                      column_labels={"mobile_phone": "연락처", "detail_address": "상세주소"})
    selection = replace(selection, document=replace(selection.document, substitute_options=options))
    def writer(data, opts, path, **kwargs):
        workbook, _, _ = _build_sheet(data, opts)
        return publish_workbook(workbook, path, password=kwargs["output_password"], overwrite=False)
    messages = Messages()
    result = DocumentGenerationController(session, message_box=messages,
        actor_provider=lambda: ACTOR, substitute_writer=writer).run(selection)
    assert result.completed, messages.errors
    run = session.database.document_runs()[0]
    assert run.template_manifest["column_mapping_setting"] is None
    assert run.template_manifest["column_fallbacks"] == {"mobile_phone": "detail_address"}
    with selection.output_path.open("rb") as stream:
        encrypted = msoffcrypto.OfficeFile(stream)
        encrypted.load_key(password="sheet-password")
        content = BytesIO()
        encrypted.decrypt(content)
    content.seek(0)
    book = openpyxl.load_workbook(content)
    assert book.active["A2"].value == "연락처"
    assert book.active["A3"].value == "001호"
    book.close()
