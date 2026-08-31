"""Confirmed workbook imports commit once and trigger an automatic backup."""

from datetime import UTC, date, datetime
from pathlib import Path
from uuid import UUID

import openpyxl
import pytest

from src.domain import Meal, RevisionActor
from src.importing import (
    HouseholdMatchingRule,
    IdentityResolution,
    ImportMappingProfile,
    ProfileField,
    ReconciliationDecision,
    build_import_revision,
    household_matching_rules_from_snapshot,
    import_profiles_from_snapshot,
    parse_workbook,
    stage_reconciliation,
    stage_household_grouping,
)
from src.storage import DatabaseSession


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


def _prepared(path: Path, *, ignore: bool = False):
    preview = parse_workbook(path, _profile())
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
        assert result.record.path != initial_backup.path
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
