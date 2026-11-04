from dataclasses import replace
from copy import copy
from datetime import date
from pathlib import Path
from uuid import uuid4

import pytest
from openpyxl import load_workbook
from PySide6.QtCore import QSettings

from src.core.settings import get_settings
from src.desktop_v2.delivery_template import DeliveryTemplateStore
from src.desktop_v2.document_hub import DocumentAction
from src.domain.document_templates import DocumentTemplateProfile
from src.domain.document_column_mappings import MAPPING_ENTITY_KIND, mapping_entity_id
from src.domain.document_column_sources import AUTO_HISTORY
from src.domain.projections import roster_from_historical_snapshot, roster_entity_states
from src.domain.revisions import EntityRef, HistoricalSnapshot
from src.domain.roster import Meal
from src.excel.delivery_templates import validate_delivery_template
from src.generators.happy_delivery_report import build_happy_report_workbook
from src.services.happy_delivery_report import prepare_happy_report_data
from tests.test_sqlite_delivery_report_service import _snapshot


def paths():
    return {key: get_settings().get_happy_report_template_path(meal)
            for key, meal in (("dinner", "석식"), ("lunch", "중식"))}


def test_physical_quantities_both_layouts_styles_merges_and_household_distribution(tmp_path):
    snapshot, _ = _snapshot(people=2, meals=tuple(Meal))
    result = prepare_happy_report_data(snapshot, date(2026, 8, 17), date(2026, 8, 21), tuple(Meal))
    assert [(r.breakfast_total, r.dinner_total, r.lunch_total) for r in result.reports] == [(6, 2, 2)] * 5
    original = {k: Path(p).read_bytes() for k, p in paths().items()}
    workbook = build_happy_report_workbook(result.reports, paths())
    target = tmp_path / "daily.xlsx"
    workbook.save(target)
    workbook.close()
    saved = load_workbook(target)
    try:
        assert saved.sheetnames[:2] == ["26.08.21", "중식_26.08.21"]
        dinner, lunch = saved["26.08.20"], saved["중식_26.08.20"]
        assert dinner["E4"].value == dinner["E5"].value == 1
        assert dinner["J4"].value == "2*1"  # two regions, one stored household
        assert dinner["H4"].value == "=SUM(E4:E5)"
        assert dinner["E6"].value == "=SUM(E4:E5)"
        assert "B4:B5" in {str(v) for v in dinner.merged_cells.ranges}
        assert "F4:H5" in {str(v) for v in lunch.merged_cells.ranges}
        assert lunch["D4"].value == lunch["D5"].value == 1
        assert dinner.print_area == "'26.08.20'!$A$1:$I$6"
        assert dinner["G6"].value is dinner["J6"].value is lunch["J6"].value is None
        assert dinner.column_dimensions["G"].hidden
        assert dinner.column_dimensions["J"].hidden
        assert lunch.column_dimensions["J"].hidden
        assert dinner.column_dimensions["D"].min == 4
        assert dinner.column_dimensions["D"].max == 5
        assert dinner.column_dimensions["D"].width == 17
        template = load_workbook(paths()["dinner"])
        assert copy(dinner["C4"].font) == copy(template.active["C4"].font)
        assert dinner["A1"].number_format == template.active["A1"].number_format
        template.close()
    finally:
        saved.close()
    assert original == {k: Path(p).read_bytes() for k, p in paths().items()}


def test_shared_mapping_fallback_zero_and_literal_notes_for_both_meals(tmp_path):
    snapshot, _ = _snapshot(meals=(Meal.DINNER, Meal.LUNCH))
    roster = roster_from_historical_snapshot(snapshot)
    roster = replace(roster, persons=(replace(roster.persons[0], profile=replace(roster.persons[0].profile,
        mobile_phone="010-0000-0000", note="=1+1")),))
    entities = {**snapshot.entities, **roster_entity_states(roster)}
    entities[EntityRef(MAPPING_ENTITY_KIND, mapping_entity_id("행복도시락", "daily_report"))] = {
        "policy_version": 2, "profile": "행복도시락", "document_kind": "daily_report", "version": 1,
        "overrides": {"driver": "핸드폰", "region": "", "address": "", "note": "비고"}, "fallbacks": {"region": "성명"},
        "changed_in_revision_id": str(uuid4())}
    mapped = HistoricalSnapshot(snapshot.revision_id, entities)
    result = prepare_happy_report_data(mapped, date(2026, 8, 17), date(2026, 8, 17), (Meal.DINNER, Meal.LUNCH))
    row = result.reports[0].rows[0]
    assert row.route.driver == "010-0000-0000"
    assert row.route.region == "아동1"
    workbook = build_happy_report_workbook(result.reports, paths())
    for sheet in workbook:
        assert sheet["B4"].value == "010-0000-0000"
        assert sheet["I4"].value == "=1+1"
        assert sheet["I4"].data_type == "s"
    workbook.close()


def test_variant_selection_is_independent_and_wrong_variant_is_rejected(tmp_path):
    prefs = QSettings(str(tmp_path / "prefs.ini"), QSettings.Format.IniFormat)
    store = DeliveryTemplateStore("synthetic", profile=DocumentTemplateProfile.HAPPY_LUNCHBOX,
        settings=prefs, root=tmp_path / "templates")
    action = DocumentAction.DELIVERY_REPORT
    dinner, lunch = store.create(action), store.create(action, meal="중식")
    store.save(action, dinner)
    store.save(action, lunch, meal="중식")
    assert store.selected(action) == dinner
    assert store.selected(action, meal="중식") == lunch
    with pytest.raises(ValueError):
        store.save(action, dinner, meal="중식")
    assert store.selected(action, meal="중식") == lunch
    store.save(action, None)
    assert store.selected(action) is None
    assert store.selected(action, meal="중식") == lunch
    validate_delivery_template(lunch, "happy_report_lunch")


def test_unassigned_driver_goes_to_unclassified_and_missing_lunch_is_excluded():
    snapshot, _ = _snapshot(include_driver=False, meals=(Meal.DINNER, Meal.LUNCH))
    result = prepare_happy_report_data(snapshot, date(2026, 8, 17), date(2026, 8, 17), (Meal.DINNER,))
    assert result.reports[0].rows[0].route.driver == "미분류"
    assert result.reports[0].rows[0].route.region == "미분류"
    missing = prepare_happy_report_data(snapshot, date(2026, 10, 5), date(2026, 10, 5), (Meal.LUNCH,))
    assert not missing.shipments
    assert missing.calculation_manifest["missing_lunch_schedules"]


def test_mapping_dialog_exposes_shared_daily_scope_for_both_profiles():
    from PySide6.QtWidgets import QApplication
    from src.desktop_v2.document_column_mapping import DocumentColumnMappingDialog, _SCOPES
    from src.services.document_column_mappings import mapping_editor_spec
    app = QApplication.instance() or QApplication([])
    source, _ = _snapshot()
    roster = roster_from_historical_snapshot(source)
    specs = {scope: mapping_editor_spec(*scope, roster=roster) for scope in _SCOPES}
    dialog = DocumentColumnMappingDialog(specs, {scope: {} for scope in specs})
    dialog._profile_combo.setCurrentIndex(dialog._profile_combo.findData("행복도시락"))
    dialog._document_combo.setCurrentIndex(dialog._document_combo.findData("daily_report"))
    assert dialog.document_kind == "daily_report"
    assert set(dialog.desired_mapping()) == {"driver", "region", "address", "note"}
    assert dialog.desired_mapping()["driver"] == "배송담당"
    assert "같은 연결" in dialog._scope_note.text()
    dialog._profile_combo.setCurrentIndex(dialog._profile_combo.findData("급식"))
    assert dialog.document_kind == "daily_report"
    assert dialog.selected_profile == "급식"
    assert dialog._document_combo.model().item(dialog._document_combo.findData("daily_report")).isEnabled()
    assert set(dialog.desired_mapping()) == {"driver", "region", "address", "note"}
    assert dialog.desired_mapping()["note"] == AUTO_HISTORY
    dialog.reject()


def test_main_totals_and_hidden_package_columns_are_preserved(tmp_path):
    from src.services.happy_delivery_report import DailyDeliveryRow, DailyDeliverySheet, DeliveryRoute
    rows = (
        DailyDeliveryRow(DeliveryRoute("가기사", "가동"), breakfast=3, dinner=1, lunch=1,
                         _dinner_household_keys=["shared"], _lunch_household_keys=["shared"]),
        DailyDeliveryRow(DeliveryRoute("가기사", "나동"), breakfast=3, dinner=1, lunch=1,
                         _dinner_household_keys=["shared"], _lunch_household_keys=["shared"]),
        DailyDeliveryRow(DeliveryRoute("나기사", "다동"), breakfast=3, dinner=1, lunch=1,
                         _dinner_household_keys=["shared"], _lunch_household_keys=["shared"]),
    )
    report = DailyDeliverySheet(date(2026, 8, 17), rows, 9, 3, 3)
    workbook = build_happy_report_workbook((report,), paths())
    target = tmp_path / "totals.xlsx"
    workbook.save(target)
    workbook.close()
    saved = load_workbook(target)
    try:
        dinner, lunch = saved.worksheets
        assert dinner["F4"].value == "=SUM(D4:D5)"
        assert dinner["H4"].value == "=SUM(E4:E5)"
        assert dinner["H6"].value == "=SUM(E6:E6)"
        assert dinner["E7"].value == "=SUM(E4:E6)"
        assert lunch["F4"].value == "=SUM(D4:D5)"
        assert lunch["F7"].value == "=SUM(F4:F6)"
        for sheet in saved:
            assert sheet["J4"].value == "2*1"
            assert sheet["J6"].value == "1*1"
            assert sheet["J7"].value is None
            assert sheet.column_dimensions["J"].hidden
            assert "$I$7" in sheet.print_area
        assert dinner["G7"].value is None
        assert dinner.column_dimensions["G"].hidden
        assert dinner["J3"].value == "석식갯수"
    finally:
        saved.close()
