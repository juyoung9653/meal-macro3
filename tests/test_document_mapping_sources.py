"""Mapping choices use roster labels while output retains stable field identities."""

import os
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import openpyxl
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.core.invoice_data import MealPrices, MonthlyDelivery, MonthlyDeliveryBatch, WeeklyDelivery
from src.core.models import FULL_ADDRESS_FIELD
from src.core.settings import get_settings
from src.domain.document_column_mappings import MAPPING_ENTITY_KIND, mapping_entity_id
from src.domain.revisions import EntityRef, HistoricalSnapshot, RevisionActor
from src.domain.roster import CustomFieldDefinition, CustomFieldType, Person, PersonProfile, RosterSnapshot
from src.generators.confirm_generator import make_confirm_publication
from src.generators.invoice_generator import make_invoice_publication
from src.services.document_column_mappings import (
    build_mapping_revision, confirmation_default_mapping, effective_mapping,
    invoice_default_mapping, mapping_editor_spec,
)
from src.services.sqlite_document_service import _roster_entry
from src.templates.config import get_template_sheet_name, read_template_columns
from src.templates.template_scanner import scan_placeholders


def _roster():
    fields = tuple(
        CustomFieldDefinition(uuid4(), name, CustomFieldType.TEXT, index)
        for index, name in enumerate(("성명", "핸드폰2", "전화번호2"))
    )
    person = Person(uuid4(), PersonProfile(
        "기본 이름", school_name="가상 학교", mobile_phone="010-1111-1111",
        household_head_name="가상 보호자", jurisdiction="가상동",
        road_address="가상로 1", detail_address="101호",
        custom_values={fields[0].definition_id: "추가 이름", fields[1].definition_id: "010-2222-2222"},
    ))
    return RosterSnapshot(uuid4(), persons=(person,), custom_fields=fields)


@pytest.mark.parametrize("kind", ["invoice", "confirmation"])
def test_source_labels_match_roster_and_keep_field_ids(kind):
    roster = _roster()
    spec = mapping_editor_spec("급식", kind, roster=roster)
    assert spec.source_labels["학교명"] == "학교"
    assert spec.source_labels["핸드폰"] == "핸드폰1"
    assert spec.source_labels["전화번호"] == "전화번호1"
    assert spec.source_labels[FULL_ADDRESS_FIELD] == "전체 주소 (도로명주소 + 상세주소)"
    assert spec.source_labels["성명"] == "성명 (기본 항목 · 성명)"
    custom_key = f"custom:{roster.custom_fields[0].definition_id}"
    assert spec.source_labels[custom_key] == "성명 (추가 항목)"
    assert not {"이름", "구분", "동", "주소", "세대주성명", "아동과의관계"} & set(spec.source_options)
    renamed = mapping_editor_spec("급식", kind, roster=roster, column_labels={"school": "학교 이름", "mobile": "연락처", "name": "대상자"})
    assert renamed.source_options == spec.source_options
    assert renamed.source_labels["학교명"] == "학교 이름"
    assert renamed.source_labels["핸드폰"] == "연락처"
    assert renamed.source_labels["성명"] == "대상자"


@pytest.mark.parametrize("source", ["이름", "도로명주소", "상세주소", FULL_ADDRESS_FIELD])
def test_saved_legacy_source_is_preserved_without_new_revision(source):
    revision_id = uuid4()
    state = {
        "policy_version": 1, "version": 1, "profile": "급식", "document_kind": "confirmation",
        "overrides": {"성명": source}, "changed_in_revision_id": str(revision_id),
    }
    snapshot = HistoricalSnapshot(revision_id, {
        EntityRef(MAPPING_ENTITY_KIND, mapping_entity_id("급식", "confirmation")): state,
    })
    result = build_mapping_revision(
        revision_id, snapshot, "급식", "confirmation",
        {"성명": source, "세대주 성명": "세대주 성명", "핸드폰": "핸드폰"},
        actor=RevisionActor("tester", "synthetic"), recorded_at=datetime.now(UTC), revision_id=uuid4(),
    )
    assert result is None


@pytest.mark.parametrize("profile", ["급식", "행복도시락"])
@pytest.mark.parametrize("kind", ["invoice", "confirmation"])
def test_new_choices_only_include_roster_columns_and_clear_missing_binding(profile, kind):
    from PySide6.QtWidgets import QApplication
    from src.desktop_v2.document_column_mapping import DocumentColumnMappingDialog
    from src.desktop_v2.roster_window import RosterTableModel

    app = QApplication.instance() or QApplication([])
    roster = _roster()
    model = RosterTableModel(roster)
    column_labels = {column.key: column.label for column in model.columns}
    scopes = tuple((p, k) for p in ("급식", "행복도시락") for k in ("invoice", "confirmation"))
    specs = {scope: mapping_editor_spec(*scope, roster=roster, column_labels=column_labels) for scope in scopes}
    overrides = {scope: {"성명": "반"} for scope in scopes}
    view = DocumentColumnMappingDialog(specs, overrides)
    view._profile_combo.setCurrentIndex(view._profile_combo.findData(profile))
    view._document_combo.setCurrentIndex(view._document_combo.findData(kind))
    expected = {"", "성명", "생년월일", "학교명", "학년", "전화번호", "핸드폰", "관할동", "도로명주소", "상세주소", "비고", "세대주 성명"}
    expected.update(f"custom:{field.definition_id}" for field in roster.custom_fields)
    if kind == "invoice":
        from src.domain.document_column_sources import INVOICE_SOURCE_LABELS
        expected.update(INVOICE_SOURCE_LABELS)
    try:
        assert set(specs[(profile, kind)].selectable_source_options) == expected
        for combo in (*view._combos.values(), *view._fallback_combos.values()):
            enabled = {combo.itemData(i) for i in range(combo.count()) if combo.model().item(i).isEnabled()}
            assert enabled == expected
        combo = view._combos["성명"]
        assert combo.currentData() == ""
        assert combo.findData("반") == -1
        assert all("기존 연결" not in combo.itemText(i) for i in range(combo.count()))
        assert not view._dirty()
        saved_id = uuid4()
        old_setting = {
            "policy_version": 1, "version": 1, "profile": profile, "document_kind": kind,
            "overrides": {"성명": "반"}, "changed_in_revision_id": str(saved_id),
        }
        saved = HistoricalSnapshot(saved_id, {EntityRef(MAPPING_ENTITY_KIND, mapping_entity_id(profile, kind)): old_setting})
        cleared = build_mapping_revision(
            saved_id, saved, profile, kind, view.desired_mapping(), roster=roster,
            actor=RevisionActor("tester", "synthetic"), recorded_at=datetime.now(UTC), revision_id=uuid4(),
        )
        assert cleared.setting.overrides["성명"] == ""
        # The stored household-head field is now a registered roster column.
        head_target = next(target for target in view._combos if "세대주" in target)
        head = view._combos[head_target]
        head.setCurrentIndex(head.findData("학교명"))
        view._reset_scope()
        assert head.currentData() == "세대주 성명"
        assert combo.findData(FULL_ADDRESS_FIELD) == -1
        combo.setCurrentIndex(combo.findData("도로명주소"))
        desired = view.desired_mapping()
        revision_id = uuid4()
        changed = build_mapping_revision(
            revision_id, HistoricalSnapshot(revision_id, {}), profile, kind, desired,
            roster=roster, actor=RevisionActor("tester", "synthetic"),
            recorded_at=datetime.now(UTC), revision_id=uuid4(),
        )
        assert changed.setting.overrides["성명"] == "도로명주소"
    finally:
        view.close()


@pytest.mark.parametrize("profile", ["급식", "행복도시락"])
@pytest.mark.parametrize("kind", ["invoice", "confirmation"])
@pytest.mark.parametrize("use_custom", [False, True])
def test_selected_identity_reaches_actual_output_cells(tmp_path, profile, kind, use_custom):
    roster = _roster()
    source_key = f"custom:{roster.custom_fields[0].definition_id}" if use_custom else "성명"
    source = _roster_entry(roster.persons[0], roster, 2)
    assert source.extra["성명"] == "기본 이름"
    assert source.extra[f"custom:{roster.custom_fields[0].definition_id}"] == "추가 이름"
    delivery = MonthlyDelivery(
        source, "", "가상 기사", "001", 0, 0, 1,
        (WeeklyDelivery(1, date(2026, 6, 1), date(2026, 6, 6), 0, 0, 1),),
        billing_amount=10000, household_key="synthetic-household",
    )
    batch = MonthlyDeliveryBatch(2026, 6, MealPrices(10000, 10000, 10000), False, (delivery,))
    settings = get_settings()
    output = tmp_path / "mapped.xlsx"
    if kind == "invoice":
        template = Path(settings.get_invoice_template_path(profile))
        mapping = effective_mapping(invoice_default_mapping(template, profile, False), {"성명": source_key})
        make_invoice_publication(batch, mapping, str(output), str(template), meal_type=profile)
        sheet = get_template_sheet_name(False, profile)
        row, column = 4, read_template_columns(str(template), sheet_name=sheet).index("성명") + 1
        if profile == "행복도시락":
            sheet = "총괄"
    else:
        template = settings.get_confirm_template_path(profile)
        mapping = effective_mapping(confirmation_default_mapping(), {"성명": source_key})
        make_confirm_publication(batch, mapping, output, template, meal_type=profile)
        sheet = "가상동"
        row, column = scan_placeholders(template)["1명"]["child1"][0]
    workbook = openpyxl.load_workbook(output)
    try:
        assert workbook[sheet].cell(row, column).value == ("추가 이름" if use_custom else "기본 이름")
    finally:
        workbook.close()


def test_mapping_controller_uses_current_roster_column_labels(monkeypatch):
    from PySide6.QtWidgets import QApplication, QDialog, QWidget
    from src.desktop_v2.document_column_mapping import DocumentColumnMappingController, DocumentColumnMappingDialog
    from src.desktop_v2.roster_window import RosterTableModel

    app = QApplication.instance() or QApplication([])
    roster = _roster()
    parent = QWidget()
    parent.table_model = RosterTableModel(roster, column_aliases={"school": "학교 이름", "mobile": "연락처"})
    session = SimpleNamespace(database=SimpleNamespace(
        current_roster=lambda: roster,
        snapshot=lambda revision_id: HistoricalSnapshot(revision_id, {}),
    ))
    def inspect(dialog):
        for scope in ((p, k) for p in ("급식", "행복도시락") for k in ("invoice", "confirmation")):
            spec = dialog._specs[scope]
            assert spec.source_labels["학교명"] == "학교 이름"
            assert spec.source_labels["핸드폰"] == "연락처"
        dialog.show()
        app.processEvents()
        combo = dialog._combos["성명"]
        assert combo.itemText(combo.findData("학교명")) == "학교 이름"
        assert combo.findData("이름") == -1
        dialog.close()
        return QDialog.DialogCode.Rejected

    monkeypatch.setattr(DocumentColumnMappingDialog, "exec", inspect)
    try:
        outcome = DocumentColumnMappingController(session).run(parent)
        assert not outcome.completed
    finally:
        parent.close()


@pytest.mark.parametrize("profile,kind", tuple((p, k) for p in ("급식", "행복도시락") for k in ("invoice", "confirmation")))
def test_synthetic_address_is_unselectable_but_same_named_real_column_is_available(profile, kind):
    from dataclasses import replace
    from PySide6.QtWidgets import QApplication
    from src.desktop_v2.document_column_mapping import DocumentColumnMappingDialog
    from src.desktop_v2.roster_window import RosterTableModel
    app = QApplication.instance() or QApplication([])
    roster = _roster()
    field = CustomFieldDefinition(uuid4(), FULL_ADDRESS_FIELD, CustomFieldType.TEXT, 3)
    roster = replace(roster, custom_fields=(*roster.custom_fields, field))
    labels = {column.key: column.label for column in RosterTableModel(roster).columns}
    scopes = tuple((p, k) for p in ("급식", "행복도시락") for k in ("invoice", "confirmation"))
    specs = {scope: mapping_editor_spec(*scope, roster=roster, column_labels=labels) for scope in scopes}
    overrides = {scope: {"성명": FULL_ADDRESS_FIELD} for scope in scopes}
    fallbacks = {scope: {"성명": FULL_ADDRESS_FIELD} for scope in scopes}
    view = DocumentColumnMappingDialog(specs, overrides, fallbacks=fallbacks)
    view._profile_combo.setCurrentIndex(view._profile_combo.findData(profile))
    view._document_combo.setCurrentIndex(view._document_combo.findData(kind))
    try:
        for combo in (*view._combos.values(), *view._fallback_combos.values()):
            assert combo.findData(FULL_ADDRESS_FIELD) == -1
            assert combo.findData("도로명주소") >= 0 and combo.findData("상세주소") >= 0
            assert combo.findData(f"custom:{field.definition_id}") >= 0
        assert view.desired_mapping()["성명"] == ""
        assert "성명" not in view.desired_fallbacks()
        assert not view._dirty()
        revision_id = uuid4()
        state = {"policy_version": 2, "version": 1, "profile": profile, "document_kind": kind,
                 "overrides": overrides[(profile,kind)], "fallbacks": fallbacks[(profile,kind)],
                 "changed_in_revision_id": str(revision_id)}
        snapshot = HistoricalSnapshot(revision_id, {EntityRef(MAPPING_ENTITY_KIND, mapping_entity_id(profile,kind)): state})
        changed = build_mapping_revision(revision_id, snapshot, profile, kind, view.desired_mapping(),
            desired_fallbacks=view.desired_fallbacks(), roster=roster, actor=RevisionActor("tester","synthetic"),
            recorded_at=datetime.now(UTC), revision_id=uuid4())
        assert changed.setting.overrides["성명"] == ""
        assert not changed.setting.fallbacks
        view.reject()
        assert state["overrides"]["성명"] == FULL_ADDRESS_FIELD
        assert state["fallbacks"]["성명"] == FULL_ADDRESS_FIELD
    finally:
        view.close()
