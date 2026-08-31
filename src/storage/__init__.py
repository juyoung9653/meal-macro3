"""Storage Adapters for the SQLite desktop application."""

from __future__ import annotations

from .database_credentials import (
    CredentialAlreadyExistsError,
    CredentialFormatError,
    CredentialNotFoundError,
    CredentialUnlockError,
    DatabaseCredentials,
    PasswordPolicyError,
    credential_path_for,
)

_ENCRYPTED_DATABASE_EXPORTS = {
    "AutomaticBackupResult",
    "BACKUP_EXTENSION",
    "DEFAULT_MAX_BACKUPS",
    "BackupAlreadyExistsError",
    "BackupDirectoryNotFoundError",
    "BackupFormatError",
    "BackupIntegrityError",
    "BackupRecord",
    "BackupRestoreConflictError",
    "DatabaseAlreadyExistsError",
    "DatabaseBackupService",
    "DatabaseEncryptionUnavailableError",
    "DatabaseFormatError",
    "DatabaseIntegrityError",
    "DatabaseNotFoundError",
    "DatabaseSession",
    "DatabaseUnlockError",
    "EncryptedDatabase",
    "RevisionConflictError",
    "inspect_backup",
    "restore_backup_with_recovery_password",
    "restore_backup_with_user_password",
}

__all__ = [
    "DatabaseAlreadyExistsError",
    "DatabaseEncryptionUnavailableError",
    "DatabaseFormatError",
    "DatabaseIntegrityError",
    "DatabaseNotFoundError",
    "DatabaseUnlockError",
    "EncryptedDatabase",
    "RevisionConflictError",
    "CredentialAlreadyExistsError",
    "CredentialFormatError",
    "CredentialNotFoundError",
    "CredentialUnlockError",
    "DatabaseCredentials",
    "PasswordPolicyError",
    "credential_path_for",
    "AutomaticBackupResult",
    "BACKUP_EXTENSION",
    "DEFAULT_MAX_BACKUPS",
    "BackupAlreadyExistsError",
    "BackupDirectoryNotFoundError",
    "BackupFormatError",
    "BackupIntegrityError",
    "BackupRecord",
    "BackupRestoreConflictError",
    "DatabaseBackupService",
    "DatabaseSession",
    "inspect_backup",
    "restore_backup_with_recovery_password",
    "restore_backup_with_user_password",
]


def __getattr__(name: str):
    """Load the APSW-backed Adapter only when a caller actually requests it."""

    if name not in _ENCRYPTED_DATABASE_EXPORTS:
        raise AttributeError(name)
    if name in {
        "AutomaticBackupResult",
        "DatabaseSession",
    }:
        from . import database_session as module
    elif name in {
        "BACKUP_EXTENSION",
        "DEFAULT_MAX_BACKUPS",
        "BackupAlreadyExistsError",
        "BackupDirectoryNotFoundError",
        "BackupFormatError",
        "BackupIntegrityError",
        "BackupRecord",
        "BackupRestoreConflictError",
        "DatabaseBackupService",
        "inspect_backup",
        "restore_backup_with_recovery_password",
        "restore_backup_with_user_password",
    }:
        from . import database_backup as module
    else:
        from . import encrypted_database as module

    value = getattr(module, name)
    globals()[name] = value
    return value
