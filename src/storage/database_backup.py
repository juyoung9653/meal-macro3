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
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from .database_credentials import (
    DatabaseCredentials,
    CredentialFormatError,
    PasswordPolicyError,
    credential_path_for,
)
from .file_lock import ExclusiveFileLock, database_lock
from .encrypted_database import EncryptedDatabase
from src.storage import backup_snapshot
from .migrations import CURRENT_SCHEMA_VERSION


BACKUP_EXTENSION = ".mealbackup"
DEFAULT_MAX_BACKUPS = 100
_FORMAT = "meal-delivery-v2-backup"
_VERSION = 1
_SNAPSHOT_VERSION = 2
FULL_BACKUP_INTERVAL = timedelta(days=7)
MAX_INCREMENTAL_CHAIN = 32
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
    format_version: int = 1
    backup_kind: str = "full"
    backup_id: UUID | None = None
    parent_backup_id: UUID | None = None
    parent_database_sha256: str | None = None
    full_backup_id: UUID | None = None
    full_created_at: datetime | None = None
    snapshot_size: int | None = None
    snapshot_sha256: str | None = None
    authenticated_manifest_sha256: str | None = None


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
        self._snapshot_cache = None

    def clear_cache(self) -> None:
        """Release the one plaintext in-memory image when its session ends."""
        self._snapshot_cache = None

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
        force_full: bool = False,
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

        snapshot = database.snapshot_bytes()
        latest_revision_id = database.latest_revision_id()
        compressed = None
        parent = None
        previous_snapshot = None
        verified_chain = ()
        # Reuse discovery during preparation; recheck dependencies before publication.
        records = self.list_backups() if not force_full else ()
        if not force_full:
            predecessor = next((item for item in records
                                if item.database_id == credentials.database_id), None)
            if (predecessor is not None and predecessor.format_version == 2
                    and predecessor.schema_version == database.schema_version
                    and predecessor.created_at <= timestamp
                    and predecessor.full_created_at is not None
                    and timestamp - predecessor.full_created_at < FULL_BACKUP_INTERVAL):
                try:
                    chain = _snapshot_chain(predecessor, records=records)
                    if len(chain) < MAX_INCREMENTAL_CHAIN:
                        cached = self._snapshot_cache
                        key_digest = hashlib.sha256(database_key).digest()
                        # list_backups freshly hashes disk payloads and manifests.
                        # Reuse decoded bytes only for that identical authenticated
                        # chain; size/mtime alone must never authorize this cache.
                        if cached is not None and cached[:2] == (key_digest, chain):
                            previous_snapshot = cached[2]
                        else:
                            previous_snapshot = _reconstruct_snapshot(predecessor, database_key, records=records)
                        delta = backup_snapshot.compress_delta(previous_snapshot, snapshot)
                        if len(delta) > max(4096, len(snapshot) // 100):
                            compressed = backup_snapshot.compress_full(snapshot)
                        if compressed is None or len(delta) < len(compressed):
                            compressed, parent = delta, predecessor
                            verified_chain = chain
                except (BackupFormatError, BackupIntegrityError, OSError):
                    logger.warning("이전 백업을 검증할 수 없어 전체 백업을 생성합니다: %s", predecessor.path)
        if compressed is None:
            compressed = backup_snapshot.compress_full(snapshot)
        backup_id = uuid4()
        credential_payload = credentials.to_bytes()
        manifest = {
            "format": _FORMAT, "version": _SNAPSHOT_VERSION,
            "created_at": timestamp.isoformat(),
            "source_database_name": database.path.name,
            "database_id": str(credentials.database_id),
            "schema_version": database.schema_version,
            "latest_revision_id": None if latest_revision_id is None else str(latest_revision_id),
            "backup_kind": "full" if parent is None else "incremental",
            "backup_id": str(backup_id),
            "parent_backup_id": None if parent is None else str(parent.backup_id),
            "parent_database_sha256": None if parent is None else parent.snapshot_sha256,
            "full_backup_id": str(backup_id if parent is None else parent.full_backup_id),
            "full_created_at": (timestamp if parent is None else parent.full_created_at).isoformat(),
            "snapshot": {"size": len(snapshot), "sha256": hashlib.sha256(snapshot).hexdigest()},
            "credentials": {"member": _CREDENTIAL_MEMBER, "size": len(credential_payload),
                            "sha256": hashlib.sha256(credential_payload).hexdigest()},
        }
        payload = backup_snapshot.encrypt(compressed, database_key, manifest)
        manifest["database"] = {"member": _DATABASE_MEMBER, "size": len(payload),
                                "sha256": hashlib.sha256(payload).hexdigest()}
        try:
            with zipfile.ZipFile(temporary_archive, "x", compression=zipfile.ZIP_STORED,
                                 allowZip64=True) as archive:
                archive.writestr(_DATABASE_MEMBER, payload)
                archive.writestr(_CREDENTIAL_MEMBER, credential_payload)
                archive.writestr(_MANIFEST_MEMBER, _json_bytes(manifest))
            _flush_file(temporary_archive)
            staged_record = inspect_backup(temporary_archive)
            # Dependencies can be deleted/replaced externally while we write the
            # staged archive. Rehash their on-disk payloads before reusing the
            # authenticated parent bytes; identical backup copies are equivalent.
            current_chain = _snapshot_chain(staged_record)[:-1]
            if len(current_chain) != len(verified_chain) or any(
                replace(current, path=verified.path,
                        credentials_sha256=verified.credentials_sha256) != verified
                for current, verified in zip(current_chain, verified_chain)
            ):
                raise BackupIntegrityError("백업 생성 중 이전 백업 내용이 달라졌습니다.")
            if _decode_snapshot(staged_record, database_key,
                                previous_snapshot if parent is not None else None) != snapshot:
                raise BackupIntegrityError("새 백업의 snapshot 검증에 실패했습니다.")
            if final_path.exists():
                raise BackupAlreadyExistsError(f"백업 파일이 이미 있습니다: {final_path}")
            os.replace(temporary_archive, final_path)
        finally:
            temporary_archive.unlink(missing_ok=True)

        record = replace(staged_record, path=final_path)
        self._snapshot_cache = (hashlib.sha256(database_key).digest(),
                                (*verified_chain, record), snapshot)
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
        """Keep newest restore points and every ancestor they depend on."""

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
        # An unreadable archive might be a needed child or parent. Keep files
        # until the ambiguity is repaired instead of making recovery worse.
        for candidate in self._directory.glob(f"*{BACKUP_EXTENSION}"):
            try:
                inspect_backup(candidate)
            except Exception:
                # Clearly unrelated non-ZIP files remain untouched; a damaged
                # archive may contain lineage information we cannot trust.
                try:
                    with candidate.open("rb") as stream:
                        zip_header = stream.read(2) == b"PK"
                except OSError:
                    return ()
                if zip_header or zipfile.is_zipfile(candidate):
                    return ()
        keep: set[Path] = set()
        if protected is not None and any(
            record.path == protected for record in records
        ):
            keep.add(protected)
        for record in records:
            if len(keep) >= self._max_backups:
                break
            keep.add(record.path)

        try:
            for record in records:
                if record.path in keep and record.format_version == 2:
                    keep.update(item.path for item in _snapshot_chain(record, records=records))
        except (BackupFormatError, BackupIntegrityError, OSError):
            return ()

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
    from .database_replica import REPLICA_EXTENSION, inspect_replica
    if Path(path).suffix.casefold() == REPLICA_EXTENSION:
        return inspect_replica(path)
    return _inspect_backup(path, verify_credentials=True)


def _inspect_backup(path: str | Path, *, verify_credentials: bool) -> BackupRecord:
    backup_path = Path(path).resolve()
    if not backup_path.is_file():
        raise BackupDirectoryNotFoundError(f"백업 파일이 없습니다: {backup_path}")
    try:
        with zipfile.ZipFile(backup_path, mode="r") as archive:
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
            if manifest["version"] == 2 and any(info.compress_type != zipfile.ZIP_STORED for info in archive.infolist()):
                raise BackupFormatError("백업 ZIP 압축 형식이 올바르지 않습니다.")
            if manifest["version"] == 2 and archive.getinfo(_DATABASE_MEMBER).file_size > backup_snapshot.MAX_SNAPSHOT_BYTES + 1024 * 1024:
                raise BackupFormatError("백업 payload가 비정상적으로 큽니다.")
            credentials = (DatabaseCredentials.from_bytes(credential_payload)
                           if verify_credentials else None)
            if credentials is not None and str(credentials.database_id) != manifest["database_id"]:
                raise BackupIntegrityError(
                    "백업 DB와 비밀번호 정보의 식별자가 다릅니다."
                )

            database_info = archive.getinfo(_DATABASE_MEMBER)
            credential_info = archive.getinfo(_CREDENTIAL_MEMBER)
            if database_info.file_size != manifest["database_size"]:
                raise BackupIntegrityError("백업 DB 크기가 기록과 다릅니다.")
            if verify_credentials and credential_info.file_size != manifest["credentials_size"]:
                raise BackupIntegrityError(
                    "백업 비밀번호 정보 크기가 기록과 다릅니다."
                )
            database_sha256 = _sha256_member(archive, _DATABASE_MEMBER)
            credentials_sha256 = _sha256_member(archive, _CREDENTIAL_MEMBER)
            if database_sha256 != manifest["database_sha256"]:
                raise BackupIntegrityError("백업 DB의 SHA-256이 기록과 다릅니다.")
            if verify_credentials and credentials_sha256 != manifest["credentials_sha256"]:
                raise BackupIntegrityError(
                    "백업 비밀번호 정보의 SHA-256이 기록과 다릅니다."
                )
    except (zipfile.BadZipFile, CredentialFormatError) as error:
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
        format_version=manifest["version"],
        backup_kind=manifest.get("backup_kind", "full"),
        backup_id=manifest.get("backup_id"),
        parent_backup_id=manifest.get("parent_backup_id"),
        parent_database_sha256=manifest.get("parent_database_sha256"),
        full_backup_id=manifest.get("full_backup_id"),
        full_created_at=manifest.get("full_created_at"),
        snapshot_size=manifest.get("snapshot_size"),
        snapshot_sha256=manifest.get("snapshot_sha256"),
        authenticated_manifest_sha256=(
            hashlib.sha256(backup_snapshot._aad(json.loads(manifest_payload))).hexdigest()
            if manifest["version"] == 2 else None
        ),
    )


def backup_has_recovery_password(path: str | Path) -> bool:
    """Check whether a validated backup contains a legacy recovery key slot."""
    record = inspect_backup(path)
    if record.format_version == 3:
        return DatabaseCredentials.load(record.path).recovery_slot is not None
    with zipfile.ZipFile(record.path, mode="r") as archive:
        payload = _read_small_member(archive, _CREDENTIAL_MEMBER, _MAX_CREDENTIAL_BYTES)
    return DatabaseCredentials.from_bytes(payload).recovery_slot is not None


def _snapshot_chain(record: BackupRecord, *, records: tuple[BackupRecord, ...] | None = None) -> tuple[BackupRecord, ...]:
    """Resolve immutable IDs in this directory, rejecting ambiguous dependencies."""
    if not 1 <= record.schema_version <= CURRENT_SCHEMA_VERSION:
        raise BackupFormatError("지원하지 않는 백업 schema version입니다.")
    if record.backup_kind == "full":
        return (record,)
    directory = record.path.parent.resolve()
    by_id = {}
    ambiguous = set()
    if records is None:
        discovered = []
        for candidate in directory.glob(f"*{BACKUP_EXTENSION}"):
            path = candidate.resolve()
            if path.parent != directory or path == record.path:
                continue
            try:
                discovered.append(_inspect_backup(path, verify_credentials=False))
            except (BackupFormatError, BackupIntegrityError, OSError):
                continue
        records = tuple(discovered)
    for item in records:
        if item.path == record.path:
            continue
        if item.format_version != 2:
            continue
        previous = by_id.get(item.backup_id)
        if previous is None:
            by_id[item.backup_id] = item
        elif replace(item, path=previous.path,
                     credentials_sha256=previous.credentials_sha256) != previous:
            # Identical copies (including independently rewrapped credentials)
            # are interchangeable. Only conflicting required ancestors block us.
            ambiguous.add(item.backup_id)
    chain = []
    seen = set()
    current = record
    while True:
        if current.backup_id in seen:
            raise BackupIntegrityError("백업 의존 관계가 순환합니다.")
        seen.add(current.backup_id)
        if not 1 <= current.schema_version <= CURRENT_SCHEMA_VERSION:
            raise BackupFormatError("지원하지 않는 백업 schema version입니다.")
        chain.append(current)
        if current.backup_kind == "full":
            break
        if current.parent_backup_id in ambiguous:
            raise BackupIntegrityError("필요한 이전 백업 식별자에 서로 다른 내용이 있습니다.")
        parent = by_id.get(current.parent_backup_id)
        if parent is None:
            raise BackupIntegrityError("증분 백업에 필요한 이전 백업 파일이 없습니다.")
        if (parent.database_id != current.database_id
                or parent.schema_version != current.schema_version
                or parent.snapshot_sha256 != current.parent_database_sha256
                or parent.created_at > current.created_at
                or parent.full_backup_id != current.full_backup_id
                or parent.full_created_at != current.full_created_at):
            raise BackupIntegrityError("증분 백업의 이전 이력 또는 식별자가 다릅니다.")
        current = parent
    return tuple(reversed(chain))


def _decode_snapshot(
    item: BackupRecord, database_key: bytes, parent: bytes | None,
) -> bytes:
    """Authenticate one on-disk archive against its inspected record."""
    try:
        with zipfile.ZipFile(item.path, "r") as archive:
            manifest = json.loads(_read_small_member(archive, _MANIFEST_MEMBER, _MAX_MANIFEST_BYTES))
            payload = archive.read(_DATABASE_MEMBER)
        if hashlib.sha256(payload).hexdigest() != item.database_sha256:
            raise BackupIntegrityError("백업 payload가 변경되었습니다.")
        snapshot = backup_snapshot.decrypt(payload, database_key, manifest, parent)
        if (len(snapshot) != item.snapshot_size
                or hashlib.sha256(snapshot).hexdigest() != item.snapshot_sha256
                or snapshot[:16] != b"SQLite format 3\x00"
                or int.from_bytes(snapshot[60:64], "big") != item.schema_version):
            raise BackupIntegrityError("복원 snapshot 크기, SHA-256 또는 schema가 다릅니다.")
        return snapshot
    except (BackupFormatError, BackupIntegrityError, OSError):
        raise
    except Exception as error:
        raise BackupIntegrityError("백업 인증 또는 증분 데이터 검증에 실패했습니다.") from error


def _reconstruct_snapshot(
    record: BackupRecord, database_key: bytes, *,
    records: tuple[BackupRecord, ...] | None = None,
) -> bytes:
    snapshot = None
    for item in _snapshot_chain(record, records=records):
        snapshot = _decode_snapshot(item, database_key, snapshot)
    if snapshot is None:
        raise BackupIntegrityError("복원할 snapshot이 없습니다.")
    return snapshot


def _rewrap_snapshot_backup(path: Path, record: BackupRecord, database_key: bytes,
                            new_password: str) -> None:
    snapshot = _reconstruct_snapshot(record, database_key)
    with tempfile.TemporaryDirectory(prefix=".mealrewrap-", dir=path.parent) as staging_value:
        staging = Path(staging_value)
        validation_file = staging / "validation.sqlite3"
        EncryptedDatabase.restore_snapshot(snapshot, validation_file, database_key)
        with EncryptedDatabase.open(validation_file, database_key) as database:
            latest_id = database.latest_revision_id()
            if (latest_id != record.latest_revision_id
                    or database.schema_version < record.schema_version):
                raise BackupIntegrityError("백업 DB 이력이 기록과 다릅니다.")
        with zipfile.ZipFile(path, "r") as archive:
            credentials = DatabaseCredentials.from_bytes(archive.read(_CREDENTIAL_MEMBER))
            manifest = json.loads(archive.read(_MANIFEST_MEMBER))
            payload = archive.read(_DATABASE_MEMBER)
        credential_payload = credentials.with_user_password(database_key, new_password).to_bytes()
        manifest["credentials"]["size"] = len(credential_payload)
        manifest["credentials"]["sha256"] = hashlib.sha256(credential_payload).hexdigest()
        staged = staging / path.name
        with zipfile.ZipFile(staged, "x", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
            archive.writestr(_DATABASE_MEMBER, payload)
            archive.writestr(_CREDENTIAL_MEMBER, credential_payload)
            archive.writestr(_MANIFEST_MEMBER, _json_bytes(manifest))
        _flush_file(staged)
        staged_record = inspect_backup(staged)
        if (staged_record.database_sha256 != record.database_sha256
                or staged_record.snapshot_sha256 != record.snapshot_sha256
                or staged_record.backup_id != record.backup_id):
            raise BackupIntegrityError("비밀번호 변경 중 백업 내용이 달라졌습니다.")
        if DatabaseCredentials.from_bytes(credential_payload).unlock_user(new_password) != database_key:
            raise BackupIntegrityError("새 비밀번호로 백업을 열 수 없습니다.")
        os.replace(staged, path)


def _rewrap_backup(
    path: Path, record: BackupRecord, database_key: bytes, new_password: str,
) -> None:
    """Validate the historical database with the active key before replacement."""
    if record.format_version == 2:
        _rewrap_snapshot_backup(path, record, database_key, new_password)
        return
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
            latest_id = database.latest_revision_id()
            if (database.schema_version < record.schema_version or
                latest_id != record.latest_revision_id):
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
    from .database_replica import REPLICA_EXTENSION, restore_replica
    if Path(backup_path).suffix.casefold() == REPLICA_EXTENSION:
        return restore_replica(backup_path, destination_database_path, password,
            use_recovery=use_recovery, new_user_password=new_user_password, _lock=_lock)
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
                    if record.format_version == 1:
                        _extract_member(archive, _DATABASE_MEMBER, staged_database)
                    _write_bytes(staged_credentials, credentials.to_bytes())

                if record.format_version == 2:
                    snapshot = _reconstruct_snapshot(record, key)
                    EncryptedDatabase.restore_snapshot(snapshot, staged_database, key)
                elif _sha256_file(staged_database) != record.database_sha256:
                    raise BackupIntegrityError("복원 중 백업 DB 내용이 달라졌습니다.")
                with EncryptedDatabase.open(staged_database, key) as restored:
                    restored.check_integrity()
                    latest_id = restored.latest_revision_id()
                    if (latest_id != record.latest_revision_id
                            or restored.schema_version < record.schema_version):
                        raise BackupIntegrityError("복원 DB 이력이 기록과 다릅니다.")
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
        version = _required_int(document, "version")
        if document.get("format") != _FORMAT or version not in (1, 2):
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
        extra = {}
        if version == 2:
            backup_id = UUID(_required_text(document, "backup_id"))
            kind = _required_text(document, "backup_kind")
            parent_id = document.get("parent_backup_id")
            parent_hash = document.get("parent_database_sha256")
            full_id = UUID(_required_text(document, "full_backup_id"))
            full_created_at = datetime.fromisoformat(_required_text(document, "full_created_at"))
            if full_created_at.tzinfo is None or full_created_at > created_at:
                raise ValueError
            if kind == "full":
                if parent_id is not None or parent_hash is not None or full_id != backup_id or full_created_at != created_at:
                    raise ValueError
            elif kind == "incremental":
                parent_id = UUID(_required_text(document, "parent_backup_id"))
                parent_hash = _required_sha256(document, "parent_database_sha256")
                if parent_id == backup_id or full_id == backup_id:
                    raise ValueError
            else:
                raise ValueError
            snapshot = document["snapshot"]
            if not isinstance(snapshot, dict):
                raise TypeError
            snapshot_size = _required_nonnegative_int(snapshot, "size")
            if not 100 <= snapshot_size <= backup_snapshot.MAX_SNAPSHOT_BYTES or database_size < 28:
                raise ValueError
            extra = {"backup_kind": kind, "backup_id": backup_id,
                     "parent_backup_id": parent_id, "parent_database_sha256": parent_hash,
                     "full_backup_id": full_id, "full_created_at": full_created_at.astimezone(UTC),
                     "snapshot_size": snapshot_size,
                     "snapshot_sha256": _required_sha256(snapshot, "sha256")}
    except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError) as error:
        raise BackupFormatError("백업 manifest 형식이 올바르지 않습니다.") from error
    return {
        "version": version,
        **extra,
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
