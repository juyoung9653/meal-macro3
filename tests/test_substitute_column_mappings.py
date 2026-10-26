"""Meal-specific output mappings preserve delivery facts and source values."""
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from io import BytesIO
from uuid import uuid4

import msoffcrypto
import openpyxl
import pytest

from src.desktop_v2.document_column_mapping import DocumentColumnMappingDialog, _SCOPES
from src.desktop_v2.document_generation import DocumentGenerationController
from src.domain.delivery_substitute import STANDARD_COLUMNS
from src.domain.document_column_mappings import mapping_setting_from_snapshot
from src.domain.projections import roster_entity_states
from src.domain.revisions import EntityRef, HistoricalSnapshot
from src.domain.roster import Meal, CustomFieldType
from src.excel.publication import publish_workbook
from src.generators.substitute_generator import _build_sheet
from src.services.document_column_mappings import build_mapping_revision, mapping_editor_spec
from src.services.sqlite_delivery_substitute_service import prepare_delivery_substitute_data
from tests.test_delivery_substitute_v2 import (
    ACTOR, CUSTOM, DAY, Messages, app, session, options, selection_for, source,
)


def mapped(snapshot, roster, overrides, fallbacks=None, profile="급식"):
    spec = mapping_editor_spec(profile, "substitute", roster=roster)
    revision = build_mapping_revision(
        snapshot.revision_id, snapshot, profile, "substitute", {**spec.defaults, **overrides},
        desired_fallbacks=fallbacks or {}, roster=roster, actor=ACTOR,
        recorded_at=datetime.now(UTC), revision_id=uuid4())
    assert revision is not None
    entities = dict(snapshot.entities)
    for change in revision.record.changes:
        entities[EntityRef(change.entity_kind, change.entity_id)] = change.after
    return HistoricalSnapshot(revision.setting.changed_in_revision_id, entities), revision


@pytest.mark.parametrize("column", [column.key for column in STANDARD_COLUMNS] + [f"custom:{CUSTOM}"])
def test_every_output_can_use_any_source_and_fallback(column):
    snapshot, roster = source(sizes=(1,))
    updated, _ = mapped(snapshot, roster, {f"조식:{column}": "전화번호"}, {f"조식:{column}": "상세주소"})
    data = prepare_delivery_substitute_data(snapshot, DAY, Meal.BREAKFAST, mapping_snapshot=updated)
    assert data.rows[0].values[column] == "001호"
    assert roster.persons[0].profile.custom_values[CUSTOM] == "=문자 그대로"
    assert data.default_columns[-1] == f"custom:{CUSTOM}"


@pytest.mark.parametrize("value,expected", [(None, "표준 비고"), (" \n ", "표준 비고"), (0, "0")])
def test_only_missing_values_use_second_priority(value, expected):
    snapshot, roster = source(sizes=(1,))
    roster = replace(roster, persons=tuple(replace(person, profile=replace(person.profile,
        custom_values={CUSTOM: value})) for person in roster.persons),
        custom_fields=tuple(replace(field, field_type=CustomFieldType.NUMBER if type(value) is int else CustomFieldType.TEXT)
                            for field in roster.custom_fields))
    snapshot = HistoricalSnapshot(snapshot.revision_id, roster_entity_states(roster))
    updated, _ = mapped(snapshot, roster, {"조식:road_address": f"custom:{CUSTOM}"}, {"조식:road_address": "비고"})
    data = prepare_delivery_substitute_data(snapshot, DAY, Meal.BREAKFAST, mapping_snapshot=updated)
    assert data.rows[0].values["road_address"] == expected


@pytest.mark.parametrize("meal", [Meal.BREAKFAST, Meal.DINNER])
def test_meal_and_template_profile_settings_are_independent(meal):
    snapshot, roster = source(sizes=(1,), meal=meal)
    day = DAY if meal is Meal.BREAKFAST else DAY - timedelta(days=1)
    updated, _ = mapped(snapshot, roster, {"조식:road_address": "상세주소", "석식:road_address": "비고"})
    data = prepare_delivery_substitute_data(snapshot, day, meal, mapping_snapshot=updated)
    assert data.rows[0].values["road_address"] == ("001호" if meal is Meal.BREAKFAST else "표준 비고")
    other, _ = mapped(snapshot, roster, {"조식:road_address": "비고"}, profile="행복도시락")
    assert prepare_delivery_substitute_data(snapshot, day, meal, mapping_snapshot=other).rows[0].values["road_address"] == "테스트로 1"


def test_mapping_uses_original_sources_and_keeps_grouping_and_sorting():
    snapshot, roster = source(sizes=(2, 1, 1))
    original = prepare_delivery_substitute_data(snapshot, DAY, Meal.BREAKFAST)
    updated, _ = mapped(snapshot, roster, {
        "조식:road_address": "상세주소", "조식:detail_address": "도로명주소",
        "조식:mobile_phone": "", "조식:jurisdiction": "", "조식:sequence": "성명",
        "조식:name": f"custom:{CUSTOM}", f"조식:custom:{CUSTOM}": "성명",
    })
    data = prepare_delivery_substitute_data(snapshot, DAY, Meal.BREAKFAST, mapping_snapshot=updated)
    assert [row.person_id for row in data.rows] == [row.person_id for row in original.rows]
    assert data.rows[0].values["road_address"] == "001호"
    assert data.rows[0].values["detail_address"] == "테스트로 1"
    assert data.rows[0].values["name"] == "=문자 그대로"
    wb, _, _ = _build_sheet(data, replace(options(data), columns=("name", "mobile_phone", "road_address")))
    assert wb.active["A3"].data_type == "s"
    assert "B3:B5" in {str(item) for item in wb.active.merged_cells.ranges}
    assert wb.active["D3"].value == 3
    assert wb.active["C2"].value == "주소"
    wb.close()


def test_missing_source_definition_fails_for_both_priorities():
    snapshot, roster = source(sizes=(1,))
    without_custom = replace(roster, custom_fields=(), persons=tuple(
        replace(person, profile=replace(person.profile, custom_values={})) for person in roster.persons))
    old = HistoricalSnapshot(uuid4(), roster_entity_states(without_custom))
    for priority in (1, 2):
        updated, _ = mapped(snapshot, roster,
            {"조식:road_address": f"custom:{CUSTOM}" if priority == 1 else "도로명주소"},
            {"조식:road_address": f"custom:{CUSTOM}"} if priority == 2 else {})
        with pytest.raises(ValueError, match="선택한 원본에 없습니다"):
            prepare_delivery_substitute_data(old, DAY, Meal.BREAKFAST, mapping_snapshot=updated)


def test_mapping_settings_only_offer_invoice_and_confirmation(app):
    _, roster = source()
    specs = {scope: mapping_editor_spec(*scope, roster=roster) for scope in _SCOPES}
    dialog = DocumentColumnMappingDialog(specs, {scope: {} for scope in _SCOPES})
    assert dialog._document_combo.findData("substitute") == -1
    assert {dialog._document_combo.itemData(index) for index in range(dialog._document_combo.count())} == {"invoice", "confirmation"}
    dialog.reject()


def test_current_settings_apply_to_historical_source_and_persist_with_output(app, session, tmp_path):
    selection = selection_for(session, tmp_path)
    snapshot = session.database.snapshot(selection.document.period.source.revision_id)
    roster = session.database.current_roster()
    updated, revision = mapped(snapshot, roster, {"조식:road_address": "전화번호",
        f"조식:custom:{CUSTOM}": "상세주소"}, {"조식:road_address": "비고"})
    session.append_revision(revision.record)
    # Exercise the real controller, workbook and encrypted publication without
    # invoking Excel's separate row-height measurement boundary.
    def writer(data, opts, path, **kwargs):
        workbook, _, _ = _build_sheet(data, opts)
        return publish_workbook(workbook, path, password=kwargs["output_password"], overwrite=False)
    messages = Messages()
    outcome = DocumentGenerationController(session, message_box=messages,
        actor_provider=lambda: ACTOR, substitute_writer=writer).run(selection)
    assert outcome.completed, messages.errors
    run = session.database.document_runs()[0]
    assert run.source_revision_id == snapshot.revision_id
    assert run.template_manifest["column_fallbacks"]["road_address"] == "비고"
    assert run.template_manifest["column_mapping_setting"]["setting_revision_id"] == str(updated.revision_id)
    with selection.output_path.open("rb") as stream:
        encrypted = msoffcrypto.OfficeFile(stream)
        encrypted.load_key(password="sheet-password")
        content = BytesIO()
        encrypted.decrypt(content)
    content.seek(0)
    workbook = openpyxl.load_workbook(content)
    assert workbook.active["E3"].value == "표준 비고"
    assert workbook.active["G3"].value == "001호"
    workbook.close()
    content.close()
    current = session.database.snapshot(session.database.current_roster().revision_id)
    setting = mapping_setting_from_snapshot(current, "급식", "substitute")
    assert setting.fallbacks == {"조식:road_address": "비고"}
    spec = mapping_editor_spec("급식", "substitute", roster=roster)
    assert build_mapping_revision(current.revision_id, current, "급식", "substitute",
        {**spec.defaults, **setting.overrides}, desired_fallbacks=setting.fallbacks,
        roster=roster, actor=ACTOR, recorded_at=datetime.now(UTC), revision_id=uuid4()) is None
    session.database.check_integrity()
