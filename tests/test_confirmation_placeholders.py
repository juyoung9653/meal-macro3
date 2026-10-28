from dataclasses import replace
from types import SimpleNamespace
from datetime import UTC, datetime
from uuid import uuid4

import openpyxl
import pytest
from PySide6.QtWidgets import QApplication

from src.templates.confirmation_fields import confirmation_fields, placeholder_mapping
from src.services.document_column_mappings import mapping_editor_spec, build_mapping_revision
from src.desktop_v2.document_column_mapping import DocumentColumnMappingDialog
from src.domain.revisions import HistoricalSnapshot, RevisionActor
from src.generators.confirm_generator import make_confirm_publication
from src.core.invoice_data import MonthlyDeliveryBatch, MealPrices
from tests.test_confirm_generator import _typed_delivery


@pytest.fixture
def template(tmp_path):
    path = tmp_path / "confirmation.xlsx"
    workbook = openpyxl.Workbook()
    workbook.remove(workbook.active)
    for index in range(1, 6):
        sheet = workbook.create_sheet(f"{index}명")
        for cell, token in (("A1", "{title}"), ("A2", "{addr}"), ("A3", "{dong}"), ("A4", "{child1}")):
            sheet[cell] = token
        if index == 1:
            sheet["B1"] = "{extra1}"
            sheet["B2"] = "{extra1}"
            sheet["B3"] = "{unused}"
            sheet["B4"] = "{성명}"
    workbook.save(path)
    workbook.close()
    return path


def settings_for(path):
    return SimpleNamespace(get_confirm_template_path=lambda profile: str(path),
                           get=lambda *args: [f"{i}명" for i in range(1, 6)])


def test_discovery_keeps_partial_placeholders_and_legacy_connections(template):
    spec = mapping_editor_spec("급식", "confirmation", settings=settings_for(template))
    assert "{extra1}" in spec.targets
    assert spec.defaults["{성명}"] == "성명"
    assert spec.missing_sheets["{extra1}"] == ("2명", "3명", "4명", "5명")
    assert "{addr}" not in spec.missing_sheets
    assert placeholder_mapping(spec.defaults, {"성명": "학교명"})["{child1}"] == "학교명"
    assert placeholder_mapping(spec.defaults, {"성명": "학교명", "{child1}": ""})["{child1}"] == ""
    original = uuid4()
    saved = build_mapping_revision(original, HistoricalSnapshot(original, {}), "급식", "confirmation",
        {"{extra1}": "성명"}, desired_fallbacks={"{extra1}": "세대주 성명"},
        settings=settings_for(template), actor=RevisionActor("test", "test"),
        recorded_at=datetime.now(UTC), revision_id=uuid4())
    assert saved.setting.overrides == {"{extra1}": "성명"}
    assert saved.setting.fallbacks == {"{extra1}": "세대주 성명"}


def test_partial_placeholder_is_editable_with_visible_warning(template):
    app = QApplication.instance() or QApplication([])
    spec = mapping_editor_spec("급식", "confirmation", settings=settings_for(template))
    scopes = [(p, d) for p in ("급식", "행복도시락") for d in ("invoice", "confirmation")]
    dialog = DocumentColumnMappingDialog({scope: spec for scope in scopes}, {scope: {} for scope in scopes})
    dialog._document_combo.setCurrentIndex(1)
    assert dialog._target_heading.text() == "플레이스홀더"
    assert "전체 시트에 있지 않음 (1/5)" in dialog._indicators["{extra1}"].text()
    assert "2명" in dialog._indicators["{extra1}"].toolTip()
    combo = dialog._combos["{extra1}"]
    combo.setCurrentIndex(combo.findData("성명"))
    assert dialog.desired_mapping()["{extra1}"] == "성명"
    dialog.reject()
    app.processEvents()


@pytest.mark.parametrize("primary", ["", " ", 0, "=literal"])
def test_custom_token_renders_fallback_duplicates_and_missing_sheets(template, tmp_path, primary):
    person = _typed_delivery("합성 이름", "household", "합성 주소")
    person = replace(person, roster=replace(person.roster, extra=dict(person.roster.extra, **{"custom:field": primary})))
    batch = MonthlyDeliveryBatch(2026, 6, MealPrices(10000, 10000, 10000), False, (person,))
    output = tmp_path / "output.xlsx"
    created, _ = make_confirm_publication(batch, {"{extra1}": "custom:field", "{child1}": "세대주 성명"},
        output, str(template), column_fallbacks={"{extra1}": "성명"})
    workbook = openpyxl.load_workbook(output)
    try:
        sheet = workbook[created[0]["sheet_name"]]
        expected = "합성 이름" if primary in ("", " ") else primary
        assert sheet["B1"].value == sheet["B2"].value == expected
        assert sheet["B1"].data_type != "f"
        assert sheet["A4"].value == "홍보호"
        assert sheet["B4"].value == "합성 이름"
        assert sheet["B3"].value is None
    finally:
        workbook.close()
