"""Encrypted weekly snapshots and dependency-safe incremental restore points."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path
import random
import struct
from uuid import uuid4
import zipfile
import zlib

import pytest

from src.domain.revisions import AuditChange, ChangeOperation, RevisionRecord
from src.storage.database_backup import (
    DatabaseBackupService,
    inspect_backup,
)
from src.storage.database_session import DatabaseSession
from src.storage.database_backup import BackupFormatError, BackupIntegrityError
from src.storage.database_credentials import credential_path_for
from tests.test_database_backup import _record, _document_run, _legacy_backup
from tests.test_encrypted_database import _record as _import_record


START = datetime(2026, 10, 14, 0, tzinfo=UTC)


def _contents(database):
    connection = database._require_connection()
    schema = tuple(connection.execute("SELECT type, name, tbl_name, sql FROM sqlite_schema ORDER BY type,name"))
    tables = {}
    for kind, name, _table, _sql in schema:
        if kind == "table":
            quoted = '"' + name.replace('"', '""') + '"'
            tables[name] = tuple(sorted(connection.execute(f"SELECT * FROM {quoted}"), key=repr))
    return schema, tables, connection.pragma("user_version"), connection.pragma("application_id")


def _save(service, session, when=START, **kwargs):
    return service.create_backup(session.database, session.credentials, session._key_bytes(),
                                 created_at=when, **kwargs)


def _append_probe(session, value, *, previous=None):
    template = _record()
    latest = session.database.latest_revision()
    record = RevisionRecord(
        replace(template.metadata, revision_id=uuid4(),
                parent_revision_id=latest.metadata.revision_id if latest else None),
        (replace(template.changes[0],
                 operation=ChangeOperation.INSERT if previous is None else ChangeOperation.UPDATE,
                 before=previous, after=value),),
    )
    session.database.append_revision(record)
    return record


def test_weekly_full_boundary_survives_restart_and_empty_delta(tmp_path):
    folder = tmp_path / "backups"
    folder.mkdir()
    source = tmp_path / "source.sqlite3"
    service = DatabaseBackupService(folder, max_backups=None)
    with DatabaseSession.create(source, "password") as session:
        full = _save(service, session)
        assert full.format_version == 2 and full.backup_kind == "full"
        assert full.parent_backup_id is None
        _append_probe(session, {"value": "one"})
        delta = _save(service, session, START + timedelta(hours=1))
        assert delta.backup_kind == "incremental"
        assert delta.parent_backup_id == full.backup_id
        expected = _contents(session.database)
    with DatabaseSession.open(source, "password") as session:
        service = DatabaseBackupService(folder, max_backups=None)
        unchanged = _save(service, session, START + timedelta(days=7, seconds=-1))
        assert unchanged.backup_kind == "incremental"
        assert unchanged.snapshot_sha256 == delta.snapshot_sha256
        weekly = _save(service, session, START + timedelta(days=7))
        assert weekly.backup_kind == "full"
        assert weekly.parent_backup_id is None
    for i, point in enumerate((delta, unchanged, weekly)):
        with DatabaseSession.restore_and_open_with_user_password(
            point.path, tmp_path / f"restore-{i}.sqlite3", "password"
        ) as restored:
            assert _contents(restored.database) == expected


def test_incremental_restores_imports_and_document_only_changes_exactly(tmp_path):
    folder = tmp_path / "backups"
    folder.mkdir()
    service = DatabaseBackupService(folder, max_backups=None)
    with DatabaseSession.create(tmp_path / "source.sqlite3", "password") as session:
        _save(service, session)
        record = _import_record()
        session.database.append_revision(record)
        imported = _save(service, session, START + timedelta(minutes=1))
        expected_import = _contents(session.database)
        run = replace(_document_run(), source_revision_id=record.metadata.revision_id,
                      source_revision_display_code=session.database.revision_display_code(record.metadata.revision_id))
        session.database.append_document_run(run)
        document = _save(service, session, START + timedelta(minutes=2))
        expected_document = _contents(session.database)
        assert document.backup_kind == "incremental"
        assert document.latest_revision_id == imported.latest_revision_id
        assert document.snapshot_sha256 != imported.snapshot_sha256
    for i, (point, expected) in enumerate(((imported, expected_import), (document, expected_document))):
        with DatabaseSession.restore_and_open_with_user_password(
            point.path, tmp_path / f"restore-{i}.sqlite3", "password"
        ) as restored:
            assert _contents(restored.database) == expected


def test_small_edit_creates_smaller_encrypted_incremental(tmp_path):
    folder = tmp_path / "backups"
    folder.mkdir()
    service = DatabaseBackupService(folder, max_backups=None)
    rng = random.Random(7)
    changes = tuple(AuditChange("size-probe", uuid4(), ChangeOperation.INSERT, None,
                               {"text": rng.randbytes(1024).hex()}) for _ in range(160))
    template = _record()
    with DatabaseSession.create(tmp_path / "source.sqlite3", "password") as session:
        session.database.append_revision(RevisionRecord(template.metadata, changes))
        full = _save(service, session)
        update = RevisionRecord(replace(template.metadata, revision_id=uuid4(),
                                       parent_revision_id=template.metadata.revision_id),
                                (AuditChange("size-probe", changes[0].entity_id, ChangeOperation.UPDATE,
                                             changes[0].after, {"text": "changed"}),))
        session.database.append_revision(update)
        delta = _save(service, session, START + timedelta(minutes=1))
        assert delta.backup_kind == "incremental"
        assert delta.path.stat().st_size < full.path.stat().st_size / 2
        expected = _contents(session.database)
    with zipfile.ZipFile(delta.path) as archive:
        payload = archive.read("database.sqlite3")
        assert b"changed" not in payload and b"SQLite format 3" not in payload
    with DatabaseSession.restore_and_open_with_user_password(delta.path, tmp_path / "restored.sqlite3", "password") as restored:
        assert _contents(restored.database) == expected
        assert restored.database.path.read_bytes()[:16] != b"SQLite format 3\x00"


def test_retention_keeps_ancestors_until_no_retained_point_needs_them(tmp_path):
    folder = tmp_path / "backups"
    folder.mkdir()
    service = DatabaseBackupService(folder, max_backups=2)
    with DatabaseSession.create(tmp_path / "source.sqlite3", "password") as session:
        full = _save(service, session)
        first = _save(service, session, START + timedelta(minutes=1))
        second = _save(service, session, START + timedelta(minutes=2))
        assert all(item.path.exists() for item in (full, first, second))
        weekly = _save(service, session, START + timedelta(days=7))
        assert all(item.path.exists() for item in (full, first, second, weekly))
        newest = _save(service, session, START + timedelta(days=7, minutes=1))
        assert weekly.path.exists() and newest.path.exists()
        assert all(not item.path.exists() for item in (full, first, second))
    with DatabaseSession.restore_and_open_with_user_password(newest.path, tmp_path / "restored.sqlite3", "password") as restored:
        restored.database.check_integrity()


def test_copied_backup_does_not_block_full_or_incremental_creation_and_restore(tmp_path):
    folder = tmp_path / "backups"
    folder.mkdir()
    service = DatabaseBackupService(folder, max_backups=None)
    with DatabaseSession.create(tmp_path / "source.sqlite3", "password") as session:
        full = _save(service, session)
        (folder / "copy.mealbackup").write_bytes(full.path.read_bytes())
        delta = _save(service, session, START + timedelta(minutes=1))
        assert delta.backup_kind == "incremental"
        independent = _save(service, session, START + timedelta(minutes=2), force_full=True)
        assert independent.backup_kind == "full"
        expected = _contents(session.database)
    for index, point in enumerate((full, delta, independent)):
        with DatabaseSession.restore_and_open_with_user_password(
            point.path, tmp_path / f"restored-{index}.sqlite3", "password"
        ) as restored:
            assert _contents(restored.database) == expected


def test_truncated_zip_prevents_pruning_uncertain_dependencies(tmp_path):
    folder = tmp_path / "backups"
    folder.mkdir()
    service = DatabaseBackupService(folder, max_backups=1)
    with DatabaseSession.create(tmp_path / "source.sqlite3", "password") as session:
        first = _save(service, session)
        damaged = folder / "truncated.mealbackup"
        damaged.write_bytes(first.path.read_bytes()[:100])
        newest = _save(service, session, START + timedelta(days=7))
        assert first.path.exists() and newest.path.exists() and damaged.exists()


def test_missing_parent_fails_without_installing_and_next_backup_is_full(tmp_path):
    folder = tmp_path / "backups"
    folder.mkdir()
    service = DatabaseBackupService(folder, max_backups=None)
    with DatabaseSession.create(tmp_path / "source.sqlite3", "password") as session:
        full = _save(service, session)
        delta = _save(service, session, START + timedelta(minutes=1))
        full.path.rename(tmp_path / full.path.name)
        destination = tmp_path / "restored.sqlite3"
        with pytest.raises((BackupFormatError, BackupIntegrityError, FileNotFoundError)):
            DatabaseSession.restore_and_open_with_user_password(delta.path, destination, "password")
        assert not destination.exists() and not credential_path_for(destination).exists()
        newest = _save(service, session, START + timedelta(minutes=2))
        assert newest.backup_kind == "full"


def test_ciphertext_tamper_even_with_rehashed_envelope_is_rejected(tmp_path):
    folder = tmp_path / "backups"
    folder.mkdir()
    service = DatabaseBackupService(folder, max_backups=None)
    with DatabaseSession.create(tmp_path / "source.sqlite3", "password") as session:
        _save(service, session)
        point = _save(service, session, START + timedelta(minutes=1))
    with zipfile.ZipFile(point.path) as archive:
        entries = {name: archive.read(name) for name in archive.namelist()}
    damaged = bytearray(entries["database.sqlite3"])
    damaged[-1] ^= 1
    entries["database.sqlite3"] = bytes(damaged)
    manifest = json.loads(entries["manifest.json"])
    manifest["database"]["sha256"] = hashlib.sha256(damaged).hexdigest()
    entries["manifest.json"] = json.dumps(manifest).encode()
    with zipfile.ZipFile(point.path, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    destination = tmp_path / "restored.sqlite3"
    with pytest.raises((BackupFormatError, BackupIntegrityError)):
        DatabaseSession.restore_and_open_with_user_password(point.path, destination, "password")
    assert not destination.exists() and not credential_path_for(destination).exists()


@pytest.mark.parametrize("mutation", ["cycle", "wrong_lineage", "future_schema", "snapshot_hash"])
def test_invalid_chain_metadata_does_not_install_a_database(tmp_path, mutation):
    folder = tmp_path / "backups"
    folder.mkdir()
    service = DatabaseBackupService(folder, max_backups=None)
    with DatabaseSession.create(tmp_path / "source.sqlite3", "password") as session:
        _save(service, session)
        point = _save(service, session, START + timedelta(minutes=1))
    with zipfile.ZipFile(point.path) as archive:
        entries = {name: archive.read(name) for name in archive.namelist()}
    manifest = json.loads(entries["manifest.json"])
    if mutation == "cycle":
        manifest["parent_backup_id"] = manifest["backup_id"]
    elif mutation == "wrong_lineage":
        manifest["database_id"] = str(uuid4())
    elif mutation == "future_schema":
        manifest["schema_version"] = 9999
    else:
        manifest["snapshot"]["sha256"] = "0" * 64
    entries["manifest.json"] = json.dumps(manifest).encode()
    with zipfile.ZipFile(point.path, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    destination = tmp_path / "restored.sqlite3"
    with pytest.raises((BackupFormatError, BackupIntegrityError)):
        DatabaseSession.restore_and_open_with_user_password(point.path, destination, "password")
    assert not destination.exists() and not credential_path_for(destination).exists()


def test_password_rotation_keeps_chain_and_historical_states_restorable(tmp_path):
    folder = tmp_path / "backups"
    folder.mkdir()
    with DatabaseSession.create(tmp_path / "source.sqlite3", "before", "recovery", backup_directory=folder) as session:
        full = session.backup_now()
        changed = session.append_revision(_record()).record
        assert changed.backup_kind == "incremental"
        points = (full, changed)
        payloads = {}
        for point in points:
            with zipfile.ZipFile(point.path) as archive:
                payloads[point.path] = archive.read("database.sqlite3")
        result = session.change_user_password("before", "after")
        assert result.succeeded and not result.rotation.failed
        assert set(result.rotation.updated) == {item.path for item in points}
    for i, point in enumerate(points):
        with zipfile.ZipFile(point.path) as archive:
            assert archive.read("database.sqlite3") == payloads[point.path]
        with DatabaseSession.restore_and_open_with_user_password(point.path, tmp_path / f"restored-{i}.sqlite3", "after") as restored:
            latest = restored.database.latest_revision()
            assert (latest.metadata.revision_id if latest else None) == point.latest_revision_id


def test_legacy_full_restores_and_new_directory_starts_independent_full(tmp_path):
    folder = tmp_path / "backups"
    folder.mkdir()
    other = tmp_path / "new-backups"
    other.mkdir()
    with DatabaseSession.create(tmp_path / "source.sqlite3", "password") as session:
        session.database.append_revision(_record())
        legacy = _legacy_backup(session, folder)
        assert legacy.format_version == 1
        expected = _contents(session.database)
        current = _save(DatabaseBackupService(folder), session)
        assert current.format_version == 2 and current.backup_kind == "full"
        moved = _save(DatabaseBackupService(other), session)
        assert moved.backup_kind == "full" and moved.parent_backup_id is None
    for i, point in enumerate((legacy, current, moved)):
        with DatabaseSession.restore_and_open_with_user_password(point.path, tmp_path / f"restore-{i}.sqlite3", "password") as restored:
            assert _contents(restored.database) == expected


@pytest.mark.parametrize("target_size", [4096, 8192, 8192 + 153])
def test_block_delta_preserves_growth_shrinkage_and_last_partial_block(target_size):
    from src.storage.backup_snapshot import compress_delta, decrypt, encrypt

    parent = random.Random(22).randbytes(8192)
    target = (parent[:target_size] + b"tail" * 40)[:target_size]
    target = b"changed" + target[7:]
    manifest = {"backup_kind": "incremental", "snapshot": {"size": len(target)}}
    encrypted = encrypt(compress_delta(parent, target), b"x" * 32, manifest)
    assert decrypt(encrypted, b"x" * 32, manifest, parent) == target


@pytest.mark.parametrize("offsets", [(0, 0), (0, 1), (0, 8192)])
def test_invalid_delta_offsets_are_rejected_even_in_authenticated_payload(offsets):
    from src.storage.backup_snapshot import decrypt, encrypt

    manifest = {"backup_kind": "incremental", "snapshot": {"size": 8192}}
    raw = struct.pack(">QI", 8192, len(offsets))
    raw += b"".join(struct.pack(">QI", offset, 4096) + b"b" * 4096 for offset in offsets)
    payload = encrypt(zlib.compress(raw), b"x" * 32, manifest)
    with pytest.raises(ValueError):
        decrypt(payload, b"x" * 32, manifest, b"a" * 8192)


def test_backup_creation_failure_keeps_existing_points_and_committed_revision(tmp_path, monkeypatch):
    from src.storage import database_backup as module

    folder = tmp_path / "backups"
    folder.mkdir()
    with DatabaseSession.create(tmp_path / "source.sqlite3", "password", backup_directory=folder) as session:
        first = session.backup_now()
        original = first.path.read_bytes()
        real_replace = module.os.replace

        def fail_publish(source, destination):
            if Path(destination).parent == folder and str(destination).endswith(".mealbackup"):
                raise OSError("synthetic publish failure")
            return real_replace(source, destination)

        monkeypatch.setattr(module.os, "replace", fail_publish)
        outcome = session.append_revision(_record())
        assert outcome.error is not None
        assert session.database.latest_revision().metadata.revision_id == _record().metadata.revision_id
        assert first.path.read_bytes() == original
        assert set(folder.iterdir()) == {first.path}
