"""Fast paths must retain transaction, corruption and restoration guarantees."""
from dataclasses import replace
from datetime import timedelta
import os
import json
import zipfile
from uuid import uuid4

import pytest
import apsw

from src.storage.database_session import DatabaseSession
from src.storage.database_backup import DatabaseBackupService
from src.storage.encrypted_database import EncryptedDatabase, DatabaseIntegrityError
from src.storage.typed_projection import ProjectionContractError
from tests.test_encrypted_database import KEY, _roster_snapshot, _roster_record
from tests.test_database_backup_incremental import START, _save


def edit(roster):
    return replace(roster, revision_id=uuid4(), persons=(
        replace(roster.persons[0], profile=replace(roster.persons[0].profile, note="edited")),
        *roster.persons[1:],
    ))


def test_verified_person_save_and_backup_do_not_replay_whole_database(tmp_path, monkeypatch):
    import src.storage.encrypted_database as database_module
    import src.storage.typed_projection as projections
    source = _roster_snapshot(uuid4())
    source = replace(source, persons=(*source.persons, replace(source.persons[0], person_id=uuid4())))
    with EncryptedDatabase.create(tmp_path / "db.sqlite3", KEY) as database:
        database.append_revision(_roster_record(source))
        original = database.current_roster()
        def unexpected(*args, **kwargs):
            raise AssertionError("replayed unchanged data")
        with monkeypatch.context() as patch:
            patch.setattr(database_module, "_validate_ledger", unexpected)
            patch.setattr(projections, "_load_typed_state_and_roster", unexpected)
            changed = edit(source)
            database.append_revision(_roster_record(changed, before=source))
            current = database.current_roster()
            unchanged_id = source.persons[1].person_id
            assert current.person(unchanged_id) is original.person(unchanged_id)
            assert current.person(source.persons[0].person_id).profile.note == "edited"
            assert database.snapshot_bytes().startswith(b"SQLite format 3\x00")
        database.check_integrity()


@pytest.mark.parametrize("external", [False, True])
def test_unexpected_projection_write_invalidates_trust(tmp_path, external):
    source = _roster_snapshot(uuid4())
    source = replace(source, persons=(*source.persons, replace(source.persons[0], person_id=uuid4())))
    path = tmp_path / "db.sqlite3"
    with EncryptedDatabase.create(path, KEY) as database:
        database.append_revision(_roster_record(source))
        connection = database._require_connection()
        if external:
            connection = apsw.Connection(str(path))
            connection.pragma("cipher", "chacha20")
            connection.pragma("hexkey", KEY.hex())
        try:
            for (name,) in tuple(connection.execute("SELECT name FROM sqlite_schema WHERE type='trigger' AND tbl_name='person_projection'")):
                connection.execute('DROP TRIGGER "' + name.replace('"', '""') + '"')
            connection.execute("UPDATE person_projection SET note='tampered' WHERE person_id=?",
                               (str(source.persons[1].person_id),))
        finally:
            if external:
                connection.close()
        assert database.last_committed_record is None
        with pytest.raises(ProjectionContractError):
            database.append_revision(_roster_record(edit(source), before=source))
        with pytest.raises(DatabaseIntegrityError):
            database.snapshot_bytes()
        assert database.latest_revision_id() == source.revision_id


def test_outer_rollback_never_reuses_uncommitted_roster_or_revision(tmp_path):
    source = _roster_snapshot(uuid4())
    with EncryptedDatabase.create(tmp_path / "db.sqlite3", KEY) as database:
        database.append_revision(_roster_record(source))
        database.revision(source.revision_id)
        connection = database._require_connection()
        connection.execute("BEGIN IMMEDIATE")
        changed = edit(source)
        database.append_revision(_roster_record(changed, before=source))
        database.check_integrity()
        assert database.current_roster().revision_id == changed.revision_id
        assert database.branch_revision_ids() == (source.revision_id, changed.revision_id)
        database.revision(changed.revision_id)
        connection.execute("ROLLBACK")
        assert database.current_roster().revision_id == source.revision_id
        assert database.branch_revision_ids() == (source.revision_id,)
        with pytest.raises(KeyError):
            database.revision(changed.revision_id)
        database.check_integrity()


@pytest.mark.parametrize("reopen", [False, True])
def test_branch_registry_is_incremental_after_create_or_open(tmp_path, monkeypatch, reopen):
    source = _roster_snapshot(uuid4())
    path = tmp_path / "db.sqlite3"
    database = EncryptedDatabase.create(path, KEY)
    try:
        database.append_revision(_roster_record(source))
        if reopen:
            database.close()
            database = EncryptedDatabase.open(path, KEY)
        def unexpected(*args, **kwargs):
            raise AssertionError("unchanged revision history was replayed")
        with monkeypatch.context() as patch:
            patch.setattr("src.services.revision_branches.registry_from_connection", unexpected)
            ids = [source.revision_id]
            for index in range(3):
                changed = edit(source)
                changed = replace(changed, persons=(replace(changed.persons[0],
                    profile=replace(changed.persons[0].profile, note=f"edit {index}")),
                    *changed.persons[1:]))
                database.append_revision(_roster_record(changed, before=source))
                ids.append(changed.revision_id)
                assert database.branch_revision_ids() == tuple(ids)
                assert database.branches()[0].tip_revision_id == changed.revision_id
                source = changed
        database.check_integrity()
    finally:
        database.close()
    assert database._branch_registry_cache is None


def test_external_append_refreshes_branch_registry_once(tmp_path, monkeypatch):
    import src.services.revision_branches as branches
    source = _roster_snapshot(uuid4())
    path = tmp_path / "db.sqlite3"
    with EncryptedDatabase.create(path, KEY) as database:
        database.append_revision(_roster_record(source))
        assert database.branch_revision_ids() == (source.revision_id,)
        changed = edit(source)
        with EncryptedDatabase.open(path, KEY) as other:
            other.append_revision(_roster_record(changed, before=source))
        original = branches.registry_from_connection
        calls = []
        def counted(connection):
            calls.append(True)
            return original(connection)
        monkeypatch.setattr(branches, "registry_from_connection", counted)
        for _ in range(2):
            assert database.branch_revision_ids() == (source.revision_id, changed.revision_id)
            assert database.branches()[0].tip_revision_id == changed.revision_id
        assert len(calls) == 1


def test_failed_save_does_not_publish_candidate_branch_tip(tmp_path, monkeypatch):
    source = _roster_snapshot(uuid4())
    with EncryptedDatabase.create(tmp_path / "db.sqlite3", KEY) as database:
        database.append_revision(_roster_record(source))
        before = database.branches()
        failed = edit(source)
        def fail(*args, **kwargs):
            raise RuntimeError("injected after ledger write")
        with monkeypatch.context() as patch:
            patch.setattr("src.storage.encrypted_database.apply_typed_projection_changes", fail)
            with pytest.raises(RuntimeError, match="injected"):
                database.append_revision(_roster_record(failed, before=source))
        assert database.branches() == before
        assert database.branch_revision_ids() == (source.revision_id,)
        changed = edit(source)
        database.append_revision(_roster_record(changed, before=source))
        assert database.branch_revision_ids() == (source.revision_id, changed.revision_id)
        database.check_integrity()


def test_cached_revision_rechecks_direct_sql_tampering(tmp_path):
    source = _roster_snapshot(uuid4())
    with EncryptedDatabase.create(tmp_path / "db.sqlite3", KEY) as database:
        database.append_revision(_roster_record(source))
        database.revision(source.revision_id)
        connection = database._require_connection()
        connection.execute("DROP TRIGGER revision_seal_no_update")
        connection.execute("UPDATE revision_seal SET change_count=change_count+1")
        with pytest.raises(DatabaseIntegrityError):
            database.revision(source.revision_id)


def test_cached_backup_rechecks_bytes_even_with_same_size_and_timestamp(tmp_path):
    folder = tmp_path / "backups"
    folder.mkdir()
    service = DatabaseBackupService(folder, max_backups=None)
    with DatabaseSession.create(tmp_path / "source.sqlite3", "password") as session:
        first = _save(service, session)
        original_stat = first.path.stat()
        content = bytearray(first.path.read_bytes())
        content[60] ^= 1
        first.path.write_bytes(content)
        os.utime(first.path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        second = _save(service, session, START + timedelta(minutes=1))
        assert second.backup_kind == "full"
    with DatabaseSession.restore_and_open_with_user_password(
        second.path, tmp_path / "restored.sqlite3", "password",
    ) as restored:
        restored.database.check_integrity()


def test_cached_backup_rechecks_unknown_authenticated_manifest_fields(tmp_path):
    folder = tmp_path / "backups"
    folder.mkdir()
    service = DatabaseBackupService(folder, max_backups=None)
    with DatabaseSession.create(tmp_path / "source.sqlite3", "password") as session:
        first = _save(service, session)
        with zipfile.ZipFile(first.path) as archive:
            entries = {name: archive.read(name) for name in archive.namelist()}
        manifest = json.loads(entries["manifest.json"])
        manifest["unknown_authenticated_field"] = "tampered"
        entries["manifest.json"] = json.dumps(manifest).encode()
        with zipfile.ZipFile(first.path, "w", compression=zipfile.ZIP_STORED) as archive:
            for name, payload in entries.items():
                archive.writestr(name, payload)
        second = _save(service, session, START + timedelta(minutes=1))
        assert second.backup_kind == "full"
    with DatabaseSession.restore_and_open_with_user_password(
        second.path, tmp_path / "restored.sqlite3", "password",
    ) as restored:
        restored.database.check_integrity()


def test_backup_chain_is_bounded_and_every_point_restores(tmp_path, monkeypatch):
    import src.storage.database_backup as module
    monkeypatch.setattr(module, "MAX_INCREMENTAL_CHAIN", 3)
    folder = tmp_path / "backups"
    folder.mkdir()
    service = DatabaseBackupService(folder, max_backups=None)
    with DatabaseSession.create(tmp_path / "source.sqlite3", "password") as session:
        points = [_save(service, session, START + timedelta(minutes=i)) for i in range(5)]
        assert [point.backup_kind for point in points] == ["full", "incremental", "incremental", "full", "incremental"]
    for index, point in enumerate(points):
        with DatabaseSession.restore_and_open_with_user_password(
            point.path, tmp_path / f"restored-{index}.sqlite3", "password",
        ) as restored:
            restored.database.check_integrity()


def test_session_close_releases_backup_image(tmp_path):
    folder = tmp_path / "backups"
    folder.mkdir()
    with DatabaseSession.create(tmp_path / "db.sqlite3", "password", backup_directory=folder) as session:
        session.backup_now()
        service = session._backup_service
        assert service._replica is not None
    assert service._replica is None


def test_revision_search_matches_saved_values_and_number_without_roster_replay(tmp_path, monkeypatch):
    source = _roster_snapshot(uuid4())
    with EncryptedDatabase.create(tmp_path / "db.sqlite3", KEY) as database:
        database.append_revision(_roster_record(source))
        changed = edit(source)
        database.append_revision(_roster_record(changed, before=source))
        code = database.revision_display_code(changed.revision_id)
        def unexpected(*args):
            raise AssertionError("search should not reconstruct snapshots")
        monkeypatch.setattr(database, "snapshot", unexpected)
        assert database.search_revision_ids(code + " edited") == {changed.revision_id}
        assert database.search_revision_ids("홍길동") == {source.revision_id, changed.revision_id}
        assert not database.search_revision_ids("없는검색어")
        assert not database.search_revision_ids("%_")
