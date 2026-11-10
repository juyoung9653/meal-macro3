"""Imported standard and custom columns remain usable after encrypted reopen."""

import os
from dataclasses import replace
from datetime import UTC, date, datetime
from uuid import uuid4

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import openpyxl
import pytest
from openpyxl.worksheet.table import Table
from PySide6.QtCore import QCoreApplication, QEvent, QSettings, Qt
from PySide6.QtWidgets import QApplication, QDialog

from tests.import_wizard_helpers import select_single_header_source

from src.desktop_v2.application import DesktopStartupController
from src.desktop_v2.import_wizard import RosterImportWizard
from src.desktop_v2.roster_cell_edit import is_roster_cell_editable, updated_profile_for_cell
from src.desktop_v2.roster_row_edit import RosterRowCreateDialog
from src.desktop_v2.roster_window import RosterTableModel, RosterWindow
from src.domain.revisions import RevisionActor
from src.domain.roster import Meal, Person, PersonProfile, RosterSnapshot
from src.importing.application import IdentityResolution, ReconciliationDecision
from src.importing.contracts import ProfileField
from src.importing.profile_history import import_profiles_from_snapshot
from src.importing.reconciliation import stage_reconciliation
from src.importing.revision import build_import_revision
from src.importing.workbook import parse_workbook
from src.services.document_column_mappings import mapping_editor_spec
from src.storage.database_session import DatabaseSession

DAY = date(2026, 10, 15)
OPTIONAL = {"school_type": "학교 유형", "household_head_name": "세대주 성명", "relationship_to_child": "아동과의 관계"}


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def dispose_widgets(app):
    yield
    for widget in QApplication.topLevelWidgets():
        widget.close()
        widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def make_source(tmp_path, blank=False):
    path = tmp_path / "synthetic.xlsx"
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(("성명", "생년월일", "식종", "학교 유형", "학교명", "학년", "세대주 성명", "아동과의 관계", "관할동", "도로명주소", "상세주소", "전화번호", "핸드폰", "비고", "관리코드", "제외 항목", "반", "제공기관"))
    sheet.append(("합성 아동", "20150302", "석식", None if blank else "초등학교", "가상초", "5", None if blank else "합성 보호자", None if blank else "모", "가상동", "가상로 1", "101호", "031-111-2222", "010-1111-2222", "합성 메모", "001", "제외값", "2반", "원본 기관"))
    sheet.add_table(Table(displayName="명단", ref="A1:R2"))
    workbook.save(path)
    workbook.close()
    return path


def prepare(preview):
    plan = stage_reconciliation(None, preview.observations, effective_on=DAY)
    decisions = {item.observation.observation_id: ReconciliationDecision(item.observation.observation_id, IdentityResolution.CREATE_NEW, uuid4()) for item in plan.staged_people}
    return build_import_revision(None, preview, plan, decisions=decisions, actor=RevisionActor("synthetic", "test"), recorded_at=datetime.now(UTC), revision_id=uuid4(), batch_id=uuid4())


@pytest.mark.parametrize("blank", [False, True])
@pytest.mark.parametrize("as_custom", [False, True])
def test_wizard_all_columns_survive_encrypted_reopen(app, tmp_path, blank, as_custom):
    path = make_source(tmp_path, blank)
    wizard = RosterImportWizard()
    select_single_header_source(wizard, path)
    assert wizard._inspect_source(), wizard.error_label.text()
    if as_custom:
        for key in OPTIONAL:
            wizard._set_combo(wizard._mapping_combos[ProfileField(key)], None)
    # Explicitly retain extra fields, including the intentionally unmapped class/provider.
    for row in range(wizard.ignored_columns_list.count()):
        item = wizard.ignored_columns_list.item(row)
        if item.data(Qt.ItemDataRole.UserRole) in {"D", "G", "H", "O", "Q", "R"}:
            item.setCheckState(Qt.CheckState.Unchecked)
    profile = wizard._build_profile()
    assert profile.ignored_columns == ("P",)
    assert set(profile.profile_columns) == set(ProfileField) - {ProfileField.CLASS_NAME, ProfileField.PROVIDER} - ({ProfileField(key) for key in OPTIONAL} if as_custom else set())
    preview = parse_workbook(path, profile, table_name="명단")
    assert preview.can_continue, preview.blocking_issues
    prepared = prepare(preview)
    database_path = tmp_path / "roster.sqlite3"
    backups = tmp_path / "backups"
    backups.mkdir()
    with DatabaseSession.create(database_path, "test-password", backup_directory=backups) as session:
        assert session.append_revision(prepared.record).succeeded
    with DatabaseSession.open(database_path, "test-password") as session:
        roster = session.database.current_roster()
        fields = DesktopStartupController._imported_profile_fields(session, roster)
        saved = import_profiles_from_snapshot(session.database.snapshot(roster.revision_id))
        reused = RosterImportWizard(existing_profiles=saved)
        select_single_header_source(reused, path)
        assert reused._inspect_source()
        assert reused._build_profile() == profile
        window = RosterWindow(roster, imported_profile_fields=fields, column_settings=QSettings(str(tmp_path / "view.ini"), QSettings.Format.IniFormat))
        window._set_day(DAY)
        model = window.table_model
        assert model.rowCount() == 1
        displayed = {column.label: model.data(model.index(0, index)) for index, column in enumerate(model.columns)}
        assert displayed.items() >= {"학교 유형": "" if blank else "초등학교", "세대주 성명": "" if blank else "합성 보호자", "아동과의 관계": "" if blank else "모", "학교": "가상초", "학년": "5", "성명": "합성 아동", "생년월일": "2015-03-02", "전화번호": "031-111-2222", "핸드폰": "010-1111-2222", "관할동": "가상동", "도로명주소": "가상로 1", "상세주소": "101호", "비고": "합성 메모", "관리코드": "001", "반": "2반", "제공기관": "원본 기관"}.items()
        assert "제외 항목" not in displayed
        assert model.column_filter_values("school_type" if not as_custom else next(column.key for column in model.columns if column.label == "학교 유형")) == (("",) if blank else ("초등학교",))
        if not blank:
            model.set_search("합성 보호자")
            assert model.rowCount() == 1
            model.set_search("")
        snapshot = window.current_sheet_snapshot()
        for label in OPTIONAL.values():
            assert label in snapshot.headers
            assert snapshot.rows[0][snapshot.headers.index(label)] == displayed[label]
        if blank and not as_custom:
            # A later import adds even all-blank fields to an already-open window.
            existing = RosterWindow(roster)
            assert OPTIONAL.keys().isdisjoint(column.key for column in existing.table_model.columns)
            DesktopStartupController()._replace_roster(session, existing, roster)
            assert OPTIONAL.keys() <= {column.key for column in existing.table_model.columns}
        if not as_custom:
            labels = {column.key: column.label for column in model.columns}
            spec = mapping_editor_spec("급식", "invoice", roster=roster, column_labels=labels)
            assert {"취학유형", "세대주 성명", "아동과의 관계"} <= set(spec.selectable_source_options)
            assert spec.source_labels["취학유형"] == "학교 유형"
            # Clearing a stored value and refreshing does not remove an imported column.
            person = roster.persons[0]
            edited = replace(roster, persons=(replace(person, profile=replace(person.profile, school_type=None)),))
            DesktopStartupController()._replace_roster(session, window, edited)
            assert OPTIONAL.keys() <= {column.key for column in model.columns}


def test_absent_fields_are_not_added_but_legacy_values_are_visible(app):
    plain = RosterSnapshot(uuid4(), (Person(uuid4(), PersonProfile("합성 아동")),))
    assert OPTIONAL.keys().isdisjoint(column.key for column in RosterTableModel(plain).columns)
    for key in OPTIONAL:
        populated = replace(plain, persons=(replace(plain.persons[0], profile=replace(plain.persons[0].profile, **{key: "기존값"})),))
        model = RosterTableModel(populated)
        column = next(index for index, item in enumerate(model.columns) if item.key == key)
        assert model.data(model.index(0, column)) == "기존값"


@pytest.mark.parametrize("key", OPTIONAL)
def test_restored_columns_use_standard_cell_and_create_editors(app, key):
    person = Person(uuid4(), PersonProfile("합성 아동", **{key: "기존값"}))
    assert is_roster_cell_editable(key)
    changed = updated_profile_for_cell(person, key, "수정값", ())
    assert getattr(changed, key) == "수정값"
    assert not changed.custom_values
    view = RosterRowCreateDialog(Meal.DINNER, column_labels={"name": "성명", key: OPTIONAL[key]})
    view._editors["name"].setText("신규 합성")
    view._editors[key].setText("새값")
    view._accept_valid()
    assert view.result() == QDialog.DialogCode.Accepted
    assert getattr(view.request().profile, key) == "새값"


def test_blank_mapped_column_layout_and_filters_survive_reopen(app, tmp_path):
    roster = RosterSnapshot(uuid4(), (Person(uuid4(), PersonProfile("합성 아동")),))
    fields = frozenset(OPTIONAL)
    settings_path = tmp_path / "columns.ini"
    first = RosterWindow(roster, imported_profile_fields=fields, column_settings=QSettings(str(settings_path), QSettings.Format.IniFormat))
    first._rename_column("school_type", "학교 구분")
    first._column_layout._set_hidden("household_head_name", True)
    first.table_model.set_column_values("school_type", frozenset())
    first._save_search_filters()
    first.close()
    restored = RosterWindow(roster, imported_profile_fields=fields, column_settings=QSettings(str(settings_path), QSettings.Format.IniFormat))
    columns = {column.key: index for index, column in enumerate(restored.table_model.columns)}
    assert restored.table_model.columns[columns["school_type"]].label == "학교 구분"
    assert restored._table.isColumnHidden(columns["household_head_name"])
    assert restored.table_model.selected_column_values("school_type") == frozenset()
    assert restored.table_model.rowCount() == 0
