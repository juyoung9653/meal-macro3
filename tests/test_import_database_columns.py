"""Workbook columns map to existing database IDs and preserve typed values."""
from dataclasses import replace
from datetime import date, datetime
from uuid import uuid4

import openpyxl
import pytest
from PySide6.QtWidgets import QApplication
from openpyxl.worksheet.table import Table

from src.desktop_v2.import_wizard import RosterImportWizard
from src.domain.roster import CustomFieldDefinition, CustomFieldType, Person, PersonProfile, RosterSnapshot
from src.importing.contracts import ProfileField, ImportMappingProfile
from src.importing.profile_history import StoredImportProfile
from src.importing.workbook import parse_workbook
from tests.test_import_custom_columns import _prepared
from src.storage.database_session import DatabaseSession


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def source(path, headers=("대상자", "생년월일", "식종", "원본 코드", "수량", "확인일", "분류", "새 메모"),
           rows=(("합성 아동", "20150302", "석식", "001", 2, datetime(2026, 10, 28), "A", "새 내용"),)):
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.append(headers)
    for row in rows:
        sheet.append(row)
    sheet.add_table(Table(displayName="명단", ref=f"A1:{openpyxl.utils.get_column_letter(len(headers))}{len(rows)+1}"))
    book.save(path)
    book.close()
    return path


def roster():
    definitions = tuple(CustomFieldDefinition(uuid4(), name, kind, index, choices) for index, (name, kind, choices) in enumerate((
        ("관리번호", CustomFieldType.TEXT, ()), ("수량", CustomFieldType.NUMBER, ()),
        ("확인일", CustomFieldType.DATE, ()), ("분류", CustomFieldType.CHOICE, ("A", "B")),
    )))
    return RosterSnapshot(uuid4(), (Person(uuid4(), PersonProfile("합성 아동", birth_date=date(2015,3,2),
                          custom_values={definitions[0].definition_id: "기존 코드"})),), custom_fields=definitions)


def wizard_for(path, current, *, profiles=(), connect=True):
    wizard = RosterImportWizard(existing_profiles=profiles)
    wizard.set_database_columns(current, {"name": "대상자", "school": "재학 학교"})
    wizard.path_edit.setText(str(path))
    assert wizard._inspect_source(), wizard.error_label.text()
    assert all(combo.currentData() == "ignore" for combo in wizard.destination_table.combos.values())
    if connect:
        choose(wizard, "A", "profile:name")
        choose(wizard, "B", "profile:birth_date")
        choose(wizard, "C", "meal:combined")
        for column, definition in zip(("E", "F", "G"), current.custom_fields[1:]):
            if column in wizard.destination_table.combos:
                choose(wizard, column, "custom:" + str(definition.definition_id))
    return wizard


def choose(wizard, column, target):
    combo = wizard.destination_table.combos[column]
    assert combo.findData(target) >= 0
    combo.setCurrentIndex(combo.findData(target))


@pytest.mark.parametrize("lunch", (False, True))
def test_existing_import_starts_unmapped_even_with_saved_profile(app, tmp_path, lunch):
    from src.importing.revision import ImportPurpose
    path = source(tmp_path / "manual.xlsx",
                  headers=("성명", "생년월일", "식종", "원본 코드", "수량", "확인일", "분류", "새 메모"))
    initial = RosterImportWizard()
    initial.path_edit.setText(str(path))
    assert initial._inspect_source()
    original = initial._build_profile()
    assert original.profile_columns[ProfileField.NAME] == "A"
    assert original.combined_meals_column == "C"
    stored = StoredImportProfile(original, initial._inspection.header_fingerprint)
    initial.close()
    wizard = RosterImportWizard(existing_profiles=(stored,), allow_vacation_lunch=True)
    wizard.set_database_columns(roster())
    if lunch:
        wizard.purpose_combo.setCurrentIndex(wizard.purpose_combo.findData(ImportPurpose.VACATION_LUNCH))
    wizard.path_edit.setText(str(path))
    try:
        assert wizard._inspect_source()
        assert all(combo.currentData() == "ignore" for combo in wizard.destination_table.combos.values())
        assert wizard.destination_table.pool.tiles.count() == len(wizard._inspection.columns)
        assert bool([key for key in wizard.destination_table.cards if key.startswith("meal:")]) is not lunch
        choose(wizard, "A", "profile:name")
        choose(wizard, "B", "profile:birth_date")
        if not lunch:
            choose(wizard, "C", "meal:combined")
        profile = wizard._build_profile()
        preview = parse_workbook(path, profile, table_name="명단",
                                 cell_overrides={(2, "C"): "식종으로 읽으면 오류"} if lunch else None)
        assert preview.can_continue, preview.issues
        from src.domain.roster import Meal
        assert preview.observations[0].meals == ((Meal.LUNCH,) if lunch else (Meal.DINNER,))
        assert wizard._inspect_source()
        assert all(combo.currentData() == "ignore" for combo in wizard.destination_table.combos.values())
    finally:
        wizard.close()


def test_mapping_uses_db_labels_custom_ids_and_typed_values_without_duplicate_columns(app, tmp_path):
    current = roster()
    path = source(tmp_path / "existing.xlsx")
    wizard = wizard_for(path, current)
    try:
        assert wizard.destination_table.combos["A"].currentText() == "대상자 *"
        assert wizard.destination_table.combos["A"].currentData() == "profile:name"
        choose(wizard, "D", "custom:" + str(current.custom_fields[0].definition_id))
        for column, definition in zip(("E", "F", "G"), current.custom_fields[1:]):
            assert wizard.destination_table.combos[column].currentData() == "custom:" + str(definition.definition_id)
        choose(wizard, "H", "new")
        profile = wizard._build_profile()
        assert profile == wizard._build_profile()
        preview = parse_workbook(path, profile, table_name="명단", existing_custom_fields=current.custom_fields)
        assert preview.can_continue, preview.issues
        prepared = _prepared(preview, current)
        saved = prepared.result.snapshot
        values = saved.persons[0].profile.custom_values
        assert [values[field.definition_id] for field in current.custom_fields] == ["001", 2, date(2026,10,28), "A"]
        assert saved.custom_fields[:4] == current.custom_fields
        assert len(saved.custom_fields) == 5
        assert saved.custom_fields[-1].name == "새 메모"
        assert values[saved.custom_fields[-1].definition_id] == "새 내용"
    finally:
        wizard.close()


def test_saved_connections_are_not_restored_and_current_ids_remain_available(app, tmp_path):
    current = roster()
    path = source(tmp_path / "saved.xlsx")
    wizard = wizard_for(path, current)
    choose(wizard, "D", "custom:" + str(current.custom_fields[0].definition_id))
    profile = wizard._build_profile()
    preview = parse_workbook(path, profile, table_name="명단", existing_custom_fields=current.custom_fields)
    stored = StoredImportProfile(profile, preview.header_fingerprint)
    wizard.close()
    renamed = replace(current, custom_fields=(replace(current.custom_fields[0], name="바뀐 관리번호"), *current.custom_fields[1:]))
    reopened = wizard_for(path, renamed, profiles=(stored,))
    try:
        assert reopened.destination_table.combos["D"].currentData() == "ignore"
        choose(reopened, "D", "custom:" + str(current.custom_fields[0].definition_id))
        assert reopened.destination_table.combos["D"].currentText().startswith("바뀐 관리번호")
        assert reopened._build_profile().custom_field_columns[current.custom_fields[0].definition_id] == "D"
        assert reopened._build_profile().custom_field_names[current.custom_fields[0].definition_id] == "바뀐 관리번호"
    finally:
        reopened.close()
    deleted = replace(current, persons=(), custom_fields=current.custom_fields[1:])
    reopened = wizard_for(path, deleted, profiles=(stored,), connect=False)
    try:
        assert reopened.destination_table.combos["D"].currentData() == "ignore"
        assert reopened.destination_table.combos["D"].findData("custom:" + str(current.custom_fields[0].definition_id)) < 0
        choose(reopened, "A", "profile:name")
        choose(reopened, "C", "meal:combined")
        assert current.custom_fields[0].definition_id not in reopened._build_profile().custom_field_columns
    finally:
        reopened.close()


def test_destination_duplicate_is_rejected_and_unmapping_does_not_restore_it(app, tmp_path):
    current = roster()
    wizard = wizard_for(source(tmp_path / "duplicate.xlsx"), current)
    try:
        choose(wizard, "D", "custom:" + str(current.custom_fields[0].definition_id))
        choose(wizard, "H", "custom:" + str(current.custom_fields[0].definition_id))
        assert wizard.destination_table.combos["H"].currentData() == "ignore"
        assert "이미 D열" in wizard.destination_table.error.text()
        choose(wizard, "D", "ignore")
        choose(wizard, "H", "custom:" + str(current.custom_fields[0].definition_id))
        assert wizard._build_profile().custom_field_columns[current.custom_fields[0].definition_id] == "H"
        choose(wizard, "A", "ignore")
        with pytest.raises(ValueError, match="필수"):
            wizard._build_profile()
        choose(wizard, "A", "profile:name")
        assert wizard._build_profile().profile_columns[ProfileField.NAME] == "A"
    finally:
        wizard.close()


@pytest.mark.parametrize("column,value", (("E", "숫자 아님"), ("F", "날짜 아님"), ("G", "C")))
def test_invalid_custom_value_is_a_correctable_source_cell(app, tmp_path, column, value):
    current = roster()
    raw = ["합성 아동", "20150302", "석식", "001", 2, datetime(2026,10,28), "A", "새 내용"]
    raw[openpyxl.utils.column_index_from_string(column)-1] = value
    path = source(tmp_path / "invalid.xlsx", rows=(tuple(raw),))
    wizard = wizard_for(path, current)
    try:
        profile = wizard._build_profile()
        preview = parse_workbook(path, profile, table_name="명단", existing_custom_fields=current.custom_fields)
        assert not preview.can_continue
        assert any(issue.blocking and issue.row_number == 2 and issue.column == column for issue in preview.issues)
        corrected = {"E": "0", "F": "2026-10-29", "G": "B"}[column]
        preview = parse_workbook(path, profile, table_name="명단", existing_custom_fields=current.custom_fields,
                                 cell_overrides={(2,column): corrected})
        assert preview.can_continue, preview.issues
        saved = _prepared(preview,current).result.snapshot
        definition = current.custom_fields[{"E": 1, "F": 2, "G": 3}[column]]
        assert saved.persons[0].profile.custom_values[definition.definition_id] == {"E":0, "F":date(2026,10,29), "G":"B"}[column]
    finally:
        wizard.close()


def test_same_named_standard_and_custom_column_are_not_arbitrarily_matched(app, tmp_path):
    current = roster()
    current = replace(current, custom_fields=(*current.custom_fields, CustomFieldDefinition(uuid4(),"비고",CustomFieldType.TEXT,4)))
    path = source(tmp_path / "ambiguous.xlsx", headers=("대상자","생년월일","식종","비고"),
                  rows=(("합성 아동","20150302","석식","내용"),))
    wizard = wizard_for(path,current)
    try:
        assert wizard.destination_table.combos["D"].currentData() == "ignore"
        choose(wizard,"D","custom:"+str(current.custom_fields[-1].definition_id))
        assert wizard._build_profile().custom_field_columns[current.custom_fields[-1].definition_id] == "D"
    finally:
        wizard.close()


def test_typed_values_and_ids_survive_one_import_commit_and_reopen(app,tmp_path):
    from datetime import UTC
    from src.domain.projections import roster_audit_changes
    from src.domain.revisions import RevisionActor, RevisionMetadata, RevisionRecord, RevisionSource
    current = roster()
    path = source(tmp_path / "persist.xlsx")
    wizard = wizard_for(path,current)
    choose(wizard,"D","custom:"+str(current.custom_fields[0].definition_id))
    profile = wizard._build_profile()
    preview = parse_workbook(path,profile,table_name="명단",existing_custom_fields=current.custom_fields)
    wizard.close()
    database_path=tmp_path / "operation.sqlite3"
    with DatabaseSession.create(database_path,"synthetic-password") as session:
        session.append_revision(RevisionRecord(RevisionMetadata(current.revision_id,None,datetime.now(UTC),date(2026,10,1),
            RevisionActor("synthetic","synthetic-sid"),"seed",RevisionSource.UI),roster_audit_changes(None,current)))
        prepared=_prepared(preview,current)
        session.append_revision(prepared.record)
    with DatabaseSession.open(database_path,"synthetic-password") as session:
        saved=session.database.current_roster()
        assert saved.custom_fields == current.custom_fields
        assert saved.persons[0].profile.custom_values[current.custom_fields[2].definition_id] == date(2026,10,28)


def test_switching_import_purpose_preserves_manual_mapping_without_meal_targets(app, tmp_path):
    from src.importing.revision import ImportPurpose
    current = roster()
    wizard = RosterImportWizard(allow_vacation_lunch=True)
    wizard.set_database_columns(current, {"name": "대상자"})
    wizard.path_edit.setText(str(source(tmp_path / "modes.xlsx")))
    assert wizard._inspect_source()
    try:
        key = "custom:" + str(current.custom_fields[0].definition_id)
        assert all(combo.currentData() == "ignore" for combo in wizard.destination_table.combos.values())
        choose(wizard, "A", "profile:name")
        choose(wizard, "C", "meal:combined")
        choose(wizard, "D", key)
        assert wizard._build_profile().combined_meals_column == "C"
        assert not wizard.destination_table.sections["meal"].isHidden()
        wizard.purpose_combo.setCurrentIndex(wizard.purpose_combo.findData(ImportPurpose.VACATION_LUNCH))
        combo = wizard.destination_table.combos["C"]
        assert combo.findData("meal:combined") < 0
        assert combo.currentData() == "ignore"
        assert wizard.destination_table.combos["D"].currentData() == key
        assert wizard._build_profile().custom_field_columns[current.custom_fields[0].definition_id] == "D"
        assert wizard._build_profile().default_meals
        assert wizard.destination_table.sections["meal"].isHidden()
        assert wizard.meal_section.isHidden()
        assert wizard.meal_detection_panel.isHidden()
        assert "식종" not in wizard.destination_table.status.text()
        wizard.purpose_combo.setCurrentIndex(wizard.purpose_combo.findData(ImportPurpose.DATABASE))
        combo = wizard.destination_table.combos["C"]
        assert combo.currentData() == "meal:combined"
        assert wizard._build_profile().combined_meals_column == "C"
        assert not wizard.destination_table.sections["meal"].isHidden()
    finally:
        wizard.close()


def test_equal_numeric_and_date_values_merge_without_false_conflict(app, tmp_path):
    current = roster()
    rows = (("합성 아동", "20150302", "조식", "001", 2, "2026-10-28", "A", ""),
            ("합성 아동", "20150302", "석식", "001", "2.0", datetime(2026, 10, 28), "A", ""))
    path = source(tmp_path / "repeated.xlsx", rows=rows)
    wizard = wizard_for(path, current)
    try:
        preview = parse_workbook(path, wizard._build_profile(), table_name="명단", existing_custom_fields=current.custom_fields)
        assert preview.can_continue, preview.issues
        assert len(preview.observations) == 1
        result = _prepared(preview, current).result.snapshot
        assert result.persons[0].profile.custom_values[current.custom_fields[1].definition_id] == 2
    finally:
        wizard.close()


def test_custom_display_alias_is_used_without_changing_definition(app, tmp_path):
    current = roster()
    key = "custom:" + str(current.custom_fields[0].definition_id)
    path = source(tmp_path / "alias.xlsx", headers=("대상자", "생년월일", "식종", "현장 관리번호"),
                  rows=(("합성 아동", "20150302", "석식", "007"),))
    wizard = RosterImportWizard()
    wizard.set_database_columns(current, {"name": "대상자", key: "현장 관리번호"})
    wizard.path_edit.setText(str(path))
    try:
        assert wizard._inspect_source()
        assert wizard.destination_table.combos["D"].currentData() == "ignore"
        choose(wizard, "A", "profile:name")
        choose(wizard, "C", "meal:combined")
        choose(wizard, "D", key)
        assert wizard.destination_table.combos["D"].currentData() == key
        assert wizard.destination_table.combos["D"].currentText().startswith("현장 관리번호")
        assert wizard._build_profile().custom_field_names[current.custom_fields[0].definition_id] == "관리번호"
    finally:
        wizard.close()
