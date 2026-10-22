"""Regression contracts for selective reads and operation-local backup reuse."""
import zipfile
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest

from src.domain.revisions import AuditChange, ChangeOperation, RevisionRecord
from src.storage.database_backup import BackupIntegrityError, DatabaseBackupService
from src.storage.database_session import DatabaseSession
from src.storage.encrypted_database import DatabaseIntegrityError, EncryptedDatabase
from src.storage.typed_projection import ProjectionContractError
from tests.test_database_backup_incremental import START, _save
from tests.test_encrypted_database import (
    ENTITY_ID,
    KEY,
    REVISION_ID,
    SECOND_REVISION_ID,
    _record,
    _roster_record,
    _roster_snapshot,
)


def test_metadata_only_read_preserves_metadata_without_decoding_changes(tmp_path, monkeypatch):
    import src.storage.encrypted_database as module
    with EncryptedDatabase.create(tmp_path / "test.sqlite3", KEY) as database:
        database.append_revision(_record())
        expected = database.revision(REVISION_ID).metadata
        def unexpected(*args, **kwargs):
            raise AssertionError("metadata read materialized audit changes")
        monkeypatch.setattr(module, "AuditChange", unexpected)
        assert database.revision_metadata(REVISION_ID) == expected
        assert database.latest_revision_id() == REVISION_ID


@pytest.mark.parametrize("mutation", ["unsealed", "count", "batch"])
def test_selective_reads_reject_damaged_seals(tmp_path, mutation):
    with EncryptedDatabase.create(tmp_path / "test.sqlite3", KEY) as database:
        database.append_revision(_record())
        connection = database._require_connection()
        if mutation == "unsealed":
            connection.execute("DROP TRIGGER revision_seal_no_delete")
            connection.execute("DELETE FROM revision_seal")
        elif mutation == "count":
            connection.execute("DROP TRIGGER revision_seal_no_update")
            connection.execute("UPDATE revision_seal SET change_count = change_count + 1")
        else:
            connection.execute("DROP TRIGGER import_batch_no_delete")
            connection.execute("DELETE FROM import_batch")
        for read in (database.revision_metadata, database.snapshot,
                     lambda revision: database.snapshot_for_kinds(revision, ())):
            with pytest.raises(DatabaseIntegrityError):
                read(REVISION_ID)


def test_selective_reads_reject_missing_revision_and_invalid_id(tmp_path):
    with EncryptedDatabase.create(tmp_path / "test.sqlite3", KEY) as database:
        assert database.latest_revision_id() is None
        for read in (database.revision_metadata, database.snapshot,
                     lambda revision: database.snapshot_for_kinds(revision, ())):
            with pytest.raises(KeyError):
                read(uuid4())
            with pytest.raises(TypeError):
                read("not a UUID")


def test_filtered_snapshot_preserves_historical_order_deletes_and_freezing(tmp_path):
    first = _record()
    extra_id = uuid4()
    first = replace(first, changes=first.changes + (
        AuditChange("other", extra_id, ChangeOperation.INSERT, None, {"nested": [1, {"x": 2}]}),
    ))
    second = RevisionRecord(
        replace(first.metadata, revision_id=SECOND_REVISION_ID, parent_revision_id=REVISION_ID),
        (AuditChange("person", ENTITY_ID, ChangeOperation.DELETE, first.changes[0].after, None),
         AuditChange("other", extra_id, ChangeOperation.UPDATE, first.changes[1].after,
                     {"nested": [3, {"x": 4}]})),
    )
    with EncryptedDatabase.create(tmp_path / "test.sqlite3", KEY) as database:
        database.append_revision(first)
        database.append_revision(second)
        for revision_id in (REVISION_ID, SECOND_REVISION_ID):
            full = database.snapshot(revision_id)
            filtered = database.snapshot_for_kinds(revision_id, ("other", "other"))
            assert tuple(filtered.entities) == tuple(ref for ref in full.entities if ref.entity_kind == "other")
            assert filtered.entities == {ref: state for ref, state in full.entities.items() if ref.entity_kind == "other"}
            with pytest.raises(TypeError):
                filtered.entity("other", extra_id)["nested"][1]["x"] = 7
        assert database.snapshot(SECOND_REVISION_ID).entity("person", ENTITY_ID) is None
        assert database.snapshot_for_kinds(REVISION_ID, ()).entities == {}


@pytest.mark.parametrize("tamper_target", ["changed", "unrelated"])
def test_person_edit_detects_projection_tamper_and_rolls_back(tmp_path, tamper_target):
    first = _roster_snapshot(REVISION_ID)
    other_id = uuid4()
    first = replace(first, persons=first.persons + (replace(first.persons[0], person_id=other_id),))
    second = replace(first, revision_id=SECOND_REVISION_ID,
                     persons=(replace(first.persons[0], profile=replace(first.persons[0].profile, note="new")), first.persons[1]))
    with EncryptedDatabase.create(tmp_path / "test.sqlite3", KEY) as database:
        database.append_revision(_roster_record(first))
        connection = database._require_connection()
        for (name,) in tuple(connection.execute("SELECT name FROM sqlite_schema WHERE type = 'trigger' AND tbl_name = 'person_projection'")):
            connection.execute('DROP TRIGGER "' + name.replace('"', '""') + '"')
        target = ENTITY_ID if tamper_target == "changed" else other_id
        connection.execute("UPDATE person_projection SET note = 'tampered' WHERE person_id = ?", (str(target),))
        with pytest.raises(ProjectionContractError):
            database.append_revision(_roster_record(second, before=first))
        assert database.latest_revision_id() == REVISION_ID
        assert database.revision_metadata(REVISION_ID).revision_id == REVISION_ID


def test_backup_reuses_parent_only_within_creation_and_checks_staged_disk(tmp_path, monkeypatch):
    import src.storage.database_backup as module
    folder = tmp_path / "backups"
    folder.mkdir()
    service = DatabaseBackupService(folder, max_backups=None)
    with DatabaseSession.create(tmp_path / "test.sqlite3", "password") as session:
        first = _save(service, session)
        decoded = []
        original = module._decode_snapshot
        def tracked(record, key, parent):
            decoded.append(record.path)
            return original(record, key, parent)
        monkeypatch.setattr(module, "_decode_snapshot", tracked)
        second = _save(service, session, START + timedelta(minutes=1))
        assert second.backup_kind == "incremental"
        assert decoded.count(first.path) == 1
        assert len(decoded) == 2  # parent and freshly read staged archive

        def corrupt_staged(record, key, parent):
            if record.path.suffix == ".tmp":
                with zipfile.ZipFile(record.path) as archive:
                    entries = {name: archive.read(name) for name in archive.namelist()}
                damaged = bytearray(entries["database.sqlite3"])
                damaged[-1] ^= 1
                entries["database.sqlite3"] = bytes(damaged)
                with zipfile.ZipFile(record.path, "w") as archive:
                    for name, value in entries.items():
                        archive.writestr(name, value)
            return original(record, key, parent)
        monkeypatch.setattr(module, "_decode_snapshot", corrupt_staged)
        with pytest.raises(BackupIntegrityError):
            _save(service, session, START + timedelta(minutes=2))
        assert set(folder.iterdir()) == {first.path, second.path}


@pytest.mark.parametrize("mutation", ["delete", "corrupt"])
def test_backup_rechecks_parent_files_before_publishing(tmp_path, monkeypatch, mutation):
    import src.storage.database_backup as module
    folder = tmp_path / "backups"
    folder.mkdir()
    service = DatabaseBackupService(folder, max_backups=None)
    with DatabaseSession.create(tmp_path / "test.sqlite3", "password") as session:
        first = _save(service, session)
        inspect = module.inspect_backup

        def mutate_parent_after_preparation(path):
            record = inspect(path)
            if path.suffix == ".tmp":
                if mutation == "delete":
                    first.path.unlink()
                else:
                    first.path.write_bytes(b"corrupted backup")
            return record

        monkeypatch.setattr(module, "inspect_backup", mutate_parent_after_preparation)
        with pytest.raises(BackupIntegrityError):
            _save(service, session, START + timedelta(minutes=1))
        assert set(folder.iterdir()) <= {first.path}
