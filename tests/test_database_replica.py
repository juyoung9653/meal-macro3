"""Production replica backup: bounded writes, catch-up and independent recovery."""
from dataclasses import replace
import json
from uuid import uuid4

import pytest

from src.domain.revisions import RevisionActor
from src.storage.database_backup import DatabaseBackupService
from src.storage.database_credentials import CredentialUnlockError, credential_path_for
from src.storage.database_replica import DatabaseReplicaService
from src.storage.database_session import DatabaseSession
from src.storage.encrypted_database import EncryptedDatabase, DatabaseIntegrityError
from tests.test_encrypted_database import _roster_snapshot, _roster_record
from tests.test_database_hot_paths import edit
from tests.test_database_backup import _record, _document_run


def change(roster, index):
    updated = edit(roster)
    return replace(updated, persons=(replace(updated.persons[0],
        profile=replace(updated.persons[0].profile, note=f"수정 {index}")), *updated.persons[1:]))


def test_ordinary_save_never_serializes_or_copies_whole_database(tmp_path, monkeypatch):
    folder = tmp_path / "backups"
    folder.mkdir()
    with DatabaseSession.create(tmp_path / "source.sqlite3", "password", backup_directory=folder) as session:
        roster = _roster_snapshot(uuid4())
        first = session.append_revision(_roster_record(roster))
        assert first.succeeded
        def forbidden(*args, **kwargs):
            raise AssertionError("whole database processing on an ordinary save")
        with monkeypatch.context() as patch:
            patch.setattr(EncryptedDatabase, "snapshot_bytes", forbidden)
            patch.setattr(EncryptedDatabase, "backup_to", forbidden)
            patch.setattr(EncryptedDatabase, "check_integrity", forbidden)
            patch.setattr("src.services.revision_branches.registry_from_connection", forbidden)
            for index in range(3):
                updated = change(roster, index)
                result = session.append_revision(_roster_record(updated, before=roster))
                assert result.succeeded, result.error
                assert result.record.path == first.record.path
                roster = updated
        assert len(tuple(folder.glob("*.mealreplica"))) == 1
        assert not tuple(folder.glob("*.mealbackup"))
    with DatabaseSession.restore_and_open_with_user_password(first.record.path, tmp_path / "restored.sqlite3", "password") as restored:
        assert restored.database.current_roster() == roster
        assert len(restored.database.branch_revision_ids()) == 4


@pytest.mark.parametrize("delete_credentials", [False, True])
def test_deleted_backup_is_recreated_on_next_save(tmp_path, delete_credentials):
    folder = tmp_path / "backups"
    folder.mkdir()
    with DatabaseSession.create(tmp_path / "source.sqlite3", "password", backup_directory=folder) as session:
        roster = _roster_snapshot(uuid4())
        first = session.append_revision(_roster_record(roster)).record
        session._backup_service.clear_cache()
        first.path.unlink()
        if delete_credentials:
            credential_path_for(first.path).unlink()
        updated = change(roster, 1)
        result = session.append_revision(_roster_record(updated, before=roster))
        assert result.succeeded, result.error
    with DatabaseSession.restore_and_open_with_user_password(result.record.path, tmp_path / "restored.sqlite3", "password") as restored:
        assert restored.database.current_roster() == updated


def test_restart_fills_missing_tail_without_duplicates(tmp_path):
    folder = tmp_path / "backups"
    folder.mkdir()
    path = tmp_path / "source.sqlite3"
    with DatabaseSession.create(path, "password", backup_directory=folder) as session:
        roster = _roster_snapshot(uuid4())
        session.append_revision(_roster_record(roster))
        for index in range(3):
            updated = change(roster, index)
            session.database.append_revision(_roster_record(updated, before=roster))
            roster = updated
    with DatabaseSession.open(path, "password", backup_directory=folder) as session:
        backup = session.backup_now()
        assert session.backup_now().path == backup.path
        expected = session.database.branch_revision_ids()
    with DatabaseSession.restore_and_open_with_user_password(backup.path, tmp_path / "restored.sqlite3", "password") as restored:
        assert restored.database.branch_revision_ids() == expected
        assert restored.database.current_roster() == roster


def test_metadata_failure_keeps_source_and_retry_completes_backup(tmp_path, monkeypatch):
    folder = tmp_path / "backups"
    folder.mkdir()
    with DatabaseSession.create(tmp_path / "source.sqlite3", "password", backup_directory=folder) as session:
        roster = _roster_snapshot(uuid4())
        session.append_revision(_roster_record(roster))
        updated = change(roster, 1)
        def fail(*args, **kwargs):
            raise OSError("injected metadata publication failure")
        with monkeypatch.context() as patch:
            patch.setattr("src.storage.database_replica._write_manifest", fail)
            result = session.append_revision(_roster_record(updated, before=roster))
            assert not result.succeeded
            assert session.database.current_roster() == updated
        backup = session.backup_now()
    with DatabaseSession.restore_and_open_with_user_password(backup.path, tmp_path / "restored.sqlite3", "password") as restored:
        assert restored.database.current_roster() == updated
        assert len(restored.database.branch_revision_ids()) == 2


def test_document_history_and_user_layout_are_in_independent_backup(tmp_path):
    folder = tmp_path / "backups"
    folder.mkdir()
    layout = {"version": 1, "active": "main", "aliases": {}, "sheets": [
        {"id": "main", "name": "명단", "layout": {"order": ["name"], "hidden": [], "widths": {"name": 200}}}]}
    with DatabaseSession.create(tmp_path / "source.sqlite3", "password", backup_directory=folder) as session:
        session.append_revision(_record())
        result = session.append_document_run(_document_run())
        assert result.succeeded
        session.database.save_user_view_layout(RevisionActor("tester", "test-sid"), "test-pc", layout)
        expected_layouts = session.database.user_view_layouts()
        backup = session.backup_now()
    with DatabaseSession.restore_and_open_with_user_password(backup.path, tmp_path / "restored.sqlite3", "password") as restored:
        assert restored.database.document_run(_document_run().run_id) == _document_run()
        assert restored.database.user_view_layouts() == expected_layouts


def test_password_rotation_changes_one_replica_and_retains_recovery(tmp_path):
    folder = tmp_path / "backups"
    folder.mkdir()
    with DatabaseSession.create(tmp_path / "source.sqlite3", "old", "recovery", backup_directory=folder) as session:
        initial = session.append_revision(_record()).record
        result = session.change_user_password("old", "new")
        assert result.succeeded
        assert result.record.path == initial.path
        assert len(tuple(folder.glob("*.mealreplica"))) == 1
    with pytest.raises(CredentialUnlockError):
        DatabaseSession.restore_and_open_with_user_password(initial.path, tmp_path / "wrong.sqlite3", "old")
    with DatabaseSession.restore_and_open_with_user_password(initial.path, tmp_path / "new.sqlite3", "new") as restored:
        assert restored.database.latest_revision_id() == _record().metadata.revision_id
    with DatabaseSession.restore_and_open_with_recovery_password(initial.path, tmp_path / "recovered.sqlite3", "recovery", "reset") as restored:
        restored.database.check_integrity()


def test_legacy_archives_removed_only_after_success_and_other_files_preserved(tmp_path, monkeypatch):
    folder = tmp_path / "backups"
    folder.mkdir()
    legacy = DatabaseBackupService(folder)
    with DatabaseSession.create(tmp_path / "foreign.sqlite3", "foreign") as other:
        foreign = legacy.create_backup(other.database, other.credentials, other._key_bytes()).path
    unknown = folder / "unknown.mealbackup"
    unknown.write_bytes(b"not an identifiable backup")
    with DatabaseSession.create(tmp_path / "source.sqlite3", "password", backup_directory=folder) as session:
        session.database.append_revision(_record())
        old = legacy.create_backup(session.database, session.credentials, session._key_bytes()).path
        def fail(*args, **kwargs):
            raise OSError("injected initial copy failure")
        with monkeypatch.context() as patch:
            patch.setattr(EncryptedDatabase, "backup_to", fail)
            assert not session.try_backup_now().succeeded
            assert old.exists()
        result = session.try_backup_now()
        assert result.succeeded, result.error
        assert not old.exists()
        assert foreign.exists() and unknown.exists()
    with DatabaseSession.restore_and_open_with_user_password(result.record.path, tmp_path / "restored.sqlite3", "password") as restored:
        restored.database.check_integrity()


def test_changed_manifest_is_rejected_without_deleting_backup(tmp_path):
    folder = tmp_path / "backups"
    folder.mkdir()
    with DatabaseSession.create(tmp_path / "source.sqlite3", "password", backup_directory=folder) as session:
        backup = session.append_revision(_record()).record
        sidecar = credential_path_for(backup.path)
        document = json.loads(sidecar.read_bytes())
        document["replica"]["source_database_name"] = "tampered"
        sidecar.write_text(json.dumps(document), encoding="utf-8")
        result = session.try_backup_now()
        assert not result.succeeded and isinstance(result.error, DatabaseIntegrityError)
        assert backup.path.exists()
    with pytest.raises(DatabaseIntegrityError):
        DatabaseSession.restore_and_open_with_user_password(backup.path, tmp_path / "restored.sqlite3", "password")
    assert not (tmp_path / "restored.sqlite3").exists()


def test_failed_password_update_preserves_old_backup_credentials(tmp_path, monkeypatch):
    folder = tmp_path / "backups"
    folder.mkdir()
    source = tmp_path / "source.sqlite3"
    with DatabaseSession.create(source, "old", backup_directory=folder) as session:
        backup = session.append_revision(_record()).record
        original = credential_path_for(backup.path).read_bytes()
        def fail(*args, **kwargs):
            raise OSError("injected sidecar replacement failure")
        with monkeypatch.context() as patch:
            patch.setattr("src.storage.database_replica._write_file", fail)
            result = session.change_user_password("old", "new")
        assert result.error is not None and result.rotation_error is not None
        assert credential_path_for(backup.path).read_bytes() == original
    with DatabaseSession.open(source, "new"):
        pass
    with DatabaseSession.restore_and_open_with_user_password(backup.path, tmp_path / "restored.sqlite3", "old") as restored:
        restored.database.check_integrity()


def test_failed_replica_append_keeps_valid_prefix_then_retry_catches_up(tmp_path, monkeypatch):
    folder = tmp_path / "backups"
    folder.mkdir()
    with DatabaseSession.create(tmp_path / "source.sqlite3", "password", backup_directory=folder) as session:
        roster = _roster_snapshot(uuid4())
        initial = session.append_revision(_roster_record(roster)).record
        replica = session._backup_service._replica
        updated = change(roster, 1)
        def fail(*args, **kwargs):
            raise OSError("injected replica append failure")
        with monkeypatch.context() as patch:
            patch.setattr(replica, "append_revision", fail)
            result = session.append_revision(_roster_record(updated, before=roster))
        assert result.error is not None
        assert session.database.current_roster() == updated
        with DatabaseSession.restore_and_open_with_user_password(initial.path, tmp_path / "prefix.sqlite3", "password") as restored:
            assert restored.database.current_roster() == roster
        assert session.try_backup_now().succeeded
    with DatabaseSession.restore_and_open_with_user_password(initial.path, tmp_path / "complete.sqlite3", "password") as restored:
        assert restored.database.current_roster() == updated
