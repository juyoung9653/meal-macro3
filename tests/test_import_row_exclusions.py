"""Explicit source-row exclusions and mandatory import mapping review."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import openpyxl
import pytest
from openpyxl.worksheet.table import Table
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QDialog

from src.desktop_v2.import_wizard import RosterImportWizard
from src.domain.roster import Meal
from src.importing.contracts import ImportMappingProfile, ProfileField, DateNormalization, PhoneNormalization
from src.importing.profile_history import StoredImportProfile
from src.importing.revision import ImportPurpose
from src.importing.workbook import parse_workbook, inspect_workbook, WorkbookStructureError
from src.storage.database_session import DatabaseSession
from tests.import_wizard_helpers import advance_wizard, select_single_header_source
from tests.test_import_profile_columns import prepare


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def source(path, *, multiple_errors=False, blank_name=False):
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.append(("성명", "생년월일", "식종", "핸드폰"))
    sheet.append((None if blank_name else "오류 대상", "잘못된 날짜" if not blank_name else "20150302", "야식" if multiple_errors else "중식", "010-1111-1111"))
    sheet.append(("정상 대상", "20160403", "중식", "010-2222-2222"))
    sheet.add_table(Table(displayName="명단", ref="A1:D3"))
    book.save(path)
    book.close()
    return path


def profile():
    return ImportMappingProfile(uuid4(), 1, "합성 명단", "xlsx", {
        ProfileField.NAME: "A", ProfileField.BIRTH_DATE: "B", ProfileField.MOBILE_PHONE: "D",
    }, combined_meals_column="C")


def test_exclusion_skips_invalid_cells_before_parsing_and_preserves_source(tmp_path):
    path = source(tmp_path / "rows.xlsx", multiple_errors=True)
    original = path.read_bytes()
    p = profile()
    assert not parse_workbook(path, p, table_name="명단").can_continue
    preview = parse_workbook(path, p, table_name="명단", excluded_rows=(2,), cell_overrides={(2, "B"): "still invalid"})
    assert preview.can_continue and not preview.issues
    assert preview.excluded_rows == (2,)
    assert preview.corrected_cells == ()
    assert [o.name for o in preview.observations] == ["정상 대상"]
    assert preview.observations[0].source.row_numbers == (3,)
    assert path.read_bytes() == original
    assert not parse_workbook(path, p, table_name="명단", excluded_rows=(2, 3)).can_continue


@pytest.mark.parametrize("rows", [(0,), (True,), (1, True), ("2",), (1,), (4,)])
def test_exclusions_reject_invalid_or_out_of_body_row_numbers(tmp_path, rows):
    path = source(tmp_path / "rows.xlsx")
    with pytest.raises((ValueError, WorkbookStructureError)):
        parse_workbook(path, profile(), table_name="명단", excluded_rows=rows)


def test_explicit_exclusion_is_saved_in_revision_and_not_reused_as_profile(tmp_path):
    path = source(tmp_path / "rows.xlsx")
    p = profile()
    preview = parse_workbook(path, p, table_name="명단", excluded_rows=(2,))
    prepared = prepare(preview)
    assert list(prepared.record.metadata.decisions["excluded_source_rows"]) == [2]
    backups = tmp_path / "backups"
    backups.mkdir()
    with DatabaseSession.create(tmp_path / "db.sqlite3", "test-password", backup_directory=backups) as session:
        assert session.append_revision(prepared.record).succeeded
    with DatabaseSession.open(tmp_path / "db.sqlite3", "test-password") as session:
        assert list(session.database.latest_revision().metadata.decisions["excluded_source_rows"]) == [2]
    assert "excluded_rows" not in p.manifest()
    assert not parse_workbook(path, p, table_name="명단").can_continue


def test_mapping_must_be_confirmed_and_edits_require_review_again(app, tmp_path):
    wizard = RosterImportWizard()
    select_single_header_source(wizard, source(tmp_path / "rows.xlsx"))
    assert wizard._inspect_source()
    wizard._go_next()
    assert wizard.next_button.text() == "열 연결 확인"
    wizard._go_next()
    assert wizard.mapping_dialog.isVisible()
    assert wizard.preview is None
    wizard.mapping_dialog.close()
    assert not wizard._mapping_review_complete()
    wizard._go_next()
    wizard.mapping_dialog.done_button.click()
    assert wizard._mapping_review_complete()
    assert wizard.next_button.text() == "불러오기"
    wizard.destination_table.assign_source("D", "ignore")
    assert not wizard._mapping_review_complete()
    wizard._go_next()
    assert wizard.preview is None
    assert wizard.mapping_dialog.isVisible()
    wizard.close()


@pytest.mark.parametrize("purpose", [ImportPurpose.DATABASE, ImportPurpose.VACATION_LUNCH])
def test_error_dialog_opens_first_and_row_exclusion_allows_import(app, tmp_path, purpose):
    wizard = RosterImportWizard(allow_vacation_lunch=True)
    from tests.test_vacation_lunch_period_import import calendar_entry
    wizard.set_lunch_calendar_entries((calendar_entry(),))
    wizard.purpose_combo.setCurrentIndex(wizard.purpose_combo.findData(purpose))
    select_single_header_source(wizard, source(tmp_path / "rows.xlsx", multiple_errors=True))
    assert wizard._inspect_source()
    advance_wizard(wizard)
    advance_wizard(wizard)
    assert wizard.correction_dialog.isVisible()
    assert wizard.result() != QDialog.DialogCode.Accepted
    assert wizard._preview.blocking_issues
    item = wizard.correction_table.item(0, 0)
    item.setCheckState(Qt.CheckState.Checked)
    for index in range(wizard.correction_table.rowCount()):
        assert wizard.correction_table.item(index, 0).checkState() == Qt.CheckState.Checked
        editor = wizard.correction_table.cellWidget(index, 5)
        assert editor is None or not editor.isEnabled()
    wizard.correction_apply_button.click()
    assert wizard.result() == QDialog.DialogCode.Accepted, wizard.error_label.text()
    assert wizard.selection().preview.excluded_rows == (2,)
    assert [o.name for o in wizard.selection().preview.observations] == ["정상 대상"]
    assert not wizard.correction_dialog.isVisible()
    wizard.close()


def test_lunch_blank_name_requires_explicit_skip_and_hides_inactive_controls(app, tmp_path):
    wizard = RosterImportWizard(allow_vacation_lunch=True)
    from tests.test_vacation_lunch_period_import import calendar_entry
    wizard.set_lunch_calendar_entries((calendar_entry(),))
    wizard.purpose_combo.setCurrentIndex(wizard.purpose_combo.findData(ImportPurpose.VACATION_LUNCH))
    select_single_header_source(wizard, source(tmp_path / "rows.xlsx", blank_name=True))
    assert wizard._inspect_source()
    advance_wizard(wizard)
    assert wizard.meal_mapping_toggle.isHidden()
    assert wizard.meal_guide.isHidden()
    assert not wizard.lunch_filter_panel.isHidden()
    advance_wizard(wizard)
    assert wizard.correction_dialog.isVisible()
    assert wizard.preview.notices
    wizard.correction_table.item(0, 0).setCheckState(Qt.CheckState.Checked)
    wizard.correction_apply_button.click()
    assert wizard.result() == QDialog.DialogCode.Accepted
    assert not wizard.selection().preview.notices
    assert wizard.selection().preview.excluded_rows == (2,)
    wizard.close()


def test_empty_import_blocked_and_exclusions_can_be_reset(app, tmp_path):
    wizard = RosterImportWizard()
    select_single_header_source(wizard, source(tmp_path / "rows.xlsx"))
    assert wizard._inspect_source()
    advance_wizard(wizard)
    wizard._open_mapping_dialog()
    wizard.mapping_dialog.done_button.click()
    wizard._excluded_rows.update((2, 3))
    assert wizard._load_preview(wizard._build_profile(), title="검사", detail="검사")
    wizard._set_page(2)
    assert not wizard._can_import()
    wizard._reset_error_corrections()
    assert not wizard._excluded_rows
    assert wizard.correction_dialog.isVisible()
    assert wizard.preview.blocking_issues
    wizard._set_page(0)
    assert wizard._inspect_source()
    assert not wizard._excluded_rows and not wizard._pending_excluded_rows
    wizard.close()


def test_saved_manual_normalization_becomes_automatic(app, tmp_path):
    path = source(tmp_path / "rows.xlsx")
    inspection = inspect_workbook(path, table_name="명단")
    old = replace(profile(), version=3, date_normalization=DateNormalization.YYYYMMDD, phone_normalization=PhoneNormalization.PRESERVE)
    wizard = RosterImportWizard(existing_profiles=(StoredImportProfile(old, inspection.header_fingerprint),))
    select_single_header_source(wizard, path)
    assert wizard._inspect_source()
    current = wizard._build_profile()
    assert current.date_normalization == DateNormalization.AUTO
    assert current.phone_normalization == PhoneNormalization.KOREAN
    assert current.name == old.name and current.version == old.version + 1
    assert not hasattr(wizard, "date_normalization_combo")
    assert not hasattr(wizard, "phone_normalization_combo")
    wizard.close()
