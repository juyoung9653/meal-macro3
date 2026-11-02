"""One encrypted backup database, advanced only by committed source records."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import tempfile
from uuid import UUID

from .database_credentials import DatabaseCredentials, credential_path_for, _write_file
from .encrypted_database import EncryptedDatabase, DatabaseIntegrityError
from .file_lock import database_lock

REPLICA_EXTENSION = ".mealreplica"
logger = logging.getLogger(__name__)


class _RestoreReplayError(Exception):
    """A committed restore could not be replayed into the replica."""


@dataclass(frozen=True)
class ReplicaBackupRecord:
    path: Path
    created_at: datetime
    source_database_name: str
    database_id: UUID
    schema_version: int
    latest_revision_id: UUID | None
    database_size: int
    format_version: int = 3
    backup_kind: str = "replica"


def _canonical(document):
    return json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _signature(document, key):
    return hmac.new(key, b"meal-replica-v1\0" + _canonical(document), hashlib.sha256).hexdigest()


def _read_manifest(path, key=None):
    sidecar = credential_path_for(path)
    if sidecar.stat().st_size > 64 * 1024:
        raise DatabaseIntegrityError("백업 비밀번호 정보 파일이 너무 큽니다.")
    document = json.loads(sidecar.read_bytes())
    signature = document.pop("replica_hmac", None)
    if key is not None and (not isinstance(signature, str) or not hmac.compare_digest(signature, _signature(document, key))):
        raise DatabaseIntegrityError("백업 DB 정보의 인증 값이 일치하지 않습니다.")
    if document.get("replica", {}).get("version") != 1:
        raise DatabaseIntegrityError("별도 백업 DB 정보가 올바르지 않습니다.")
    DatabaseCredentials.from_bytes(_canonical(document))
    return document


def inspect_replica(path):
    path = Path(path).resolve(strict=True)
    document = _read_manifest(path)
    info = document["replica"]
    return ReplicaBackupRecord(path, datetime.fromisoformat(info["updated_at"]),
        info["source_database_name"], UUID(document["database_id"]), int(info["schema_version"]),
        UUID(info["latest_revision_id"]) if info["latest_revision_id"] else None, path.stat().st_size)


def _write_manifest(path, source, credentials, key, timestamp):
    document = json.loads(credentials.to_bytes())
    latest = source.latest_revision_id()
    document["replica"] = {
        "version": 1, "source_database_name": source.path.name,
        "updated_at": timestamp.astimezone(UTC).isoformat(),
        "schema_version": source.schema_version,
        "latest_revision_id": str(latest) if latest else None,
    }
    document["replica_hmac"] = _signature(document, key)
    sidecar = credential_path_for(path)
    _write_file(sidecar, _canonical(document), replace_existing=sidecar.exists())


class DatabaseReplicaService:
    def __init__(self, directory):
        self._directory = Path(directory).resolve()
        self._replica = None
        self._file_identity = None
        self._cleaned = set()

    @property
    def directory(self):
        return self._directory

    def clear_cache(self):
        if self._replica is not None:
            self._replica.close()
        self._replica = None
        self._file_identity = None

    @staticmethod
    def _identity(path):
        stat = path.stat()
        return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns

    def create_backup(self, database, credentials, database_key, *, created_at=None, prune=True):
        from .database_backup import BackupDirectoryNotFoundError
        if not self.directory.is_dir():
            raise BackupDirectoryNotFoundError(f"자동 백업 폴더가 없습니다: {self.directory}")
        if credentials.path != credential_path_for(database.path):
            raise ValueError("DB와 비밀번호 정보 파일의 경로가 서로 맞지 않습니다.")
        timestamp = created_at or datetime.now(UTC)
        if timestamp.tzinfo is None:
            raise ValueError("백업 시각에는 timezone 정보가 필요합니다.")
        path = self.directory / f"{credentials.database_id}{REPLICA_EXTENSION}"
        if path.resolve() == database.path:
            raise ValueError("운영 DB와 백업 DB는 다른 파일이어야 합니다.")
        with database_lock(path):
            source = database._require_connection()
            if not source.get_autocommit():
                raise RuntimeError("진행 중인 DB transaction은 백업할 수 없습니다.")
            source.execute("BEGIN")
            try:
                if not database._is_verified():
                    database.check_integrity()
                if self._replica is not None and (self._replica.path != path or not path.exists()
                        or self._file_identity != self._identity(path)):
                    self.clear_cache()
                if not path.exists():
                    with tempfile.TemporaryDirectory(prefix=".replica-", dir=self.directory) as folder:
                        staged = Path(folder) / path.name
                        database.backup_to(staged, database_key)
                        # Existing backups are not touched until this copy and its
                        # credentials have been published and checked.
                        os.replace(staged, path)
                if credential_path_for(path).exists():
                    document = _read_manifest(path, database_key)
                    if document["database_id"] != str(credentials.database_id):
                        raise DatabaseIntegrityError("백업 DB 식별자가 원본과 다릅니다.")
                if self._replica is None:
                    self._replica = EncryptedDatabase.open(path, database_key)
                replica = self._replica
                if not replica._is_verified():
                    replica.check_integrity()
                try:
                    self._sync(database, replica)
                except _RestoreReplayError:
                    # Restore records describe a complete historical state. If
                    # replay rejects that diff, validate the source and stage an
                    # exact encrypted copy instead of retrying the same diff.
                    # Keep the existing file until the replacement is verified.
                    self.clear_cache()
                    with tempfile.TemporaryDirectory(prefix=".replica-repair-", dir=self.directory) as folder:
                        staged = Path(folder) / path.name
                        database.backup_to(staged, database_key)
                        os.replace(staged, path)
                    self._replica = EncryptedDatabase.open(path, database_key)
                    logger.info("복구 이력의 증분 반영 대신 검증된 원본으로 백업 DB를 갱신했습니다.")
                _write_manifest(path, database, credentials, database_key, timestamp)
                _read_manifest(path, database_key)
                self._file_identity = self._identity(path)
                result = inspect_replica(path)
            except BaseException:
                self.clear_cache()
                raise
            finally:
                source.execute("ROLLBACK")
        if prune and credentials.database_id not in self._cleaned:
            self._remove_legacy(credentials.database_id, database)
        return result

    @staticmethod
    def _sync(source, replica):
        origin, target = source._require_connection(), replica._require_connection()
        head = target.execute("SELECT sequence, revision_id FROM revision ORDER BY sequence DESC LIMIT 1").fetchone()
        sequence, head_id = head if head else (0, None)
        if head and origin.execute("SELECT revision_id FROM revision WHERE sequence=?", (sequence,)).fetchone() != (head_id,):
            raise DatabaseIntegrityError("백업 DB와 원본의 변경 이력이 갈라졌습니다. 다른 백업 폴더를 선택해주세요.")
        for (revision_id,) in tuple(origin.execute("SELECT revision_id FROM revision WHERE sequence>? ORDER BY sequence", (sequence,))):
            record = source.revision(UUID(revision_id))
            try:
                replica.append_revision(replace(record, metadata=replace(record.metadata, display_code=None)), replay=True)
            except ValueError as error:
                if "snapshot_restore_target" in record.metadata.decisions:
                    raise _RestoreReplayError() from error
                raise
            if replica.revision(record.metadata.revision_id) != record:
                raise DatabaseIntegrityError("새 변경 기록이 백업 DB에 정확히 반영되지 않았습니다.")
        head = target.execute("SELECT sequence, run_id FROM document_run ORDER BY sequence DESC LIMIT 1").fetchone()
        sequence, run_id = head if head else (0, None)
        if head and origin.execute("SELECT run_id FROM document_run WHERE sequence=?", (sequence,)).fetchone() != (run_id,):
            raise DatabaseIntegrityError("백업 DB와 원본의 문서 생성 이력이 다릅니다.")
        for (run_id,) in tuple(origin.execute("SELECT run_id FROM document_run WHERE sequence>? ORDER BY sequence", (sequence,))):
            run = source.document_run(UUID(run_id))
            replica.append_document_run(run)
            if replica.document_run(run.run_id) != run:
                raise DatabaseIntegrityError("문서 생성 기록이 백업 DB에 정확히 반영되지 않았습니다.")
        # Appearance is a small per-user table, separate from immutable revisions.
        columns = "actor_sid, actor_username, computer_name, updated_at, layout_json"
        rows = tuple(origin.execute(f"SELECT {columns} FROM user_view_layout ORDER BY actor_sid"))
        if rows != tuple(target.execute(f"SELECT {columns} FROM user_view_layout ORDER BY actor_sid")):
            with target:
                target.execute("DELETE FROM user_view_layout")
                target.executemany(f"INSERT INTO user_view_layout({columns}) VALUES (?, ?, ?, ?, ?)", rows)
                if tuple(target.execute(f"SELECT {columns} FROM user_view_layout ORDER BY actor_sid")) != rows:
                    raise DatabaseIntegrityError("화면 설정 백업이 원본과 다릅니다.")

    def _remove_legacy(self, database_id, database):
        from .database_backup import DatabaseBackupService
        failed = False
        records = DatabaseBackupService(self.directory).list_backups(database_id=database_id)
        # A restored older source may share the same database ID but not contain
        # newer archived history. Keep that archive and all of its dependencies.
        for record in records:
            if record.latest_revision_id is not None and database._require_connection().execute(
                "SELECT 1 FROM revision WHERE revision_id=?", (str(record.latest_revision_id),)).fetchone() is None:
                return
        for record in records:
            try:
                record.path.unlink()
            except OSError:
                failed = True
                logger.warning("새 백업은 완료됐지만 이전 백업을 지우지 못했습니다: %s", record.path)
        if not failed:
            self._cleaned.add(database_id)

    def rotate_passwords(self, database_id, database_key, new_password):
        from .database_backup import BackupPasswordRotation
        path = self.directory / f"{database_id}{REPLICA_EXTENSION}"
        if not path.exists():
            return BackupPasswordRotation()
        with database_lock(path):
            document = _read_manifest(path, database_key)
            credentials = DatabaseCredentials.from_bytes(_canonical(document)).with_user_password(database_key, new_password)
            updated = json.loads(credentials.to_bytes())
            updated["replica"] = document["replica"]
            updated["replica_hmac"] = _signature(updated, database_key)
            _write_file(credential_path_for(path), _canonical(updated), replace_existing=True)
        return BackupPasswordRotation(updated=(path,))


def restore_replica(path, destination, password, *, use_recovery=False, new_user_password=None, _lock=None):
    from .database_backup import BackupRestoreConflictError
    path, destination = Path(path).resolve(), Path(destination).resolve()
    owned = _lock is None
    lock = _lock or database_lock(destination).acquire()
    installed_db = installed_credentials = False
    try:
        if destination.exists() or credential_path_for(destination).exists():
            raise BackupRestoreConflictError("복원할 위치에 DB 또는 비밀번호 정보 파일이 이미 있습니다.")
        with database_lock(path):
            document = _read_manifest(path)
            credentials = DatabaseCredentials.from_bytes(_canonical(document))
            key = credentials.unlock_recovery(password) if use_recovery else credentials.unlock_user(password)
            _read_manifest(path, key)
            if use_recovery:
                credentials = credentials.with_user_password(key, new_user_password)
            with tempfile.TemporaryDirectory(prefix=".replica-restore-", dir=destination.parent) as folder:
                staged = Path(folder) / destination.name
                with EncryptedDatabase.open(path, key) as backup:
                    advertised = document["replica"]["latest_revision_id"]
                    if advertised is not None:
                        backup.revision(UUID(advertised))
                    backup.backup_to(staged, key)
                if destination.exists() or credential_path_for(destination).exists():
                    raise BackupRestoreConflictError("복원할 위치에 파일이 이미 있습니다.")
                credentials.with_path(credential_path_for(destination)).write_new()
                installed_credentials = True
                os.replace(staged, destination)
                installed_db = True
        return destination
    except BaseException:
        if installed_db:
            destination.unlink(missing_ok=True)
        if installed_credentials:
            credential_path_for(destination).unlink(missing_ok=True)
        raise
    finally:
        if owned:
            lock.close()
