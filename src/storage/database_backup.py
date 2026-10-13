"""Single-file encrypted database backups and verified restoration."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from .database_credentials import (
    DatabaseCredentials,
    PasswordPolicyError,
    credential_path_for,
)
from .file_lock import ExclusiveFileLock, database_lock
from .encrypted_database import EncryptedDatabase


BACKUP_EXTENSION = ".mealbackup"
DEFAULT_MAX_BACKUPS = 100
_FORMAT = "meal-delivery-v2-backup"
_VERSION = 1
_DATABASE_MEMBER = "database.sqlite3"
_CREDENTIAL_MEMBER = "credentials.json"
_MANIFEST_MEMBER = "manifest.json"
_EXPECTED_MEMBERS = {
    _DATABASE_MEMBER,
    _CREDENTIAL_MEMBER,
    _MANIFEST_MEMBER,
}
_MAX_MANIFEST_BYTES = 64 * 1024
_MAX_CREDENTIAL_BYTES = 64 * 1024

logger = logging.getLogger(__name__)


class BackupDirectoryNotFoundError(FileNotFoundError):
    pass


class BackupAlreadyExistsError(FileExistsError):
    pass


class BackupFormatError(RuntimeError):
    pass


class BackupIntegrityError(RuntimeError):
    pass


class BackupRestoreConflictError(FileExistsError):
    pass


@dataclass(frozen=True)
class BackupRecord:
    path: Path
    created_at: datetime
    source_database_name: str
    database_id: UUID
    schema_version: int
    latest_revision_id: UUID | None
    database_size: int
    database_sha256: str
    credentials_sha256: str


@dataclass(frozen=True)
class BackupPasswordRotation:
    updated: tuple[Path, ...] = ()
    failed: tuple[tuple[Path, str], ...] = ()
    unreadable: tuple[Path, ...] = ()


class DatabaseBackupService:
    """Creates atomic archives from an open encrypted database."""

    def __init__(
        self,
        directory: str | Path,
        *,
        max_backups: int | None = DEFAULT_MAX_BACKUPS,
    ) -> None:
        self._directory = Path(directory).resolve()
        if max_backups is not None and (
            isinstance(max_backups, bool)
            or not isinstance(max_backups, int)
            or max_backups < 1
        ):
            raise ValueError("백업 보관 개수는 1 이상의 정수 또는 None이어야 합니다.")
        self._max_backups = max_backups

    @property
    def directory(self) -> Path:
        return self._directory

    @property
    def max_backups(self) -> int | None:
        return self._max_backups

    def create_backup(
        self,
        database: EncryptedDatabase,
        credentials: DatabaseCredentials,
        database_key: bytes,
        *,
        created_at: datetime | None = None,
        prune: bool = True,
    ) -> BackupRecord:
        if not self._directory.is_dir():
            raise BackupDirectoryNotFoundError(
                f"자동 백업 폴더가 없습니다: {self._directory}"
            )
        if credentials.path != credential_path_for(database.path):
            raise ValueError("DB와 비밀번호 정보 파일의 경로가 서로 맞지 않습니다.")

        timestamp = datetime.now(UTC) if created_at is None else created_at
        if timestamp.tzinfo is None:
            raise ValueError("백업 시각에는 timezone 정보가 필요합니다.")
        timestamp = timestamp.astimezone(UTC)
        safe_stem = _safe_stem(database.path.stem)
        suffix = uuid4().hex[:8]
        backup_name = (
            f"{safe_stem}_{timestamp.strftime('%Y%m%dT%H%M%SZ')}_{suffix}"
            f"{BACKUP_EXTENSION}"
        )
        final_path = self._directory / backup_name
        if final_path.exists():
            raise BackupAlreadyExistsError(f"백업 파일이 이미 있습니다: {final_path}")
        temporary_archive = self._directory / f".{backup_name}.{uuid4().hex}.tmp"

        latest = database.latest_revision()
        latest_revision_id = (
            None if latest is None else latest.metadata.revision_id
        )
        try:
            with tempfile.TemporaryDirectory(
                prefix=".mealbackup-",
                dir=self._directory,
            ) as staging_value:
                staging = Path(staging_value)
                database_file = staging / _DATABASE_MEMBER
                credential_file = staging / _CREDENTIAL_MEMBER
                database.backup_to(database_file, database_key)
                _write_bytes(credential_file, credentials.to_bytes())

                database_sha256 = _sha256_file(database_file)
                credentials_sha256 = _sha256_file(credential_file)
                manifest = {
                    "format": _FORMAT,
                    "version": _VERSION,
                    "created_at": timestamp.isoformat(),
                    "source_database_name": database.path.name,
                    "database_id": str(credentials.database_id),
                    "schema_version": database.schema_version,
                    "latest_revision_id": (
                        None
                        if latest_revision_id is None
                        else str(latest_revision_id)
                    ),
                    "database": {
                        "member": _DATABASE_MEMBER,
                        "size": database_file.stat().st_size,
                        "sha256": database_sha256,
                    },
                    "credentials": {
                        "member": _CREDENTIAL_MEMBER,
                        "size": credential_file.stat().st_size,
                        "sha256": credentials_sha256,
                    },
                }
                manifest_file = staging / _MANIFEST_MEMBER
                _write_bytes(manifest_file, _json_bytes(manifest))

                with zipfile.ZipFile(
                    temporary_archive,
                    mode="x",
                    compression=zipfile.ZIP_STORED,
                    allowZip64=True,
                ) as archive:
                    archive.write(database_file, _DATABASE_MEMBER)
                    archive.write(credential_file, _CREDENTIAL_MEMBER)
                    archive.write(manifest_file, _MANIFEST_MEMBER)
                _flush_file(temporary_archive)
                if final_path.exists():
                    raise BackupAlreadyExistsError(
                        f"백업 파일이 이미 있습니다: {final_path}"
                    )
                os.replace(temporary_archive, final_path)
        finally:
            temporary_archive.unlink(missing_ok=True)

        record = inspect_backup(final_path)
        if prune:
            try:
                self.prune_backups(record.database_id, protected_path=record.path)
            except OSError as error:
                logger.warning("새 백업은 생성했지만 오래된 자동 백업을 정리하지 못했습니다: %s", self._directory, exc_info=error)
        return record

    def rotate_passwords(
        self, database_id: UUID, database_key: bytes, new_password: str,
    ) -> BackupPasswordRotation:
        """Rewrap verified snapshots in this directory; each replacement is atomic."""
        if not self._directory.is_dir():
            raise BackupDirectoryNotFoundError(f"자동 백업 폴더가 없습니다: {self._directory}")
        updated: list[Path] = []
        failed: list[tuple[Path, str]] = []
        unreadable: list[Path] = []
        candidates = sorted(self._directory.glob(f"*{BACKUP_EXTENSION}"))
        for candidate in candidates:
            try:
                path = candidate.resolve(strict=True)
                if path.parent != self._directory or not path.is_file():
                    continue
                record = inspect_backup(path)
            except Exception:
                unreadable.append(candidate)
                continue
            if record.database_id != database_id:
                continue
            try:
                _rewrap_backup(path, record, database_key, new_password)
                updated.append(path)
            except Exception as error:
                failed.append((path, str(error)))
        return BackupPasswordRotation(tuple(updated), tuple(failed), tuple(unreadable))

    def list_backups(
        self,
        *,
        database_id: UUID | None = None,
    ) -> tuple[BackupRecord, ...]:
        if not self._directory.is_dir():
            raise BackupDirectoryNotFoundError(
                f"자동 백업 폴더가 없습니다: {self._directory}"
            )
        records: list[BackupRecord] = []
        for candidate in self._directory.glob(f"*{BACKUP_EXTENSION}"):
            try:
                path = candidate.resolve(strict=True)
                if path.parent != self._directory:
                    continue
                records.append(inspect_backup(path))
            except (
                BackupDirectoryNotFoundError,
                BackupFormatError,
                BackupIntegrityError,
                OSError,
            ):
                continue
        if database_id is not None:
            records = [
                record for record in records if record.database_id == database_id
            ]
        return tuple(
            sorted(
                records,
                key=lambda item: (item.created_at, item.path.name),
                reverse=True,
            )
        )

    def prune_backups(
        self,
        database_id: UUID,
        *,
        protected_path: str | Path | None = None,
    ) -> tuple[Path, ...]:
        """Keep the newest configured count for one DB lineage only."""

        if not isinstance(database_id, UUID):
            raise TypeError("database ID는 UUID여야 합니다.")
        if self._max_backups is None:
            return ()
        protected = (
            None if protected_path is None else Path(protected_path).resolve()
        )
        if protected is not None and protected.parent != self._directory:
            raise ValueError("보호할 백업 파일은 자동 백업 폴더 안에 있어야 합니다.")

        records = self.list_backups(database_id=database_id)
        keep: set[Path] = set()
        if protected is not None and any(
            record.path == protected for record in records
        ):
            keep.add(protected)
        for record in records:
            if len(keep) >= self._max_backups:
                break
            keep.add(record.path)

        removed: list[Path] = []
        for record in records:
            if record.path in keep:
                continue
            if (
                record.path.parent != self._directory
                or record.path.suffix.casefold() != BACKUP_EXTENSION
            ):
                raise RuntimeError("자동 백업 폴더 밖의 파일은 삭제할 수 없습니다.")
            record.path.unlink()
            removed.append(record.path)
        return tuple(removed)


def inspect_backup(path: str | Path) -> BackupRecord:
    backup_path = Path(path).resolve()
    if not backup_path.is_file():
        raise BackupDirectoryNotFoundError(f"백업 파일이 없습니다: {backup_path}")
    try:
        with zipfile.ZipFile(backup_path, mode="r") as archive:
            if archive.testzip() is not None:
                raise BackupIntegrityError("백업 압축 데이터가 손상되었습니다.")
            names = archive.namelist()
            if len(names) != len(_EXPECTED_MEMBERS) or set(names) != _EXPECTED_MEMBERS:
                raise BackupFormatError("백업 파일 구성이 올바르지 않습니다.")
            manifest_payload = _read_small_member(
                archive,
                _MANIFEST_MEMBER,
                _MAX_MANIFEST_BYTES,
            )
            credential_payload = _read_small_member(
                archive,
                _CREDENTIAL_MEMBER,
                _MAX_CREDENTIAL_BYTES,
            )
            manifest = _parse_manifest(manifest_payload)
            credentials = DatabaseCredentials.from_bytes(credential_payload)
            if str(credentials.database_id) != manifest["database_id"]:
                raise BackupIntegrityError(
                    "백업 DB와 비밀번호 정보의 식별자가 다릅니다."
                )

            database_info = archive.getinfo(_DATABASE_MEMBER)
            credential_info = archive.getinfo(_CREDENTIAL_MEMBER)
            if database_info.file_size != manifest["database_size"]:
                raise BackupIntegrityError("백업 DB 크기가 기록과 다릅니다.")
            if credential_info.file_size != manifest["credentials_size"]:
                raise BackupIntegrityError(
                    "백업 비밀번호 정보 크기가 기록과 다릅니다."
                )
            database_sha256 = _sha256_member(archive, _DATABASE_MEMBER)
            credentials_sha256 = _sha256_member(archive, _CREDENTIAL_MEMBER)
            if database_sha256 != manifest["database_sha256"]:
                raise BackupIntegrityError("백업 DB의 SHA-256이 기록과 다릅니다.")
            if credentials_sha256 != manifest["credentials_sha256"]:
                raise BackupIntegrityError(
                    "백업 비밀번호 정보의 SHA-256이 기록과 다릅니다."
                )
    except zipfile.BadZipFile as error:
        raise BackupFormatError("선택한 파일은 올바른 DB 백업이 아닙니다.") from error

    return BackupRecord(
        path=backup_path,
        created_at=manifest["created_at"],
        source_database_name=manifest["source_database_name"],
        database_id=UUID(manifest["database_id"]),
        schema_version=manifest["schema_version"],
        latest_revision_id=(
            None
            if manifest["latest_revision_id"] is None
            else UUID(manifest["latest_revision_id"])
        ),
        database_size=manifest["database_size"],
        database_sha256=database_sha256,
        credentials_sha256=credentials_sha256,
    )


def backup_has_recovery_password(path: str | Path) -> bool:
    """Check whether a validated backup contains a legacy recovery key slot."""
    record = inspect_backup(path)
    with zipfile.ZipFile(record.path, mode="r") as archive:
        payload = _read_small_member(archive, _CREDENTIAL_MEMBER, _MAX_CREDENTIAL_BYTES)
    return DatabaseCredentials.from_bytes(payload).recovery_slot is not None


def _rewrap_backup(
    path: Path, record: BackupRecord, database_key: bytes, new_password: str,
) -> None:
    """Validate the historical database with the active key before replacement."""
    with tempfile.TemporaryDirectory(prefix=".mealrewrap-", dir=path.parent) as staging_value:
        staging = Path(staging_value)
        database_file = staging / _DATABASE_MEMBER
        staged_archive = staging / path.name
        with zipfile.ZipFile(path, "r") as archive:
            credentials = DatabaseCredentials.from_bytes(archive.read(_CREDENTIAL_MEMBER))
            manifest = json.loads(archive.read(_MANIFEST_MEMBER).decode("utf-8"))
            _extract_member(archive, _DATABASE_MEMBER, database_file)
        if _sha256_file(database_file) != record.database_sha256:
            raise BackupIntegrityError("백업 DB 내용이 달라졌습니다.")
        # Opening an older schema can migrate it, so validate a disposable copy.
        validation_file = staging / "validation.sqlite3"
        shutil.copyfile(database_file, validation_file)
        with EncryptedDatabase.open(validation_file, database_key) as database:
            database.check_integrity()
            latest = database.latest_revision()
            if (database.schema_version < record.schema_version or
                (None if latest is None else latest.metadata.revision_id) != record.latest_revision_id):
                raise BackupIntegrityError("백업 DB 이력이 기록과 다릅니다.")
        if _sha256_file(database_file) != record.database_sha256:
            raise BackupIntegrityError("검증 중 백업 DB 내용이 달라졌습니다.")
        updated_credentials = credentials.with_user_password(database_key, new_password)
        credential_payload = updated_credentials.to_bytes()
        manifest["credentials"]["size"] = len(credential_payload)
        manifest["credentials"]["sha256"] = hashlib.sha256(credential_payload).hexdigest()
        with zipfile.ZipFile(staged_archive, "x", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
            archive.write(database_file, _DATABASE_MEMBER)
            archive.writestr(_CREDENTIAL_MEMBER, credential_payload)
            archive.writestr(_MANIFEST_MEMBER, _json_bytes(manifest))
        _flush_file(staged_archive)
        staged_record = inspect_backup(staged_archive)
        if (staged_record.database_sha256 != record.database_sha256 or
            staged_record.database_size != record.database_size or
            staged_record.created_at != record.created_at or
            staged_record.latest_revision_id != record.latest_revision_id or
            staged_record.database_id != record.database_id or
            staged_record.schema_version != record.schema_version):
            raise BackupIntegrityError("변경한 백업의 이력 또는 DB 내용이 달라졌습니다.")
        with zipfile.ZipFile(staged_archive, "r") as archive:
            staged_credentials = DatabaseCredentials.from_bytes(archive.read(_CREDENTIAL_MEMBER))
            if staged_credentials.unlock_user(new_password) != database_key:
                raise BackupIntegrityError("새 비밀번호로 백업을 열 수 없습니다.")
        os.replace(staged_archive, path)


def restore_backup_with_user_password(
    backup_path: str | Path,
    destination_database_path: str | Path,
    user_password: str,
    *,
    _lock: ExclusiveFileLock | None = None,
) -> Path:
    return _restore_backup(
        backup_path,
        destination_database_path,
        password=user_password,
        new_user_password=None,
        _lock=_lock,
        use_recovery=False,
    )


def restore_backup_with_recovery_password(
    backup_path: str | Path,
    destination_database_path: str | Path,
    recovery_password: str,
    new_user_password: str,
    *,
    _lock: ExclusiveFileLock | None = None,
) -> Path:
    if recovery_password == new_user_password:
        raise PasswordPolicyError(
            "새 비밀번호는 기관 복구 비밀번호와 다르게 정해주세요."
        )
    return _restore_backup(
        backup_path,
        destination_database_path,
        password=recovery_password,
        new_user_password=new_user_password,
        _lock=_lock,
        use_recovery=True,
    )


def _restore_backup(
    backup_path: str | Path,
    destination_database_path: str | Path,
    *,
    password: str,
    new_user_password: str | None,
    use_recovery: bool,
    _lock: ExclusiveFileLock | None = None,
) -> Path:
    owned = _lock is None
    lock = _lock if _lock is not None else database_lock(destination_database_path).acquire()
    try:
        record = inspect_backup(backup_path)
        destination = Path(destination_database_path).resolve()
        destination_credentials = credential_path_for(destination)
        if not destination.parent.is_dir():
            raise BackupDirectoryNotFoundError(
                f"복원할 DB의 상위 폴더가 없습니다: {destination.parent}"
            )
        if destination.exists() or destination_credentials.exists():
            raise BackupRestoreConflictError(
                "복원할 위치에 DB 또는 비밀번호 정보 파일이 이미 있습니다."
            )

        credential_installed = False
        database_installed = False
        with tempfile.TemporaryDirectory(
            prefix=".mealrestore-",
            dir=destination.parent,
        ) as staging_value:
            staging = Path(staging_value)
            staged_database = staging / destination.name
            staged_credentials = staging / destination_credentials.name
            try:
                with zipfile.ZipFile(record.path, mode="r") as archive:
                    credential_payload = archive.read(_CREDENTIAL_MEMBER)
                    credentials = DatabaseCredentials.from_bytes(credential_payload)
                    key = (
                        credentials.unlock_recovery(password)
                        if use_recovery
                        else credentials.unlock_user(password)
                    )
                    if use_recovery:
                        assert new_user_password is not None
                        credentials = credentials.with_user_password(
                            key,
                            new_user_password,
                        )
                    _extract_member(archive, _DATABASE_MEMBER, staged_database)
                    _write_bytes(staged_credentials, credentials.to_bytes())

                if _sha256_file(staged_database) != record.database_sha256:
                    raise BackupIntegrityError("복원 중 백업 DB 내용이 달라졌습니다.")
                with EncryptedDatabase.open(staged_database, key) as restored:
                    restored.check_integrity()
                if destination.exists() or destination_credentials.exists():
                    raise BackupRestoreConflictError(
                        "복원할 위치에 DB 또는 비밀번호 정보 파일이 이미 있습니다."
                    )

                os.replace(staged_credentials, destination_credentials)
                credential_installed = True
                os.replace(staged_database, destination)
                database_installed = True
                with EncryptedDatabase.open(destination, key) as restored:
                    restored.check_integrity()
            except Exception:
                if database_installed:
                    destination.unlink(missing_ok=True)
                if credential_installed:
                    destination_credentials.unlink(missing_ok=True)
                raise
        return destination
    finally:
        if owned:
            lock.close()


def _parse_manifest(payload: bytes) -> dict[str, object]:
    try:
        document = json.loads(payload.decode("utf-8"))
        if not isinstance(document, dict):
            raise TypeError
        if document.get("format") != _FORMAT or document.get("version") != _VERSION:
            raise ValueError
        created_at = datetime.fromisoformat(_required_text(document, "created_at"))
        if created_at.tzinfo is None:
            raise ValueError
        source_name = _required_text(document, "source_database_name")
        if Path(source_name).name != source_name:
            raise ValueError
        database_id = str(UUID(_required_text(document, "database_id")))
        schema_version = _required_int(document, "schema_version")
        latest_value = document.get("latest_revision_id")
        if latest_value is not None:
            latest_value = str(UUID(_required_text(document, "latest_revision_id")))

        database = document.get("database")
        credentials = document.get("credentials")
        if not isinstance(database, dict) or not isinstance(credentials, dict):
            raise TypeError
        if database.get("member") != _DATABASE_MEMBER:
            raise ValueError
        if credentials.get("member") != _CREDENTIAL_MEMBER:
            raise ValueError
        database_size = _required_nonnegative_int(database, "size")
        credentials_size = _required_nonnegative_int(credentials, "size")
        database_sha256 = _required_sha256(database, "sha256")
        credentials_sha256 = _required_sha256(credentials, "sha256")
    except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError) as error:
        raise BackupFormatError("백업 manifest 형식이 올바르지 않습니다.") from error
    return {
        "created_at": created_at.astimezone(UTC),
        "source_database_name": source_name,
        "database_id": database_id,
        "schema_version": schema_version,
        "latest_revision_id": latest_value,
        "database_size": database_size,
        "credentials_size": credentials_size,
        "database_sha256": database_sha256,
        "credentials_sha256": credentials_sha256,
    }


def _safe_stem(value: str) -> str:
    cleaned = re.sub(r"[^\w.-]+", "_", value, flags=re.UNICODE).strip("._")
    return cleaned or "database"


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )


def _required_text(value: dict[str, object], key: str) -> str:
    found = value[key]
    if not isinstance(found, str) or not found:
        raise TypeError
    return found


def _required_int(value: dict[str, object], key: str) -> int:
    found = value[key]
    if isinstance(found, bool) or not isinstance(found, int):
        raise TypeError
    return found


def _required_nonnegative_int(value: dict[str, object], key: str) -> int:
    found = _required_int(value, key)
    if found < 0:
        raise ValueError
    return found


def _required_sha256(value: dict[str, object], key: str) -> str:
    found = _required_text(value, key)
    if re.fullmatch(r"[0-9a-f]{64}", found) is None:
        raise ValueError
    return found


def _read_small_member(
    archive: zipfile.ZipFile,
    name: str,
    maximum_size: int,
) -> bytes:
    info = archive.getinfo(name)
    if info.file_size > maximum_size:
        raise BackupFormatError(f"백업의 {name} 항목이 비정상적으로 큽니다.")
    return archive.read(name)


def _sha256_member(archive: zipfile.ZipFile, name: str) -> str:
    digest = hashlib.sha256()
    with archive.open(name, mode="r") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _extract_member(
    archive: zipfile.ZipFile,
    member: str,
    destination: Path,
) -> None:
    with archive.open(member, mode="r") as source, destination.open("xb") as output:
        shutil.copyfileobj(source, output, length=1024 * 1024)
        output.flush()
        os.fsync(output.fileno())


def _write_bytes(path: Path, payload: bytes) -> None:
    with path.open("xb") as output:
        output.write(payload)
        output.flush()
        os.fsync(output.fileno())


def _flush_file(path: Path) -> None:
    with path.open("r+b") as value:
        os.fsync(value.fileno())
