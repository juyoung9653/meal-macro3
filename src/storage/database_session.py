"""Application session that joins password access, DB writes, and auto backup."""

from __future__ import annotations

import hmac
import secrets
from dataclasses import dataclass, replace
from pathlib import Path

from src.domain.documents import DocumentRun
from src.domain.revisions import RevisionRecord

from .database_backup import (
    BackupPasswordRotation,
    BackupRecord,
    restore_backup_with_recovery_password,
    restore_backup_with_user_password,
)
from .database_replica import DatabaseReplicaService, ReplicaBackupRecord
from .backup_diagnostics import BackupDiagnostics
from .database_credentials import (
    CredentialAlreadyExistsError,
    CredentialFormatError,
    DatabaseCredentials,
    credential_path_for,
)
from .file_lock import ExclusiveFileLock, database_lock
from .encrypted_database import DatabaseAlreadyExistsError, EncryptedDatabase


_DATABASE_KEY_BYTES = 32


@dataclass(frozen=True)
class AutomaticBackupResult:
    """A DB change has succeeded; this separately reports its backup outcome."""

    record: BackupRecord | ReplicaBackupRecord | None = None
    error: Exception | None = None
    rotation: BackupPasswordRotation | None = None
    rotation_error: Exception | None = None
    notify_failure: bool = False
    log_path: Path | None = None

    def __post_init__(self) -> None:
        if self.record is not None and self.error is not None:
            raise ValueError("백업 결과와 오류를 동시에 지정할 수 없습니다.")

    @property
    def warning_required(self) -> bool:
        return self.error is not None and self.notify_failure

    @property
    def attempted(self) -> bool:
        return self.record is not None or self.error is not None

    @property
    def succeeded(self) -> bool:
        return self.record is not None


class DatabaseSession:
    """Owns the unlocked key for one application session.

    The low-level encrypted Adapter still never stores the key.  This session
    keeps a best-effort erasable bytearray because online backups need the key
    to create another encrypted SQLite file.
    """

    def __init__(
        self,
        database: EncryptedDatabase,
        credentials: DatabaseCredentials,
        database_key: bytes,
        *,
        backup_directory: str | Path | None = None,
        _lock: ExclusiveFileLock | None = None,
    ) -> None:
        if len(database_key) != _DATABASE_KEY_BYTES:
            raise ValueError(f"DB key는 {_DATABASE_KEY_BYTES} bytes여야 합니다.")
        self._database = database
        self._credentials = credentials
        self._database_key = bytearray(database_key)
        self._backup_service = (
            None
            if backup_directory is None
            else DatabaseReplicaService(backup_directory)
        )
        self._closed = False
        self.last_committed_roster = None
        self._backup_diagnostics = BackupDiagnostics(database.path, credentials.database_id)
        self._startup_backup_result = None
        self._locks = [] if _lock is None else [_lock]

    @classmethod
    def create(
        cls,
        database_path: str | Path,
        user_password: str,
        recovery_password: str | None = None,
        *,
        backup_directory: str | Path | None = None,
        _lock: ExclusiveFileLock | None = None,
    ) -> DatabaseSession:
        owned = _lock is None
        lock = _lock if _lock is not None else database_lock(database_path).acquire()
        try:
            path = Path(database_path).resolve()
            sidecar = credential_path_for(path)
            if path.exists():
                raise DatabaseAlreadyExistsError(f"DB 파일이 이미 있습니다: {path}")
            if sidecar.exists():
                raise CredentialAlreadyExistsError(
                    f"DB 비밀번호 정보가 이미 있습니다: {sidecar}"
                )

            key = secrets.token_bytes(_DATABASE_KEY_BYTES)
            database = EncryptedDatabase.create(path, key)
            try:
                credentials, returned_key = DatabaseCredentials.create(
                    path,
                    user_password,
                    recovery_password,
                    database_key=key,
                )
                if not hmac.compare_digest(key, returned_key):
                    raise CredentialFormatError("생성된 DB key가 일치하지 않습니다.")
            except Exception:
                database.close()
                _remove_created_database(path)
                raise
            return cls._from_database(
                database,
                credentials,
                key,
                backup_directory=backup_directory,
                _lock=lock,
            )
        except BaseException:
            if owned:
                lock.close()
            raise

    @classmethod
    def open(
        cls,
        database_path: str | Path,
        user_password: str,
        *,
        backup_directory: str | Path | None = None,
        _lock: ExclusiveFileLock | None = None,
    ) -> DatabaseSession:
        owned = _lock is None
        lock = _lock if _lock is not None else database_lock(database_path).acquire()
        try:
            path = Path(database_path).resolve()
            credentials = DatabaseCredentials.load(path)
            key = credentials.unlock_user(user_password)
            database = EncryptedDatabase.open(path, key)
            return cls._from_database(
                database,
                credentials,
                key,
                backup_directory=backup_directory,
                _lock=lock,
            )
        except BaseException:
            if owned:
                lock.close()
            raise

    @classmethod
    def recover_and_open(
        cls,
        database_path: str | Path,
        recovery_password: str,
        new_user_password: str,
        *,
        backup_directory: str | Path | None = None,
        _lock: ExclusiveFileLock | None = None,
    ) -> DatabaseSession:
        owned = _lock is None
        lock = _lock if _lock is not None else database_lock(database_path).acquire()
        try:
            path = Path(database_path).resolve()
            credentials = DatabaseCredentials.load(path)
            key = credentials.unlock_recovery(recovery_password)
            database = EncryptedDatabase.open(path, key)
            try:
                updated = credentials.reset_user_password(
                    recovery_password,
                    new_user_password,
                )
            except Exception:
                database.close()
                raise
            return cls._from_database(
                database,
                updated,
                key,
                backup_directory=backup_directory,
                _lock=lock,
            )
        except BaseException:
            if owned:
                lock.close()
            raise

    @classmethod
    def restore_and_open_with_user_password(
        cls,
        backup_path: str | Path,
        destination_database_path: str | Path,
        user_password: str,
        *,
        backup_directory: str | Path | None = None,
        _lock: ExclusiveFileLock | None = None,
    ) -> DatabaseSession:
        owned = _lock is None
        lock = _lock if _lock is not None else database_lock(destination_database_path).acquire()
        try:
            restored = restore_backup_with_user_password(
                backup_path,
                destination_database_path,
                user_password,
                _lock=lock,
            )
            return cls.open(
                restored,
                user_password,
                backup_directory=backup_directory,
                _lock=lock,
            )
        except BaseException:
            if owned:
                lock.close()
            raise

    @classmethod
    def restore_and_open_with_recovery_password(
        cls,
        backup_path: str | Path,
        destination_database_path: str | Path,
        recovery_password: str,
        new_user_password: str,
        *,
        backup_directory: str | Path | None = None,
        _lock: ExclusiveFileLock | None = None,
    ) -> DatabaseSession:
        owned = _lock is None
        lock = _lock if _lock is not None else database_lock(destination_database_path).acquire()
        try:
            restored = restore_backup_with_recovery_password(
                backup_path,
                destination_database_path,
                recovery_password,
                new_user_password,
                _lock=lock,
            )
            return cls.open(
                restored,
                new_user_password,
                backup_directory=backup_directory,
                _lock=lock,
            )
        except BaseException:
            if owned:
                lock.close()
            raise

    @classmethod
    def _from_database(cls, database, credentials, key, **kwargs) -> DatabaseSession:
        try:
            return cls(database, credentials, key, **kwargs)
        except BaseException:
            database.close()
            raise

    def retain_lock(self, lock: ExclusiveFileLock) -> None:
        """Keep a successfully relocated destination reserved until shutdown."""
        self._require_open()
        self._locks.append(lock)

    @property
    def database(self) -> EncryptedDatabase:
        self._require_open()
        return self._database

    @property
    def path(self) -> Path:
        return self._database.path

    @property
    def credentials(self) -> DatabaseCredentials:
        self._require_open()
        return self._credentials

    @property
    def backup_directory(self) -> Path | None:
        return (
            None
            if self._backup_service is None
            else self._backup_service.directory
        )

    def append_revision(self, record: RevisionRecord) -> AutomaticBackupResult:
        """Commit first, then report backup failure without implying data loss."""

        self._require_open()
        self.last_committed_roster = self._database.append_revision(record)
        return self.try_backup_now()

    def append_document_run(self, run: DocumentRun) -> AutomaticBackupResult:
        """Record a generated document, then make the usual verified backup."""

        self._require_open()
        self._database.append_document_run(run)
        return self.try_backup_now()

    def backup_now(self) -> ReplicaBackupRecord:
        self._require_open()
        if self._backup_service is None:
            raise RuntimeError("자동 백업 폴더가 설정되지 않았습니다.")
        self._credentials = DatabaseCredentials.load(self.path)
        record = self._backup_service.create_backup(
            self._database,
            self._credentials,
            self._key_bytes(),
        )
        self._backup_diagnostics.succeeded()
        return record

    def prepare_backup_directory(self, directory: str | Path) -> DatabaseReplicaService:
        """Verify a candidate service with a backup before it becomes active."""
        self._require_open()
        replacement = DatabaseReplicaService(directory)
        credentials = DatabaseCredentials.load(self.path)
        try:
            replacement.create_backup(
                self._database, credentials, self._key_bytes(), prune=False,
            )
        except BaseException:
            replacement.clear_cache()
            raise
        replacement.clear_cache()
        return replacement

    def activate_backup_directory(self, replacement: DatabaseReplicaService) -> None:
        """Activate a service already checked by prepare_backup_directory."""
        previous = self._backup_service
        self._backup_service = replacement
        self._backup_diagnostics.succeeded()
        if previous is not None and previous is not replacement:
            previous.clear_cache()

    def try_backup_now(self, *, _startup_retry=False) -> AutomaticBackupResult:
        self._require_open()
        if self._backup_service is None:
            return AutomaticBackupResult()
        try:
            record = self.backup_now()
        except Exception as error:
            log_path = self._backup_diagnostics.failed(error, self.backup_directory, startup_retry=_startup_retry)
            return AutomaticBackupResult(error=error, log_path=log_path)
        return AutomaticBackupResult(record=record)

    def retry_backup_on_open(self) -> AutomaticBackupResult:
        """Catch up on open; warn only if an earlier session also failed."""
        self._require_open()
        if self._startup_backup_result is None:
            result = self.try_backup_now(_startup_retry=True)
            self._startup_backup_result = replace(
                result, notify_failure=result.error is not None and self._backup_diagnostics.pending_from_previous_session)
        return self._startup_backup_result

    def change_user_password(
        self,
        current_password: str,
        new_password: str,
    ) -> AutomaticBackupResult:
        self._require_open()
        current = DatabaseCredentials.load(self.path)
        unlocked = current.unlock_user(current_password)
        if not hmac.compare_digest(unlocked, self._key_bytes()):
            raise CredentialFormatError("DB와 비밀번호 정보의 key가 일치하지 않습니다.")
        if current_password == new_password:
            return AutomaticBackupResult()
        self._credentials = current.change_user_password(
            current_password,
            new_password,
        )
        rotation = None
        rotation_error = None
        if self._backup_service is not None:
            try:
                rotation = self._backup_service.rotate_passwords(
                    current.database_id, self._key_bytes(), new_password,
                )
            except Exception as error:
                rotation_error = error
        if self._backup_service is None:
            return AutomaticBackupResult()
        try:
            record = self._backup_service.create_backup(
                self._database, self._credentials, self._key_bytes(), prune=False,
            )
            self._backup_diagnostics.succeeded()
            return AutomaticBackupResult(record=record, rotation=rotation, rotation_error=rotation_error)
        except Exception as error:
            log_path = self._backup_diagnostics.failed(error, self.backup_directory)
            return AutomaticBackupResult(error=error, rotation=rotation, rotation_error=rotation_error, log_path=log_path)

    def close(self) -> None:
        if self._closed:
            return
        try:
            self._database.close()
        finally:
            if self._backup_service is not None:
                self._backup_service.clear_cache()
            for index in range(len(self._database_key)):
                self._database_key[index] = 0
            self._closed = True
            for lock in self._locks:
                lock.close()
            self._locks.clear()

    def __enter__(self) -> DatabaseSession:
        self._require_open()
        return self

    def __exit__(self, _exc_type, _exc_value, _traceback) -> None:
        self.close()

    def _key_bytes(self) -> bytes:
        self._require_open()
        return bytes(self._database_key)

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("DB session이 이미 닫혔습니다.")


def _remove_created_database(path: Path) -> None:
    for candidate in (
        path,
        Path(f"{path}-journal"),
        Path(f"{path}-wal"),
        Path(f"{path}-shm"),
    ):
        candidate.unlink(missing_ok=True)


def committed_roster_after_save(session, prepared_roster):
    """Use the expanded snapshot captured before commit, with no fallible reread."""
    return getattr(session, "last_committed_roster", None) or prepared_roster
