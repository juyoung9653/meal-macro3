"""Password session, automatic backup, and restoration integration tests."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import UUID
import zipfile
import hashlib
import json
import tempfile

import pytest

apsw = pytest.importorskip(
    "apsw",
    reason="install requirements-sqlite-v2.txt for encrypted DB integration tests",
)

from src.domain.revisions import (
    AuditChange,
    ChangeOperation,
    RevisionActor,
    RevisionMetadata,
    RevisionRecord,
    RevisionSource,
)
from src.domain.documents import (
    DocumentKind,
    DocumentRun,
)
from src.storage.database_backup import (
    BACKUP_EXTENSION,
    BackupFormatError,
    BackupRestoreConflictError,
    DEFAULT_MAX_BACKUPS,
    DatabaseBackupService,
    inspect_backup,
)
from src.storage.database_credentials import CredentialUnlockError
from src.storage.database_session import DatabaseSession
from src.storage.database_backup import backup_has_recovery_password
import src.storage.database_backup as backup_module


REVISION_ID = UUID("70000000-0000-0000-0000-000000000001")
ENTITY_ID = UUID("70000000-0000-0000-0000-000000000002")
DOCUMENT_RUN_ID = UUID("70000000-0000-0000-0000-000000000003")


def _legacy_backup(session, directory: Path):
    """Build the shipped v1 archive independently of the current writer."""
    with tempfile.TemporaryDirectory() as folder:
        database_file = Path(folder) / "database.sqlite3"
        session.database.backup_to(database_file, session._key_bytes())
        payload = database_file.read_bytes()
    credentials = session.credentials.to_bytes()
    latest = session.database.latest_revision()
    manifest = {
        "format": "meal-delivery-v2-backup", "version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "source_database_name": session.path.name,
        "database_id": str(session.credentials.database_id),
        "schema_version": session.database.schema_version,
        "latest_revision_id": str(latest.metadata.revision_id) if latest else None,
        "database": {"member": "database.sqlite3", "size": len(payload),
                     "sha256": hashlib.sha256(payload).hexdigest()},
        "credentials": {"member": "credentials.json", "size": len(credentials),
                        "sha256": hashlib.sha256(credentials).hexdigest()},
    }
    path = directory / "legacy.mealbackup"
    with zipfile.ZipFile(path, "x", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("database.sqlite3", payload)
        archive.writestr("credentials.json", credentials)
        archive.writestr("manifest.json", json.dumps(manifest).encode())
    return inspect_backup(path)


def _record() -> RevisionRecord:
    return RevisionRecord(
        RevisionMetadata(
            revision_id=REVISION_ID,
            parent_revision_id=None,
            recorded_at=datetime(2026, 8, 24, 18, 0, tzinfo=UTC),
            effective_on=date(2026, 8, 24),
            actor=RevisionActor("tester", "S-1-5-21-test"),
            reason="자동 백업 테스트",
            source=RevisionSource.UI,
        ),
        (
            AuditChange(
                entity_kind="backup-probe",
                entity_id=ENTITY_ID,
                operation=ChangeOperation.INSERT,
                before=None,
                after={"value": "saved"},
            ),
        ),
    )


def _document_run() -> DocumentRun:
    return DocumentRun(
        run_id=DOCUMENT_RUN_ID,
        kind=DocumentKind.CONFIRMATION,
        source_revision_id=REVISION_ID,
        source_revision_display_code="20260825-001",
        period_start=date(2026, 8, 1),
        period_end=date(2026, 8, 31),
        generated_at=datetime(2026, 8, 25, 2, 0, tzinfo=UTC),
        actor=RevisionActor("tester", "S-1-5-21-test"),
        calculation_manifest={"entitlement_policy_version": 1},
        template_manifest={"filename": "확인서.xlsx", "sha256": "b" * 64},
        output_filename="확인서_202608.xlsx",
        output_sha256="c" * 64,
        output_byte_size=2048,
    )


def test_single_password_create_change_and_restore(tmp_path: Path):
    source = tmp_path / "single.sqlite3"
    backups = tmp_path / "backups"
    backups.mkdir()
    with DatabaseSession.create(source, "only-password", backup_directory=backups) as session:
        assert session.credentials.recovery_slot is None
        session.change_user_password("only-password", "new-password")
        backup = session.backup_now()
    assert not backup_has_recovery_password(backup.path)
    with pytest.raises(CredentialUnlockError):
        DatabaseSession.open(source, "only-password")
    with pytest.raises(CredentialUnlockError):
        DatabaseSession.restore_and_open_with_user_password(
            backup.path, tmp_path / "wrong.sqlite3", "only-password"
        )
    destination = tmp_path / "restored.sqlite3"
    with DatabaseSession.restore_and_open_with_user_password(
        backup.path, destination, "new-password"
    ) as restored:
        restored.database.check_integrity()
        assert restored.credentials.recovery_slot is None
    with pytest.raises(CredentialUnlockError):
        DatabaseSession.open(destination, "only-password")


def test_password_rotation_preserves_historical_snapshots_and_legacy_recovery(tmp_path: Path):
    backups = tmp_path / "backups"
    backups.mkdir()
    with DatabaseSession.create(tmp_path / "source.sqlite3", "first", "recovery", backup_directory=backups) as session:
        oldest = session.backup_now()
        session.append_revision(_record())
        middle = session.change_user_password("first", "second")
        assert middle.rotation and len(middle.rotation.updated) == 2
        before = {record.path: inspect_backup(record.path) for record in (oldest, middle.record)}
        newest = session.change_user_password("second", "third")
        assert newest.rotation and len(newest.rotation.updated) == 3
        assert not newest.rotation.failed and not newest.rotation.unreadable
        for index, (path, original) in enumerate(before.items()):
            after = inspect_backup(path)
            assert after.database_sha256 == original.database_sha256
            assert after.database_size == original.database_size
            assert after.latest_revision_id == original.latest_revision_id
            assert after.created_at == original.created_at
            assert backup_has_recovery_password(path)
            with zipfile.ZipFile(path) as archive:
                assert hashlib.sha256(archive.read("database.sqlite3")).hexdigest() == original.database_sha256
            with pytest.raises(CredentialUnlockError):
                DatabaseSession.restore_and_open_with_user_password(path, tmp_path / f"old-{index}.sqlite3", "first")
            with DatabaseSession.restore_and_open_with_user_password(path, tmp_path / f"new-{index}.sqlite3", "third") as restored:
                assert (restored.database.latest_revision() is None) == (original.latest_revision_id is None)


def test_password_rotation_excludes_other_database_and_reports_unreadable(tmp_path: Path):
    backups = tmp_path / "backups"
    backups.mkdir()
    with DatabaseSession.create(tmp_path / "other.sqlite3", "other", backup_directory=backups) as other:
        foreign = other.backup_now().path
    foreign_bytes = foreign.read_bytes()
    broken = backups / "broken.mealbackup"
    broken.write_bytes(b"broken archive")
    with DatabaseSession.create(tmp_path / "source.sqlite3", "first", backup_directory=backups) as session:
        result = session.change_user_password("first", "second")
        assert result.rotation and result.rotation.unreadable == (broken,)
        assert result.rotation.failed == ()
    assert broken.read_bytes() == b"broken archive"
    assert foreign.read_bytes() == foreign_bytes


def test_password_rotation_continues_after_corrupt_credentials(tmp_path: Path):
    backups = tmp_path / "backups"
    backups.mkdir()
    with DatabaseSession.create(tmp_path / "source.sqlite3", "first", backup_directory=backups) as session:
        corrupt = session.backup_now().path
        valid = session.backup_now().path
        with zipfile.ZipFile(corrupt) as archive:
            database_payload = archive.read("database.sqlite3")
            manifest = json.loads(archive.read("manifest.json"))
        bad_credentials = b"not json"
        manifest["credentials"]["size"] = len(bad_credentials)
        manifest["credentials"]["sha256"] = hashlib.sha256(bad_credentials).hexdigest()
        with zipfile.ZipFile(corrupt, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr("database.sqlite3", database_payload)
            archive.writestr("credentials.json", bad_credentials)
            archive.writestr("manifest.json", json.dumps(manifest).encode())
        original = corrupt.read_bytes()
        result = session.change_user_password("first", "second")
        assert result.rotation and result.rotation.unreadable == (corrupt,)
        assert valid in result.rotation.updated
        assert corrupt.read_bytes() == original


def test_older_schema_backup_rotates_without_migrating_archive_and_restores(tmp_path: Path):
    backups = tmp_path / "backups"
    backups.mkdir()
    with DatabaseSession.create(tmp_path / "source.sqlite3", "first", backup_directory=backups) as session:
        archive_path = _legacy_backup(session, backups).path
        with zipfile.ZipFile(archive_path) as archive:
            database_payload = archive.read("database.sqlite3")
            credential_payload = archive.read("credentials.json")
            manifest = json.loads(archive.read("manifest.json"))
        legacy_file = tmp_path / "legacy.sqlite3"
        legacy_file.write_bytes(database_payload)
        connection = apsw.Connection(str(legacy_file))
        try:
            connection.pragma("cipher", "chacha20")
            connection.pragma("hexkey", session.credentials.unlock_user("first").hex())
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("ALTER TABLE household_projection DROP COLUMN delivery_settings_json")
            connection.execute("ALTER TABLE person_projection DROP COLUMN delivery_settings_json")
            connection.execute("DROP TRIGGER schema_migration_no_delete")
            connection.execute("DELETE FROM schema_migration WHERE version >= 6")
            connection.execute("""CREATE TRIGGER schema_migration_no_delete
                BEFORE DELETE ON schema_migration BEGIN
                SELECT RAISE(ABORT, 'schema migration rows are immutable'); END""")
            connection.pragma("user_version", 5)
            connection.execute("COMMIT")
        finally:
            connection.close()
        database_payload = legacy_file.read_bytes()
        manifest["schema_version"] = 5
        manifest["database"]["size"] = len(database_payload)
        manifest["database"]["sha256"] = hashlib.sha256(database_payload).hexdigest()
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr("database.sqlite3", database_payload)
            archive.writestr("credentials.json", credential_payload)
            archive.writestr("manifest.json", json.dumps(manifest).encode())
        original_hash = inspect_backup(archive_path).database_sha256
        result = session.change_user_password("first", "second")
        assert result.rotation and archive_path in result.rotation.updated
        rotated = inspect_backup(archive_path)
        assert rotated.schema_version == 5
        assert rotated.database_sha256 == original_hash
        with DatabaseSession.restore_and_open_with_user_password(
            archive_path, tmp_path / "restored.sqlite3", "second"
        ) as restored:
            assert restored.database.schema_version == 8
            restored.database.check_integrity()
        assert hashlib.sha256((tmp_path / "restored.sqlite3").read_bytes()).hexdigest() != original_hash


def test_password_rotation_replace_failure_keeps_original_and_reports_partial(tmp_path: Path, monkeypatch):
    backups = tmp_path / "backups"
    backups.mkdir()
    with DatabaseSession.create(tmp_path / "source.sqlite3", "first", backup_directory=backups) as session:
        first = session.backup_now().path
        second = session.backup_now().path
        original = second.read_bytes()
        actual_replace = backup_module.os.replace
        def fail_second(source, destination):
            if Path(destination) == second:
                raise OSError("replacement interrupted")
            return actual_replace(source, destination)
        monkeypatch.setattr(backup_module.os, "replace", fail_second)
        result = session.change_user_password("first", "second")
        assert result.rotation and len(result.rotation.updated) == 1
        assert len(result.rotation.failed) == 1
        assert result.rotation.failed[0][0] == second
        assert result.record is not None
        assert second.read_bytes() == original
        with DatabaseSession.restore_and_open_with_user_password(first, tmp_path / "first-new.sqlite3", "second"):
            pass
        with DatabaseSession.restore_and_open_with_user_password(second, tmp_path / "second-old.sqlite3", "first"):
            pass


def test_user_password_opens_db_and_recovery_password_resets_it(tmp_path: Path):
    database_path = tmp_path / "operations.sqlite3"
    with DatabaseSession.create(
        database_path,
        "everyday-password",
        "institution-recovery",
    ) as session:
        session.database.check_integrity()

    with DatabaseSession.open(database_path, "everyday-password") as session:
        assert session.path == database_path.resolve()

    with DatabaseSession.recover_and_open(
        database_path,
        "institution-recovery",
        "new-everyday-password",
    ) as recovered:
        recovered.database.check_integrity()

    with pytest.raises(CredentialUnlockError):
        DatabaseSession.open(database_path, "everyday-password")
    DatabaseSession.open(database_path, "new-everyday-password").close()


def test_successful_revision_creates_one_verified_single_file_backup(tmp_path: Path):
    database_path = tmp_path / "operations.sqlite3"
    backup_directory = tmp_path / "automatic-backups"
    backup_directory.mkdir()

    with DatabaseSession.create(
        database_path,
        "everyday-password",
        "institution-recovery",
        backup_directory=backup_directory,
    ) as session:
        result = session.append_revision(_record())

    assert result.succeeded
    assert result.error is None
    assert result.record is not None
    assert result.record.path.suffix == BACKUP_EXTENSION
    assert result.record.latest_revision_id == REVISION_ID
    assert tuple(backup_directory.iterdir()) == (result.record.path,)
    assert inspect_backup(result.record.path) == result.record

    with zipfile.ZipFile(result.record.path) as archive:
        assert set(archive.namelist()) == {
            "database.sqlite3",
            "credentials.json",
            "manifest.json",
        }
        encrypted_header = archive.read("database.sqlite3")[:16]
    assert encrypted_header != b"SQLite format 3\x00"


def test_backup_restores_exact_revision_with_everyday_password(tmp_path: Path):
    source = tmp_path / "operations.sqlite3"
    backup_directory = tmp_path / "automatic-backups"
    backup_directory.mkdir()
    with DatabaseSession.create(
        source,
        "everyday-password",
        "institution-recovery",
        backup_directory=backup_directory,
    ) as session:
        backup = session.append_revision(_record()).record
    assert backup is not None

    restored_path = tmp_path / "restored.sqlite3"
    with DatabaseSession.restore_and_open_with_user_password(
        backup.path,
        restored_path,
        "everyday-password",
    ) as restored:
        revision = restored.database.latest_revision()
        assert revision is not None
        assert revision.metadata.revision_id == REVISION_ID
        assert revision.metadata.display_code == "20260825-001"
        restored.database.check_integrity()


def test_backup_restores_household_delivery_settings(tmp_path: Path):
    from src.domain.roster import (
        Household,
        HouseholdMembership,
        Meal,
        Person,
        PersonProfile,
        RosterSnapshot,
    )
    from src.domain.projections import roster_audit_changes
    from src.domain.roster import HouseholdDeliverySetting

    source = tmp_path / "settings.sqlite3"
    backup_directory = tmp_path / "backups"
    backup_directory.mkdir()
    personal_setting = HouseholdDeliverySetting(Meal.DINNER, (0, 1, 2, 3, 4), "개인 도시락")
    person = Person(ENTITY_ID, PersonProfile("합성 대상자"), (personal_setting,))
    household = Household(UUID("70000000-0000-0000-0000-000000000004"), (
        HouseholdDeliverySetting(Meal.DINNER, (0, 1, 2, 3, 4), "직접 입력 형태", (4, 1, 2, 3, 4, 5, 6)),
    ))
    roster = RosterSnapshot(REVISION_ID, persons=(person,), households=(household,),
                            memberships=(HouseholdMembership(person.person_id, household.household_id),))
    revision = RevisionRecord(
        RevisionMetadata(revision_id=REVISION_ID, parent_revision_id=None,
                         recorded_at=datetime(2026, 8, 24, 18, 0, tzinfo=UTC),
                         effective_on=date(2026, 8, 24), actor=RevisionActor("tester", "S-1-5-21-test"),
                         reason="가구 배송 설정", source=RevisionSource.UI),
        roster_audit_changes(None, roster),
    )
    with DatabaseSession.create(source, "password", backup_directory=backup_directory) as session:
        backup = session.append_revision(revision).record
    assert backup is not None
    with DatabaseSession.restore_and_open_with_user_password(
        backup.path, tmp_path / "restored.sqlite3", "password",
    ) as restored:
        assert restored.database.current_roster().households[0].delivery_settings == household.delivery_settings
        assert restored.database.current_roster().person(person.person_id).delivery_settings == (personal_setting,)


def test_document_run_creates_backup_and_restores_append_only_history(tmp_path: Path):
    source = tmp_path / "operations.sqlite3"
    backup_directory = tmp_path / "automatic-backups"
    backup_directory.mkdir()
    with DatabaseSession.create(
        source,
        "everyday-password",
        "institution-recovery",
        backup_directory=backup_directory,
    ) as session:
        session.append_revision(_record())
        result = session.append_document_run(_document_run())
    assert result.record is not None

    restored_path = tmp_path / "restored-document-history.sqlite3"
    with DatabaseSession.restore_and_open_with_user_password(
        result.record.path,
        restored_path,
        "everyday-password",
    ) as restored:
        assert restored.database.document_run(DOCUMENT_RUN_ID) == _document_run()
        assert restored.database.latest_revision().metadata.revision_id == REVISION_ID
        restored.database.check_integrity()


def test_backup_restores_with_recovery_and_sets_new_everyday_password(
    tmp_path: Path,
):
    source = tmp_path / "operations.sqlite3"
    backup_directory = tmp_path / "automatic-backups"
    backup_directory.mkdir()
    with DatabaseSession.create(
        source,
        "forgotten-password",
        "institution-recovery",
        backup_directory=backup_directory,
    ) as session:
        backup = session.append_revision(_record()).record
    assert backup is not None

    restored_path = tmp_path / "restored.sqlite3"
    with DatabaseSession.restore_and_open_with_recovery_password(
        backup.path,
        restored_path,
        "institution-recovery",
        "new-everyday-password",
    ) as restored:
        assert restored.database.latest_revision().metadata.revision_id == REVISION_ID

    with pytest.raises(CredentialUnlockError):
        DatabaseSession.open(restored_path, "forgotten-password")
    DatabaseSession.open(restored_path, "new-everyday-password").close()


def test_backup_failure_does_not_rollback_successful_database_change(tmp_path: Path):
    database_path = tmp_path / "operations.sqlite3"
    missing_backup_directory = tmp_path / "disconnected-drive"
    with DatabaseSession.create(
        database_path,
        "everyday-password",
        "institution-recovery",
        backup_directory=missing_backup_directory,
    ) as session:
        result = session.append_revision(_record())
        assert result.error is not None
        assert not result.succeeded
        assert session.database.latest_revision().metadata.revision_id == REVISION_ID

    with DatabaseSession.open(database_path, "everyday-password") as reopened:
        assert reopened.database.latest_revision().metadata.revision_id == REVISION_ID


def test_restore_refuses_to_overwrite_existing_database(tmp_path: Path):
    source = tmp_path / "operations.sqlite3"
    backup_directory = tmp_path / "automatic-backups"
    backup_directory.mkdir()
    with DatabaseSession.create(
        source,
        "everyday-password",
        "institution-recovery",
        backup_directory=backup_directory,
    ) as session:
        backup = session.append_revision(_record()).record
    assert backup is not None

    destination = tmp_path / "existing.sqlite3"
    destination.write_bytes(b"do-not-overwrite")
    with pytest.raises(BackupRestoreConflictError):
        DatabaseSession.restore_and_open_with_user_password(
            backup.path,
            destination,
            "everyday-password",
        )
    assert destination.read_bytes() == b"do-not-overwrite"


def test_truncated_archive_is_rejected(tmp_path: Path):
    broken = tmp_path / f"broken{BACKUP_EXTENSION}"
    broken.write_bytes(b"not-a-zip")

    with pytest.raises(BackupFormatError):
        inspect_backup(broken)


def test_retention_keeps_latest_for_same_database_only_and_preserves_unknown_files(
    tmp_path: Path,
):
    backup_directory = tmp_path / "automatic-backups"
    backup_directory.mkdir()
    first_path = tmp_path / "first.sqlite3"
    second_path = tmp_path / "second.sqlite3"
    first = DatabaseSession.create(first_path, "first-user", "first-recovery")
    second = DatabaseSession.create(second_path, "second-user", "second-recovery")
    service = DatabaseBackupService(backup_directory, max_backups=2)
    base = datetime(2026, 8, 24, 9, 0, tzinfo=UTC)
    try:
        first_key = first.credentials.unlock_user("first-user")
        second_key = second.credentials.unlock_user("second-user")
        first_oldest = service.create_backup(
            first.database,
            first.credentials,
            first_key,
            created_at=base,
        )
        first_middle = service.create_backup(
            first.database,
            first.credentials,
            first_key,
            created_at=base + timedelta(minutes=1),
            force_full=True,
        )
        other_database = service.create_backup(
            second.database,
            second.credentials,
            second_key,
            created_at=base,
        )
        malformed = backup_directory / "직접_넣은_손상파일.mealbackup"
        malformed.write_bytes(b"keep-unknown")

        first_latest = service.create_backup(
            first.database,
            first.credentials,
            first_key,
            created_at=base + timedelta(minutes=2),
            force_full=True,
        )
    finally:
        first.close()
        second.close()

    assert not first_oldest.path.exists()
    assert first_middle.path.exists()
    assert first_latest.path.exists()
    assert other_database.path.exists()
    assert malformed.read_bytes() == b"keep-unknown"
    assert {
        record.path
        for record in service.list_backups(database_id=first_latest.database_id)
    } == {first_middle.path, first_latest.path}


def test_new_backup_survives_when_old_backup_cleanup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    backup_directory = tmp_path / "automatic-backups"
    backup_directory.mkdir()
    database_path = tmp_path / "operations.sqlite3"
    service = DatabaseBackupService(backup_directory, max_backups=1)
    with DatabaseSession.create(
        database_path,
        "everyday-password",
        "institution-recovery",
    ) as session:
        key = session.credentials.unlock_user("everyday-password")
        first = service.create_backup(
            session.database,
            session.credentials,
            key,
        )

        def fail_cleanup(*_args, **_kwargs):
            raise OSError("backup is open elsewhere")

        monkeypatch.setattr(service, "prune_backups", fail_cleanup)
        second = service.create_backup(
            session.database,
            session.credentials,
            key,
        )

    assert first.path.exists()
    assert second.path.exists()


def test_default_retention_is_100_and_invalid_counts_are_rejected(tmp_path: Path):
    assert DatabaseBackupService(tmp_path).max_backups == DEFAULT_MAX_BACKUPS == 100
    assert DatabaseBackupService(tmp_path, max_backups=None).max_backups is None
    with pytest.raises(ValueError, match="1 이상의 정수"):
        DatabaseBackupService(tmp_path, max_backups=0)
    with pytest.raises(ValueError, match="1 이상의 정수"):
        DatabaseBackupService(tmp_path, max_backups=True)
