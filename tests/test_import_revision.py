"""Confirmed workbook imports commit once and trigger an automatic backup."""

from datetime import UTC, date, datetime
from pathlib import Path
from uuid import UUID

import openpyxl
import pytest

from src.domain.roster import Meal
from src.domain.revisions import RevisionActor
from src.importing.households import (
    HouseholdMatchingRule,
    household_matching_rules_from_snapshot,
    stage_household_grouping,
)
from src.importing.application import (
    IdentityResolution,
    ReconciliationDecision,
)
from src.importing.contracts import (
    ImportMappingProfile,
    ProfileField,
)
from src.importing.revision import build_import_revision
from src.importing.profile_history import import_profiles_from_snapshot
from src.importing.workbook import parse_workbook
from src.importing.reconciliation import stage_reconciliation
from src.storage.database_session import DatabaseSession
from src.desktop_v2.inspector_history import InspectorMealHistory
from src.desktop_v2.ledger_reader import load_revision_chain
from src.services.database_export import build_database_workbook
from src.services.person_history import build_history_tables


REVISION_ID = UUID("82000000-0000-0000-0000-000000000001")
BATCH_ID = UUID("82000000-0000-0000-0000-000000000002")
PROFILE_ID = UUID("82000000-0000-0000-0000-000000000003")
PERSON_ID = UUID("82000000-0000-0000-0000-000000000004")
HOUSEHOLD_RULE_ID = UUID("82000000-0000-0000-0000-000000000005")
HOUSEHOLD_ID = UUID("82000000-0000-0000-0000-000000000006")
EFFECTIVE_ON = date(2026, 8, 26)


def _source(path: Path) -> Path:
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.title = "기존 명단"
    worksheet.append(("성명", "생년월일", "식종", "핸드폰"))
    worksheet.append(("홍길동", "20150302", "조식 / 중식", "010-1234-5678"))
    worksheet.append((None, "20160403", "석식", "010-9999-9999"))
    workbook.save(path)
    workbook.close()
    return path


def _profile() -> ImportMappingProfile:
    return ImportMappingProfile(
        PROFILE_ID,
        1,
        "기존 Excel 명단",
        "roster-combined-xlsx",
        {
            ProfileField.NAME: "A",
            ProfileField.BIRTH_DATE: "B",
            ProfileField.MOBILE_PHONE: "D",
        },
        combined_meals_column="C",
    )


def _prepared(
    path: Path,
    *,
    ignore: bool = False,
    cell_overrides: dict[tuple[int, str], object] | None = None,
    profile: ImportMappingProfile | None = None,
):
    preview = parse_workbook(
        path,
        _profile() if profile is None else profile,
        cell_overrides=cell_overrides,
    )
    plan = stage_reconciliation(
        None,
        preview.observations,
        effective_on=EFFECTIVE_ON,
    )
    observation_id = preview.observations[0].observation_id
    decision = ReconciliationDecision(
        observation_id,
        IdentityResolution.IGNORE if ignore else IdentityResolution.CREATE_NEW,
        None if ignore else PERSON_ID,
    )
    household_plan = stage_household_grouping(
        preview.observations,
        HouseholdMatchingRule(HOUSEHOLD_RULE_ID, 1),
        effective_on=EFFECTIVE_ON,
        household_id_factory=lambda: HOUSEHOLD_ID,
    )
    return build_import_revision(
        None,
        preview,
        plan,
        decisions={observation_id: decision},
        actor=RevisionActor("tester", "S-1-5-21-test"),
        recorded_at=datetime(2026, 8, 26, 10, 30, tzinfo=UTC),
        revision_id=REVISION_ID,
        batch_id=BATCH_ID,
        household_plan=household_plan,
        household_decisions=household_plan.default_decisions(),
    )


def test_initial_import_cannot_finish_with_every_row_excluded(tmp_path: Path):
    path = _source(tmp_path / "existing.xlsx")

    with pytest.raises(ValueError, match="한 명 이상"):
        _prepared(path, ignore=True)


def test_initial_import_stores_blank_optional_fields_and_no_meal(
    tmp_path: Path,
):
    path = tmp_path / "name-only.xlsx"
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.append(("성명", "식종", "학교명"))
    worksheet.append(("빈값대상자", None, None))
    workbook.save(path)
    workbook.close()
    profile = ImportMappingProfile(
        PROFILE_ID,
        1,
        "빈값 허용 명단",
        "roster-combined-xlsx",
        {
            ProfileField.NAME: "A",
            ProfileField.SCHOOL_NAME: "C",
        },
        combined_meals_column="B",
    )
    preview = parse_workbook(path, profile)
    observation_id = preview.observations[0].observation_id
    plan = stage_reconciliation(
        None,
        preview.observations,
        effective_on=EFFECTIVE_ON,
    )
    household_plan = stage_household_grouping(
        preview.observations,
        HouseholdMatchingRule(HOUSEHOLD_RULE_ID, 1),
        effective_on=EFFECTIVE_ON,
        household_id_factory=lambda: HOUSEHOLD_ID,
    )

    prepared = build_import_revision(
        None,
        preview,
        plan,
        decisions={
            observation_id: ReconciliationDecision(
                observation_id,
                IdentityResolution.CREATE_NEW,
                PERSON_ID,
            )
        },
        actor=RevisionActor("tester", "S-1-5-21-test"),
        recorded_at=datetime(2026, 8, 26, 10, 30, tzinfo=UTC),
        revision_id=REVISION_ID,
        batch_id=BATCH_ID,
        household_plan=household_plan,
        household_decisions=household_plan.default_decisions(),
    )

    person = prepared.result.snapshot.person(PERSON_ID)
    assert person.profile.birth_date is None
    assert person.profile.school_name is None
    assert prepared.result.snapshot.meals_for(PERSON_ID, on=EFFECTIVE_ON) == ()


def test_first_import_profile_roster_and_source_are_one_revision(tmp_path: Path):
    path = _source(tmp_path / "existing.xlsx")
    prepared = _prepared(path)

    assert prepared.record.metadata.parent_revision_id is None
    assert prepared.record.metadata.effective_on == EFFECTIVE_ON
    assert prepared.record.import_batch is not None
    assert prepared.record.import_batch.source_filename == "existing.xlsx"
    assert prepared.record.import_batch.mapping_manifest["profile_id"] == str(
        PROFILE_ID
    )
    assert prepared.record.metadata.decisions["observation_sources"]
    source_notices = prepared.record.metadata.decisions["source_notices"]
    assert len(source_notices) == 1
    assert dict(source_notices[0]) == {
        "row": 3,
        "column": "A",
        "message": "성명이 없어 이 행은 가져오지 않습니다.",
    }
    assert any(change.entity_kind == "import_profile" for change in prepared.record.changes)
    assert any(
        change.entity_kind == "household_matching_rule"
        for change in prepared.record.changes
    )
    assert prepared.record.metadata.rule_versions[
        "household_matching_rule_id"
    ] == str(HOUSEHOLD_RULE_ID)
    assert prepared.record.metadata.rule_versions[
        "profile_update_policy_version"
    ] == 1
    assert prepared.record.metadata.decisions["profile_update_policy"] == {
        "version": 1,
        "mapped_blank_values": "preserve_existing",
    }
    assert prepared.result.snapshot.memberships[0].household_id == HOUSEHOLD_ID
    assert prepared.result.snapshot.meals_for(PERSON_ID, on=EFFECTIVE_ON) == (
        Meal.BREAKFAST,
        Meal.LUNCH,
    )


def test_in_memory_source_correction_location_is_audited(tmp_path: Path):
    path = _source(tmp_path / "corrected.xlsx")

    prepared = _prepared(
        path,
        cell_overrides={(2, "D"): "010-2222-3333"},
    )

    corrections = prepared.record.metadata.decisions["source_corrections"]
    assert tuple(dict(item) for item in corrections) == (
        {"row": 2, "column": "D"},
    )
    person = prepared.result.snapshot.person(PERSON_ID)
    assert person.profile.mobile_phone == "010-2222-3333"


def test_imported_delivery_history_is_stored_in_revision_and_batch(
    tmp_path: Path,
):
    path = tmp_path / "move-history.xlsx"
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.append(("성명", "생년월일", "주소", "식종", "담당", "순서"))
    worksheet.append(("이사아동", "20150302", "이전 주소", "조식", "기사A", "001"))
    worksheet.append(("이사아동", "20150302", "현재 주소", "조식", "기사B", "009"))
    workbook.save(path)
    workbook.close()
    profile = ImportMappingProfile(
        PROFILE_ID,
        1,
        "이사 전후 명단",
        "roster-combined-xlsx",
        {
            ProfileField.NAME: "A",
            ProfileField.BIRTH_DATE: "B",
            ProfileField.ROAD_ADDRESS: "C",
        },
        combined_meals_column="D",
        driver_column="E",
        sequence_column="F",
    )

    prepared = _prepared(path, profile=profile)

    history = prepared.record.metadata.decisions["imported_delivery_history"]
    assert tuple(item["field"] for item in history) == (
        "road_address",
        "driver",
        "sequence",
    )
    road_address = history[0]
    assert road_address["person_id"] == str(PERSON_ID)
    assert road_address["selection_rule"] == "later_excel_row"
    assert road_address["effective_on"] is None
    assert road_address["effective_on_status"] == "unknown_from_source"
    assert tuple(dict(item) for item in road_address["previous"]) == (
        {"row": 2, "value": "이전 주소"},
    )
    assert dict(road_address["current"]) == {
        "row": 3,
        "value": "현재 주소",
    }
    assert prepared.record.import_batch is not None
    assert (
        prepared.record.import_batch.decisions["imported_delivery_history"]
        == history
    )
    person = prepared.result.snapshot.person(PERSON_ID)
    assert person.profile.road_address == "현재 주소"
    assignment = prepared.result.snapshot.assignments[0]
    assert assignment.driver == "기사B"
    assert assignment.sequence == "009"

    database_path = tmp_path / "move-history.sqlite3"
    backup_directory = tmp_path / "move-history-backups"
    backup_directory.mkdir()
    with DatabaseSession.create(
        database_path,
        "user-password",
        "institution-recovery",
        backup_directory=backup_directory,
    ) as session:
        assert session.append_revision(prepared.record).succeeded
        stored = session.database.revision(REVISION_ID)
        assert stored.metadata.decisions["imported_delivery_history"] == history
        assert stored.import_batch is not None
        assert stored.import_batch.decisions["imported_delivery_history"] == history


def test_committed_initial_import_is_automatically_backed_up_and_restorable(
    tmp_path: Path,
):
    source = _source(tmp_path / "existing.xlsx")
    prepared = _prepared(source)
    database_path = tmp_path / "operation.sqlite3"
    backup_directory = tmp_path / "backups"
    backup_directory.mkdir()

    with DatabaseSession.create(
        database_path,
        "user-password",
        "institution-recovery",
        backup_directory=backup_directory,
    ) as session:
        initial_backup = session.backup_now()
        result = session.append_revision(prepared.record)

        assert result.succeeded
        assert result.record is not None
        assert result.record.path == initial_backup.path
        assert initial_backup.latest_revision_id is None
        assert result.record.latest_revision_id == REVISION_ID
        assert session.database.current_roster().person(PERSON_ID).profile.name == (
            "홍길동"
        )
        snapshot = session.database.snapshot(REVISION_ID)
        profiles = import_profiles_from_snapshot(snapshot)
        assert tuple(item.profile.name for item in profiles) == ("기존 Excel 명단",)
        assert household_matching_rules_from_snapshot(snapshot)[0].rule_id == (
            HOUSEHOLD_RULE_ID
        )
        committed_backup = result.record.path

    restored_path = tmp_path / "restored.sqlite3"
    with DatabaseSession.restore_and_open_with_user_password(
        committed_backup,
        restored_path,
        "user-password",
    ) as restored:
        roster = restored.database.current_roster()
        assert roster is not None
        assert roster.person(PERSON_ID).profile.name == "홍길동"
        assert roster.memberships[0].household_id == HOUSEHOLD_ID
        assert import_profiles_from_snapshot(
            restored.database.snapshot(REVISION_ID)
        )[0].profile.profile_id == PROFILE_ID
        assert household_matching_rules_from_snapshot(
            restored.database.snapshot(REVISION_ID)
        )[0].rule_id == HOUSEHOLD_RULE_ID


def test_persisted_excel_import_is_absent_from_display_history(tmp_path: Path):
    prepared = _prepared(_source(tmp_path / "existing.xlsx"))
    database_path = tmp_path / "history.sqlite3"
    backup_directory = tmp_path / "backups"
    backup_directory.mkdir()
    with DatabaseSession.create(
        database_path, "user-password", "institution-recovery",
        backup_directory=backup_directory,
    ) as session:
        assert session.append_revision(prepared.record).succeeded

    with DatabaseSession.open(database_path, "user-password") as session:
        roster = session.database.current_roster()
        assert roster.person(PERSON_ID).profile.name == "홍길동"
        records = load_revision_chain(session.database, roster.revision_id)
        assert len(records) == 1 and records[0].import_batch is not None
        snapshot = session.database.snapshot(roster.revision_id)
        tables = build_history_tables(roster, snapshot, records, person_id=PERSON_ID)
        assert not tables["정보 변경"][1]
        assert not tables["식종 변경"][1]
        assert InspectorMealHistory(session.database).rows_for(roster, PERSON_ID) == ()
        from PySide6.QtWidgets import QApplication, QTableView
        from src.desktop_v2.person_history import create_person_history_dialog

        app = QApplication.instance() or QApplication([])
        dialog = create_person_history_dialog(session, roster.person(PERSON_ID), None)
        app.processEvents()
        assert dialog.findChild(QTableView, "history_정보 변경").model().rowCount() == 0
        assert dialog.findChild(QTableView, "history_식종 변경").model().rowCount() == 0
        dialog.close()
        book = build_database_workbook(roster, snapshot, records, day=EFFECTIVE_ON, sections=("history",))
        for name in ("정보 변경 이력", "식종 변경 이력", "변경 이력"):
            assert "Excel 명단 가져오기" not in str(list(book[name].values))
        book.close()
