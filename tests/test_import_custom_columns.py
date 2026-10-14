"""Selected additional Excel columns persist through the canonical import path."""

from dataclasses import replace
from datetime import UTC, date, datetime
from uuid import uuid4

import openpyxl
import pytest
from PySide6.QtCore import Qt

from src.desktop_v2.roster_window import RosterTableModel
from src.domain.revisions import RevisionActor
from src.domain.roster import CustomFieldDefinition, CustomFieldType, Person, PersonProfile, RosterSnapshot
from src.importing.application import (
    IdentityResolution,
    ReconciliationDecision,
    apply_reconciliation,
)
from src.importing.contracts import (
    ImportMappingProfile,
    ProfileField,
)
from src.importing.revision import build_import_revision
from src.importing.profile_history import import_profiles_from_snapshot
from src.importing.workbook import parse_workbook
from src.importing.reconciliation import stage_reconciliation
from src.importing.workbook import SourceColumn, WorkbookStructureError, custom_column_names, source_column_letter
from src.storage.database_session import DatabaseSession


DAY = date(2026, 10, 13)


def _source(tmp_path, *, custom_value="001"):
    path = tmp_path / "additional.xlsx"
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(("성명", "생년월일", "식종", "관리코드", "빈 항목", "제외 항목"))
    sheet.append(("합성 아동", "20150302", "석식", custom_value, None, "제외값"))
    workbook.save(path)
    workbook.close()
    ids = (uuid4(), uuid4())
    profile = ImportMappingProfile(
        uuid4(), 1, "추가 항목", "roster-combined-xlsx",
        {ProfileField.NAME: "A", ProfileField.BIRTH_DATE: "B"},
        combined_meals_column="C", custom_field_columns=dict(zip(ids, ("D", "E"))),
        ignored_columns=("F",),
    )
    return path, profile, ids


def _prepared(preview, current=None):
    plan = stage_reconciliation(current, preview.observations, effective_on=DAY)
    decisions = {
        item.observation.observation_id: ReconciliationDecision(
            item.observation.observation_id, IdentityResolution.CREATE_NEW, uuid4()
        )
        for item in plan.staged_people if item.matched_person_id is None
    }
    return build_import_revision(
        current, preview, plan, decisions=decisions,
        actor=RevisionActor("synthetic tester", "synthetic-sid"), recorded_at=datetime.now(UTC),
        revision_id=uuid4(), batch_id=uuid4(),
    )


def test_additional_values_and_blank_definition_survive_encrypted_reopen(tmp_path):
    path, profile, (code_id, blank_id) = _source(tmp_path)
    preview = parse_workbook(path, profile)
    assert preview.can_continue
    assert preview.observations[0].custom_values == {code_id: "001", blank_id: None}
    assert [item.name for item in preview.custom_fields] == ["관리코드", "빈 항목"]
    prepared = _prepared(preview)
    database_path = tmp_path / "roster.sqlite3"
    backups = tmp_path / "backups"
    backups.mkdir()
    with DatabaseSession.create(database_path, "test-password", backup_directory=backups) as session:
        assert session.append_revision(prepared.record).succeeded
    with DatabaseSession.open(database_path, "test-password", backup_directory=backups) as session:
        roster = session.database.current_roster()
        assert roster.persons[0].profile.custom_values == {code_id: "001", blank_id: None}
        assert [item.name for item in roster.custom_fields] == ["관리코드", "빈 항목"]
        stored = import_profiles_from_snapshot(session.database.snapshot(roster.revision_id))[0]
        assert stored.profile == profile
        model = RosterTableModel(roster)
        custom_columns = {column.key: index for index, column in enumerate(model.columns) if column.custom_field}
        assert set(custom_columns) == {f"custom:{code_id}", f"custom:{blank_id}"}
        assert model.data(model.index(0, custom_columns[f"custom:{code_id}"]), Qt.ItemDataRole.DisplayRole) == "001"
        # Reusing the saved profile does not create additional definitions.
        updated = _prepared(parse_workbook(path, stored.profile), roster).result.snapshot
        assert updated.custom_fields == roster.custom_fields


@pytest.mark.parametrize(("value", "expected"), [(None, "previous"), ("new value", "new value")])
def test_mapped_values_follow_preserve_blank_policy_and_keep_unrelated_values(tmp_path, value, expected):
    path, profile, (code_id, blank_id) = _source(tmp_path, custom_value=value)
    unrelated_id = uuid4()
    current = RosterSnapshot(uuid4(), (Person(uuid4(), PersonProfile(
        "합성 아동", birth_date=date(2015, 3, 2),
        custom_values={code_id: "previous", unrelated_id: "unrelated"},
    )),), custom_fields=(
        CustomFieldDefinition(code_id, "관리코드", CustomFieldType.TEXT, 0),
        CustomFieldDefinition(unrelated_id, "기존 항목", CustomFieldType.TEXT, 1),
    ))
    preview = parse_workbook(path, profile)
    updated = _prepared(preview, current).result.snapshot
    assert updated.persons[0].profile.custom_values == {code_id: expected, unrelated_id: "unrelated"}
    assert {item.definition_id for item in updated.custom_fields} == {code_id, blank_id, unrelated_id}


def test_grouped_meal_rows_preserve_distinct_custom_text(tmp_path):
    path, profile, (code_id, blank_id) = _source(tmp_path)
    workbook = openpyxl.load_workbook(path)
    workbook.active.append(("합성 아동", "20150302", "조식", "002", None, "제외"))
    workbook.active.append(("합성 아동", "20150302", "중식", "001", None, "제외"))
    workbook.save(path)
    workbook.close()
    preview = parse_workbook(path, profile)
    assert preview.can_continue
    assert len(preview.observations) == 1
    assert preview.observations[0].custom_values == {code_id: "001 / 002", blank_id: None}
    assert _prepared(preview).result.snapshot.persons[0].profile.custom_values[code_id] == "001 / 002"


@pytest.mark.parametrize(("value", "expected"), [(True, "TRUE"), (date(2026, 10, 13), "2026-10-13 00:00:00")])
def test_custom_cells_use_text_display_conversion(tmp_path, value, expected):
    path, profile, (code_id, _) = _source(tmp_path, custom_value=value)
    assert parse_workbook(path, profile).observations[0].custom_values[code_id] == expected


def test_formula_in_selected_custom_column_blocks_import(tmp_path):
    path, profile, _ = _source(tmp_path, custom_value="=1+1")
    preview = parse_workbook(path, profile)
    assert not preview.can_continue
    assert any(issue.column == "D" and "수식" in issue.message for issue in preview.blocking_issues)
    # Explicit exclusion does not read or retain that cell.
    excluded = replace(profile, custom_field_columns={}, ignored_columns=("D", "E", "F"))
    safe = parse_workbook(path, excluded)
    assert safe.can_continue
    assert not safe.observations[0].custom_values
    assert not safe.custom_fields


def test_duplicate_blank_and_colliding_header_names_stay_distinct():
    columns = tuple(SourceColumn(index, letter, header) for index, (letter, header) in enumerate(
        (("A", "성명"), ("B", "항목"), ("C", "항목"), ("D", ""), ("E", "항목 (B열)")), start=1
    ))
    names = custom_column_names(columns)
    assert names == custom_column_names(columns)
    assert len(set(names.values())) == len(columns)
    assert names["E"] == "항목 (B열)"
    assert "B열" in names["B"] and "C열" in names["C"] and "D열" in names["D"]
    with pytest.raises(WorkbookStructureError, match="같은 헤더"):
        source_column_letter("항목", columns)


def test_duplicate_and_blank_headers_keep_separate_workbook_values(tmp_path):
    path, profile, (first_id, second_id) = _source(tmp_path)
    blank_id = uuid4()
    workbook = openpyxl.load_workbook(path)
    worksheet = workbook.active
    worksheet["D1"], worksheet["E1"], worksheet["F1"] = "동일 항목", None, "동일 항목"
    worksheet["D2"], worksheet["E2"], worksheet["F2"] = "first", "unnamed", "second"
    # A trailing header keeps the intentionally blank header inside discovery.
    workbook.save(path)
    workbook.close()
    profile = replace(profile, custom_field_columns={first_id: "D", blank_id: "E", second_id: "F"}, ignored_columns=())
    preview = parse_workbook(path, profile)
    roster = _prepared(preview).result.snapshot
    assert {item.name for item in roster.custom_fields} == {"동일 항목 (D열)", "추가 항목 (E열)", "동일 항목 (F열)"}
    assert roster.persons[0].profile.custom_values == {first_id: "first", blank_id: "unnamed", second_id: "second"}


def test_existing_same_name_does_not_conflate_different_custom_ids(tmp_path):
    path, profile, (code_id, _) = _source(tmp_path)
    unrelated_id = uuid4()
    current = RosterSnapshot(uuid4(), (), custom_fields=(
        CustomFieldDefinition(unrelated_id, "관리코드", CustomFieldType.TEXT, 0),
    ))
    updated = _prepared(parse_workbook(path, profile), current).result.snapshot
    assert {item.definition_id: item.name for item in updated.custom_fields}[code_id] == "관리코드 (2)"
    assert updated.persons[0].profile.custom_values[code_id] == "001"


def test_existing_incompatible_custom_type_fails_before_mutation(tmp_path):
    path, profile, (code_id, _) = _source(tmp_path)
    current = RosterSnapshot(uuid4(), (), custom_fields=(
        CustomFieldDefinition(code_id, "관리코드", CustomFieldType.NUMBER, 0),
    ))
    preview = parse_workbook(path, profile)
    plan = stage_reconciliation(current, preview.observations, effective_on=DAY)
    with pytest.raises(ValueError, match="필드 형식"):
        apply_reconciliation(current, plan, new_revision_id=uuid4(), custom_fields=preview.custom_fields)
    assert current.custom_fields[0].field_type is CustomFieldType.NUMBER


def test_custom_name_manifest_is_optional_and_round_trips(tmp_path):
    _, profile, (code_id, _) = _source(tmp_path)
    assert "custom_field_names" not in profile.manifest()
    assert ImportMappingProfile.from_manifest(profile.manifest()) == profile
    named = replace(profile, custom_field_names={code_id: "관리코드"})
    assert ImportMappingProfile.from_manifest(named.manifest()) == named


@pytest.mark.parametrize(("field", "header", "primary", "other"), [
    (ProfileField.TELEPHONE, "전화번호", "031-111-2222", "031-333-4444"),
    (ProfileField.MOBILE_PHONE, "핸드폰", "010-1111-2222", "010-3333-4444"),
])
@pytest.mark.parametrize("grouped", [False, True])
@pytest.mark.parametrize("ordinal", [2, 3])
def test_selected_numbered_phone_column_is_never_consumed_by_phone_merging(tmp_path, field, header, primary, other, grouped, ordinal):
    path = tmp_path / "phone-columns.xlsx"
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.append(("성명", "생년월일", "식종", header, f"{header}{ordinal}"))
    worksheet.append(("합성 아동", "20150302", "석식", primary, primary))
    if grouped:
        worksheet.append(("합성 아동", "20150302", "조식", other, "별도 원문"))
    workbook.save(path)
    workbook.close()
    selected_id = uuid4()
    profile = ImportMappingProfile(
        uuid4(), 1, "추가 연락처", "roster-combined-xlsx",
        {ProfileField.NAME: "A", ProfileField.BIRTH_DATE: "B", field: "D"},
        combined_meals_column="C", custom_field_columns={selected_id: "E"},
    )
    preview = parse_workbook(path, profile)
    assert preview.can_continue
    expected = f"{primary} / 별도 원문" if grouped else primary
    prepared = _prepared(preview)
    database_path = tmp_path / "phone-columns.sqlite3"
    backups = tmp_path / "backups"
    backups.mkdir()
    with DatabaseSession.create(database_path, "test-password", backup_directory=backups) as session:
        assert session.append_revision(prepared.record).succeeded
    with DatabaseSession.open(database_path, "test-password", backup_directory=backups) as session:
        first = session.database.current_roster()
    assert first.persons[0].profile.custom_values[selected_id] == expected
    if grouped:
        auto_ordinal = 3 if ordinal == 2 else 2
        auto_field = next(item for item in first.custom_fields if item.name == f"{header}{auto_ordinal}")
        assert first.persons[0].profile.custom_values[auto_field.definition_id] == primary
    # The saved profile reuses the selected field, even when auto phone slots exist.
    selected_again = _prepared(preview, first).result.snapshot
    assert selected_again.persons[0].profile.custom_values[selected_id] == expected
    # A later import omitting that source column still preserves its prior value.
    omitted = replace(profile, custom_field_columns={}, ignored_columns=("E",))
    later = _prepared(parse_workbook(path, omitted), first).result.snapshot
    assert later.persons[0].profile.custom_values[selected_id] == expected
