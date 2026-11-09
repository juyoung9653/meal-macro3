"""Lunch import filters operate on source cells before parsing excluded rows."""
from tests.import_wizard_helpers import advance_wizard

import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from dataclasses import replace
from uuid import uuid4

import openpyxl
import pytest
from openpyxl.worksheet.table import Table
from PySide6.QtWidgets import QApplication, QDialog

from src.domain.roster import Meal
from src.desktop_v2.import_wizard import RosterImportWizard
from src.importing.contracts import ImportMappingProfile, ImportRowFilter, ProfileField
from src.importing.profile_history import StoredImportProfile
from src.importing.revision import ImportPurpose
from src.importing.workbook import parse_workbook, inspect_workbook


def profile(row_filter=None):
    return ImportMappingProfile(uuid4(),1,'중식','xlsx',
        {ProfileField.NAME:'A',ProfileField.BIRTH_DATE:'B'},default_meals=(Meal.LUNCH,),
        ignored_columns=('C',),row_filter=row_filter)


def workbook(path, rows):
    book=openpyxl.Workbook()
    book.active.append(('성명','생년월일','중식 대상'))
    for row in rows:
        book.active.append(row)
    book.active.add_table(Table(displayName='명단',ref=f'A1:C{book.active.max_row}'))
    book.save(path)
    book.close()


@pytest.mark.parametrize('condition,expected,excluded',[
    (None,('빈칸','공백','숫자영','대상','비대상'),()),
    (ImportRowFilter('C','nonempty'),('숫자영','대상','비대상'),(2,3)),
    (ImportRowFilter('C','equals','대상'),('대상',),(2,3,4,6)),
    (ImportRowFilter('중식 대상','equals','0'),('숫자영',),(2,3,5,6)),
])
def test_all_nonempty_and_exact_value(tmp_path,condition,expected,excluded):
    path=tmp_path/'lunch.xlsx'
    workbook(path,[(n,'20150101',v) for n,v in [('빈칸',None),('공백','  '),('숫자영',0),('대상',' 대상 '),('비대상','비대상')]])
    preview=parse_workbook(path,profile(condition),table_name='명단')
    assert tuple(o.name for o in preview.observations)==expected
    assert preview.filtered_rows==excluded
    assert not preview.notices and preview.can_continue
    assert ImportMappingProfile.from_manifest(preview.profile.manifest())==preview.profile


def test_excluded_invalid_row_is_not_an_error_but_filter_formula_is(tmp_path):
    path=tmp_path/'lunch.xlsx'
    workbook(path,[('선택','20150101','1'),(None,'bad date','0')])
    preview=parse_workbook(path,profile(ImportRowFilter('C','equals','1')))
    assert preview.can_continue and not preview.issues
    assert preview.filtered_rows==(3,)
    workbook(path,[('선택','20150101','1'),(None,'bad date','=1')])
    preview=parse_workbook(path,profile(ImportRowFilter('C','equals','1')))
    assert not preview.can_continue
    assert any(issue.column=='C' and '수식' in issue.message for issue in preview.blocking_issues)


def test_zero_matches_blocks_import_and_cell_overrides_apply_before_filter(tmp_path):
    path=tmp_path/'lunch.xlsx'
    workbook(path,[('선택','20150101','0')])
    p=profile(ImportRowFilter('C','equals','1'))
    preview=parse_workbook(path,p)
    assert not preview.can_continue and preview.filtered_rows==(2,)
    assert '조건' in preview.blocking_issues[0].message
    corrected=parse_workbook(path,p,cell_overrides={(2,'C'):'1'})
    assert corrected.can_continue and corrected.filtered_rows==()


def test_filter_preserves_excel_leading_zero_display(tmp_path):
    path=tmp_path/'lunch.xlsx'
    workbook(path,[('선택','20150101',1)])
    book=openpyxl.load_workbook(path)
    book.active['C2'].number_format='000'
    book.save(path);book.close()
    assert parse_workbook(path,profile(ImportRowFilter('C','equals','001'))).can_continue
    assert not parse_workbook(path,profile(ImportRowFilter('C','equals','1'))).can_continue


def test_wizard_requires_filtered_preview_and_normal_import_ignores_filter(tmp_path):
    app=QApplication.instance() or QApplication([])
    path=tmp_path/'lunch.xlsx'
    workbook(path,[('대상','20150101','1'),('제외','20150102','0')])
    wizard=RosterImportWizard(allow_vacation_lunch=True)
    from tests.test_vacation_lunch_period_import import calendar_entry
    wizard.set_lunch_calendar_entries((calendar_entry(),))
    try:
        wizard.purpose_combo.setCurrentIndex(wizard.purpose_combo.findData(ImportPurpose.VACATION_LUNCH))
        wizard.path_edit.setText(str(path))
        assert wizard._inspect_source()
        advance_wizard(wizard)
        wizard.lunch_filter_mode.setCurrentIndex(wizard.lunch_filter_mode.findData('equals'))
        wizard.lunch_filter_column.setCurrentIndex(wizard.lunch_filter_column.findData('C'))
        wizard.lunch_filter_value.setText('1')
        advance_wizard(wizard)
        assert wizard.page_stack.currentIndex()==2
        assert wizard.result()!=QDialog.DialogCode.Accepted
        assert [o.name for o in wizard.preview.observations]==['대상']
        assert '조건 제외 1행' in wizard.preview_summary.text()
        advance_wizard(wizard)
        assert wizard.selection().preview.profile.row_filter==ImportRowFilter('C','equals','1')
        wizard.purpose_combo.setCurrentIndex(wizard.purpose_combo.findData(ImportPurpose.DATABASE))
        wizard.meal_mode_combo.setCurrentIndex(wizard.meal_mode_combo.findData('flags'))
        wizard.lunch_combo.setCurrentIndex(wizard.lunch_combo.findData('C'))
        assert wizard._build_profile().row_filter is None
    finally:
        wizard.close();wizard.deleteLater();app.processEvents()


def test_saved_filter_is_restored_and_first_import_stays_full(tmp_path):
    app=QApplication.instance() or QApplication([])
    path=tmp_path/'lunch.xlsx'
    workbook(path,[('대상','20150101','1'),('제외','20150102','0')])
    inspection=inspect_workbook(path,table_name='명단')
    p=profile(ImportRowFilter('C','equals','1'))
    stored=StoredImportProfile(p,inspection.header_fingerprint)
    wizard=RosterImportWizard(existing_profiles=(stored,),allow_vacation_lunch=True)
    try:
        wizard.purpose_combo.setCurrentIndex(wizard.purpose_combo.findData(ImportPurpose.VACATION_LUNCH))
        wizard.path_edit.setText(str(path));assert wizard._inspect_source()
        advance_wizard(wizard)
        assert wizard._build_profile().row_filter==p.row_filter
    finally:
        wizard.close();wizard.deleteLater();app.processEvents()
    first=RosterImportWizard()
    try:
        assert first.purpose_combo.findData(ImportPurpose.VACATION_LUNCH)<0
        assert first.lunch_filter_panel.isHidden()
    finally:
        first.close();first.deleteLater();app.processEvents()


def test_lunch_filter_records_excluded_rows_and_preserves_stop_semantics():
    from tests.test_vacation_lunch_import import _preview,_build,OMIT,RESTORE
    preview=_preview('유지')
    preview=replace(preview,profile=replace(preview.profile,row_filter=ImportRowFilter('C','equals','대상')),
                    filtered_rows=(3,4))
    prepared=_build(preview)
    assert prepared.record.metadata.decisions['filtered_source_rows']==(3,4)
    assert set(prepared.record.metadata.decisions['vacation_lunch']['stopped'])=={str(OMIT),str(RESTORE)}
    assert prepared.record.import_batch.mapping_manifest['row_filter']['value']=='대상'
