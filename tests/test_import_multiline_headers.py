"""Two-row government rosters retain one editable source per physical column."""
import json
from dataclasses import replace
from datetime import date
from uuid import uuid4

import openpyxl
import pytest
from openpyxl.worksheet.table import Table
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtWidgets import QApplication

from src.desktop_v2.import_wizard import RosterImportWizard
from src.domain.roster import Meal
from src.importing.contracts import ImportMappingProfile, ProfileField
from src.importing.profile_history import StoredImportProfile, stored_import_profile_from_state
from src.importing.revision import ImportPurpose
from src.importing.workbook import WorkbookStructureError, inspect_workbook, parse_workbook, proposed_profile


def _fill_sheet(sheet, *, start=1, name="합성아동"):
    sheet.title = "중식 명단"
    for column, label in {"A":"연번", "B":"구", "C":"관할동", "D":"주소",
                          "I":"생년월일", "J":"전화번호", "K":"성별",
                          "R":"⑦기타 (지역사회 추천자)", "S":"지역사회\n추천 사유", "V":"비고"}.items():
        sheet[f"{column}{start}"] = label
        sheet.merge_cells(f"{column}{start}:{column}{start+1}")
    for left, right, label in (("E","F","취학유형"), ("G","H","대상자"),
                               ("L","Q","지원 대상"), ("T","U","구분")):
        sheet[f"{left}{start}"] = label
        sheet.merge_cells(f"{left}{start}:{right}{start}")
    for column, label in {"E":"미취학, 초, 중, 고, 기타(학교 밖)", "F":"학교명", "G":"학년", "H":"성명",
                          "L":"① 수급자 가정", "M":"② 한부모 가정", "N":"③ 긴급복지 가정",
                          "O":"④ 보호자 부재", "P":"⑤ 양육 능력 미약", "Q":"⑥ 기준 중위소득 52% 이하",
                          "T":"2025 겨울방학 지원 대상자", "U":"2026 여름방학 지원 대상자"}.items():
        sheet[f"{column}{start+1}"] = label
    values = [1, "합성구", "합성동", "예시로 1", "초", "예시초", 3, name, "081217",
              "010-0000-0000", "여", 1, 0, 0, 0, 0, 0, "추천", "합성 사유", "○", "○", "합성 비고"]
    for column, value in enumerate(values, 1):
        sheet.cell(start + 2, column, value)


def _file(tmp_path, *, start=1):
    path = tmp_path / "two-rows.xlsx"
    book = openpyxl.Workbook()
    _fill_sheet(book.active, start=start)
    book.save(path)
    book.close()
    return path


def _profile(*, start=1):
    return ImportMappingProfile(uuid4(), 1, "중식 양식", "roster-xlsx",
        {ProfileField.NAME: "H", ProfileField.BIRTH_DATE: "I", ProfileField.SCHOOL_TYPE: "E",
         ProfileField.SCHOOL_NAME: "F", ProfileField.MOBILE_PHONE: "J"},
        default_meals=(Meal.LUNCH,), header_row=start, header_row_count=2, data_start_row=start+2)


def test_two_row_headers_keep_child_columns_and_parse_six_digit_birth_date(tmp_path):
    path = _file(tmp_path)
    original = path.read_bytes()
    inspection = inspect_workbook(path, require_table=True, allow_two_row_headers=True)
    assert inspection.selected_table is None and inspection.header_row_count == 2
    columns = {column.letter: column.header for column in inspection.columns}
    assert len(columns) == 22
    assert columns["E"] == "미취학, 초, 중, 고, 기타(학교 밖)"
    assert columns["F"] == "학교명" and columns["G"] == "학년" and columns["H"] == "성명"
    assert columns["I"] == "생년월일" and columns["L"] == "① 수급자 가정"
    assert columns["T"] == "2025 겨울방학 지원 대상자"
    assert columns["U"] == "2026 여름방학 지원 대상자"
    assert [row.row_number for row in inspection.preview_rows] == [3]
    profile = _profile()
    preview = parse_workbook(path, profile, sheet_name=inspection.selected_sheet)
    assert preview.can_continue, preview.blocking_issues
    assert len(preview.observations) == 1
    assert preview.observations[0].name == "합성아동"
    assert preview.observations[0].birth_date == date(2008, 12, 17)
    assert preview.observations[0].meals == (Meal.LUNCH,)
    assert preview.header_fingerprint == inspection.header_fingerprint
    stored = StoredImportProfile(profile, inspection.header_fingerprint)
    restored = stored_import_profile_from_state(profile.profile_id, json.loads(json.dumps(stored.state())))
    assert restored == stored
    assert proposed_profile(inspection, (restored,)) == profile
    old = replace(profile, header_row_count=1, data_start_row=2)
    old_manifest = old.manifest()
    assert "header_row_count" not in old_manifest
    assert ImportMappingProfile.from_manifest(old_manifest) == old
    with pytest.raises(ValueError):
        replace(profile, data_start_row=2)
    assert path.read_bytes() == original


def test_narrow_heading_wins_over_lower_merged_heading_and_duplicate_names_stay_separate(tmp_path):
    path = tmp_path / "specific.xlsx"
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.append(["성명", "생년월일", "지원", None])
    sheet.merge_cells("C1:D1")
    sheet["A2"] = "대상자 정보"
    sheet.merge_cells("A2:B2")
    sheet["C2"] = "대상"
    sheet["D2"] = "대상"
    sheet.append(["합성", 81217, "이전", "현재"])
    book.save(path)
    book.close()
    inspection = inspect_workbook(path, header_row_count=2)
    assert [(c.letter, c.header) for c in inspection.columns] == [
        ("A", "성명"), ("B", "생년월일"), ("C", "대상"), ("D", "대상")]
    profile = ImportMappingProfile(uuid4(), 1, "중복 제목", "roster-xlsx",
        {ProfileField.NAME: "A", ProfileField.BIRTH_DATE: "B", ProfileField.NOTE: "D"},
        default_meals=(Meal.LUNCH,), header_row_count=2, data_start_row=3)
    preview = parse_workbook(path, profile)
    assert preview.can_continue
    assert preview.observations[0].profile_values[ProfileField.NOTE] == "현재"


def test_header_merge_cannot_consume_first_person_row(tmp_path):
    path = _file(tmp_path)
    book = openpyxl.load_workbook(path)
    book.active.unmerge_cells("V1:V2")
    book.active.merge_cells("V1:V3")
    book.save(path)
    book.close()
    with pytest.raises(WorkbookStructureError, match="두 줄 밖"):
        inspect_workbook(path, header_row_count=2)


@pytest.fixture
def make_wizard():
    app = QApplication.instance() or QApplication([])
    windows = []
    def create(**kwargs):
        wizard = RosterImportWizard(allow_vacation_lunch=True, **kwargs)
        wizard.purpose_combo.setCurrentIndex(wizard.purpose_combo.findData(ImportPurpose.VACATION_LUNCH))
        windows.append(wizard)
        return wizard
    yield create
    for wizard in windows:
        wizard.close()
        wizard.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_wizard_suggests_leaf_headers_remembers_edits_and_can_read_a_table_next(tmp_path, make_wizard):
    path = _file(tmp_path)
    wizard = make_wizard()
    wizard.path_edit.setText(str(path))
    assert wizard._inspect_source(), wizard.error_label.text()
    assert wizard.source_layout_combo.currentData() == "two-row"
    assert wizard._mapping_combos[ProfileField.NAME].currentData() == "H"
    assert wizard._mapping_combos[ProfileField.SCHOOL_NAME].currentData() == "F"
    wizard._set_combo(wizard._mapping_combos[ProfileField.SCHOOL_TYPE], "E")
    wizard._set_combo(wizard._mapping_combos[ProfileField.TELEPHONE], None)
    wizard._set_combo(wizard._mapping_combos[ProfileField.MOBILE_PHONE], "J")
    wizard.ignored_columns_list.item(10).setCheckState(Qt.CheckState.Unchecked)
    wizard._go_next()
    wizard._go_next()
    assert wizard.preview is not None and wizard.preview.can_continue, wizard.error_label.text()
    profile = wizard.preview.profile
    assert profile.header_row_count == 2 and profile.data_start_row == 3
    assert tuple(profile.custom_field_columns.values()) == ("K",)
    stored = StoredImportProfile(profile, wizard.preview.header_fingerprint)
    restored = stored_import_profile_from_state(profile.profile_id, json.loads(json.dumps(stored.state())))
    reused = make_wizard(existing_profiles=(restored,))
    reused.path_edit.setText(str(path))
    assert reused._inspect_source()
    assert reused._build_profile() == profile
    assert reused.ignored_columns_list.item(10).checkState() == Qt.CheckState.Unchecked
    assert reused._mapping_combos[ProfileField.NOTE].isEnabled()
    reused._set_combo(reused._mapping_combos[ProfileField.NOTE], "S")
    assert reused._build_profile().version == profile.version + 1
    table_path = tmp_path / "table.xlsx"
    book = openpyxl.Workbook()
    book.active.append(["성명", "생년월일"])
    book.active.append(["다른아동", "20150302"])
    book.active.add_table(Table(displayName="명단", ref="A1:B2"))
    book.save(table_path)
    book.close()
    reused.path_edit.setText(str(table_path))
    assert reused._inspect_source()
    assert reused.inspection.selected_table == "명단"
    assert reused.inspection.header_row_count == 1


def test_explicit_header_start_and_selected_sheet_reach_the_parser(tmp_path, make_wizard):
    path = _file(tmp_path, start=4)
    book = openpyxl.load_workbook(path)
    other = book.create_sheet()
    _fill_sheet(other, start=4, name="선택한아동")
    other.title = "다른 중식"
    book.save(path)
    book.close()
    wizard = make_wizard()
    wizard.path_edit.setText(str(path))
    wizard.source_layout_combo.setCurrentIndex(wizard.source_layout_combo.findData("two-row"))
    wizard.header_row_spin.setValue(4)
    assert wizard._inspect_source(), wizard.error_label.text()
    wizard.table_combo.setCurrentIndex(wizard.table_combo.findData("다른 중식"))
    assert wizard.inspection.selected_sheet == "다른 중식"
    wizard._go_next()
    wizard._go_next()
    assert wizard.preview is not None and wizard.preview.can_continue, wizard.error_label.text()
    assert [person.name for person in wizard.preview.observations] == ["선택한아동"]
    assert wizard.preview.profile.data_start_row == 6
