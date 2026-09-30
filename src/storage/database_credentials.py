"""Password envelopes for the random encrypted-database key.

The credential sidecar contains no plaintext password and no plaintext database
key. New files have one password slot; older files may also have a recovery slot.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt


_FORMAT = "meal-delivery-v2-credentials"
_VERSION = 1
_SINGLE_PASSWORD_VERSION = 2
_DATABASE_KEY_BYTES = 32
_DERIVED_KEY_BYTES = 32
_SALT_BYTES = 16
_NONCE_BYTES = 12
_CIPHERTEXT_BYTES = _DATABASE_KEY_BYTES + 16
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_MAX_PASSWORD_BYTES = 1_024
_MAX_CREDENTIAL_BYTES = 64 * 1024

_USER_SLOT = "user"
_RECOVERY_SLOT = "recovery"


class CredentialNotFoundError(FileNotFoundError):
    pass


class CredentialAlreadyExistsError(FileExistsError):
    pass


class CredentialFormatError(RuntimeError):
    pass


class CredentialUnlockError(RuntimeError):
    pass


class PasswordPolicyError(ValueError):
    pass


@dataclass(frozen=True)
class _WrappedKeySlot:
    salt: bytes
    nonce: bytes
    ciphertext: bytes
    n: int = _SCRYPT_N
    r: int = _SCRYPT_R
    p: int = _SCRYPT_P


@dataclass(frozen=True)
class DatabaseCredentials:
    """Parsed credential document bound to a DB sidecar path when available."""

    database_id: UUID
    created_at: datetime
    user_slot: _WrappedKeySlot
    recovery_slot: _WrappedKeySlot | None
    path: Path | None = None

    @classmethod
    def create(
        cls,
        database_path: str | Path,
        user_password: str,
        recovery_password: str | None = None,
        *,
        database_key: bytes | None = None,
    ) -> tuple[DatabaseCredentials, bytes]:
        """Create a new sidecar and return it with its random DB key."""

        if recovery_password is not None and user_password == recovery_password:
            raise PasswordPolicyError(
                "기관 복구 비밀번호는 평소 사용하는 비밀번호와 다르게 정해주세요."
            )
        _password_bytes(user_password)
        if recovery_password is not None:
            _password_bytes(recovery_password)
        key = secrets.token_bytes(_DATABASE_KEY_BYTES) if database_key is None else database_key
        _validate_database_key(key)
        database_id = uuid4()
        credentials = cls(
            database_id=database_id,
            created_at=datetime.now(UTC),
            user_slot=_wrap_key(key, user_password, database_id, _USER_SLOT),
            recovery_slot=(
                None if recovery_password is None else
                _wrap_key(key, recovery_password, database_id, _RECOVERY_SLOT)
            ),
            path=credential_path_for(database_path),
        )
        credentials.write_new()
        return credentials, key

    @classmethod
    def load(cls, database_path: str | Path) -> DatabaseCredentials:
        path = credential_path_for(database_path)
        if not path.is_file():
            raise CredentialNotFoundError(f"DB 비밀번호 정보가 없습니다: {path}")
        try:
            size = path.stat().st_size
        except OSError as error:
            raise CredentialNotFoundError(f"DB 비밀번호 정보를 읽을 수 없습니다: {path}") from error
        if size > _MAX_CREDENTIAL_BYTES:
            raise CredentialFormatError("DB 비밀번호 정보 파일이 비정상적으로 큽니다.")
        return cls.from_bytes(path.read_bytes(), path=path)

    @classmethod
    def from_bytes(
        cls,
        payload: bytes,
        *,
        path: str | Path | None = None,
    ) -> DatabaseCredentials:
        if not isinstance(payload, bytes):
            raise TypeError("credential payload는 bytes여야 합니다.")
        if len(payload) > _MAX_CREDENTIAL_BYTES:
            raise CredentialFormatError("DB 비밀번호 정보가 비정상적으로 큽니다.")
        try:
            document = json.loads(payload.decode("utf-8"))
            if not isinstance(document, dict):
                raise TypeError
            version = document.get("version")
            if document.get("format") != _FORMAT or type(version) is not int or version not in (
                _VERSION, _SINGLE_PASSWORD_VERSION
            ):
                raise ValueError("format")
            database_id = UUID(_required_text(document, "database_id"))
            created_at = datetime.fromisoformat(_required_text(document, "created_at"))
            if created_at.tzinfo is None:
                raise ValueError("timezone")
            slots = document.get("slots")
            if not isinstance(slots, dict):
                raise TypeError
            user_slot = _parse_slot(slots.get(_USER_SLOT))
            if set(slots) - {_USER_SLOT, _RECOVERY_SLOT}:
                raise ValueError("slots")
            if version == _VERSION:
                recovery_slot = _parse_slot(slots[_RECOVERY_SLOT])
            else:
                if _RECOVERY_SLOT in slots:
                    raise ValueError("slots")
                recovery_slot = None
        except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError) as error:
            raise CredentialFormatError("DB 비밀번호 정보 형식이 올바르지 않습니다.") from error
        return cls(
            database_id=database_id,
            created_at=created_at.astimezone(UTC),
            user_slot=user_slot,
            recovery_slot=recovery_slot,
            path=None if path is None else Path(path).resolve(),
        )

    def unlock_user(self, password: str) -> bytes:
        return _unwrap_key(self.user_slot, password, self.database_id, _USER_SLOT)

    def unlock_recovery(self, recovery_password: str) -> bytes:
        if self.recovery_slot is None:
            raise CredentialUnlockError("이 데이터 파일에는 기관 복구 비밀번호가 없습니다.")
        return _unwrap_key(
            self.recovery_slot,
            recovery_password,
            self.database_id,
            _RECOVERY_SLOT,
        )

    def change_user_password(
        self,
        current_password: str,
        new_password: str,
    ) -> DatabaseCredentials:
        key = self.unlock_user(current_password)
        updated = self.with_user_password(key, new_password)
        updated.replace_file()
        return updated

    def reset_user_password(
        self,
        recovery_password: str,
        new_password: str,
    ) -> DatabaseCredentials:
        if recovery_password == new_password:
            raise PasswordPolicyError(
                "새 비밀번호는 기관 복구 비밀번호와 다르게 정해주세요."
            )
        key = self.unlock_recovery(recovery_password)
        updated = self.with_user_password(key, new_password)
        updated.replace_file()
        return updated

    def change_recovery_password(
        self,
        current_user_password: str,
        new_recovery_password: str,
    ) -> DatabaseCredentials:
        if self.recovery_slot is None:
            raise CredentialUnlockError("이 데이터 파일에는 기관 복구 비밀번호가 없습니다.")
        if current_user_password == new_recovery_password:
            raise PasswordPolicyError(
                "기관 복구 비밀번호는 평소 사용하는 비밀번호와 다르게 정해주세요."
            )
        key = self.unlock_user(current_user_password)
        updated = replace(
            self,
            recovery_slot=_wrap_key(
                key,
                new_recovery_password,
                self.database_id,
                _RECOVERY_SLOT,
            ),
        )
        updated.replace_file()
        return updated

    def with_user_password(
        self,
        database_key: bytes,
        new_password: str,
    ) -> DatabaseCredentials:
        _validate_database_key(database_key)
        return replace(
            self,
            user_slot=_wrap_key(
                database_key,
                new_password,
                self.database_id,
                _USER_SLOT,
            ),
        )

    def with_path(self, path: str | Path) -> DatabaseCredentials:
        return replace(self, path=Path(path).resolve())

    def to_bytes(self) -> bytes:
        document = {
            "format": _FORMAT,
            "version": _VERSION if self.recovery_slot is not None else _SINGLE_PASSWORD_VERSION,
            "database_id": str(self.database_id),
            "created_at": self.created_at.astimezone(UTC).isoformat(),
            "slots": {
                _USER_SLOT: _slot_document(self.user_slot),
            },
        }
        if self.recovery_slot is not None:
            document["slots"][_RECOVERY_SLOT] = _slot_document(self.recovery_slot)
        return (
            json.dumps(
                document,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )

    def write_new(self) -> None:
        path = self._require_path()
        if path.exists():
            raise CredentialAlreadyExistsError(f"DB 비밀번호 정보가 이미 있습니다: {path}")
        if not path.parent.is_dir():
            raise CredentialNotFoundError(f"DB 상위 폴더가 없습니다: {path.parent}")
        _write_file(path, self.to_bytes(), replace_existing=False)

    def replace_file(self) -> None:
        path = self._require_path()
        if not path.is_file():
            raise CredentialNotFoundError(f"DB 비밀번호 정보가 없습니다: {path}")
        _write_file(path, self.to_bytes(), replace_existing=True)

    def _require_path(self) -> Path:
        if self.path is None:
            raise RuntimeError("credential 파일 경로가 지정되지 않았습니다.")
        return self.path


def credential_path_for(database_path: str | Path) -> Path:
    path = Path(database_path)
    if not path.name:
        raise ValueError("DB 파일 경로가 필요합니다.")
    return path.resolve().with_name(f"{path.name}.credentials.json")


def _wrap_key(
    database_key: bytes,
    password: str,
    database_id: UUID,
    purpose: str,
) -> _WrappedKeySlot:
    _validate_database_key(database_key)
    salt = secrets.token_bytes(_SALT_BYTES)
    nonce = secrets.token_bytes(_NONCE_BYTES)
    wrapping_key = _derive_key(password, salt, _SCRYPT_N, _SCRYPT_R, _SCRYPT_P)
    ciphertext = AESGCM(wrapping_key).encrypt(
        nonce,
        database_key,
        _associated_data(database_id, purpose),
    )
    return _WrappedKeySlot(salt, nonce, ciphertext)


def _unwrap_key(
    slot: _WrappedKeySlot,
    password: str,
    database_id: UUID,
    purpose: str,
) -> bytes:
    wrapping_key = _derive_key(password, slot.salt, slot.n, slot.r, slot.p)
    try:
        database_key = AESGCM(wrapping_key).decrypt(
            slot.nonce,
            slot.ciphertext,
            _associated_data(database_id, purpose),
        )
    except InvalidTag as error:
        raise CredentialUnlockError(
            "비밀번호가 올바르지 않거나 DB 비밀번호 정보가 손상되었습니다."
        ) from error
    _validate_database_key(database_key)
    return database_key


def _derive_key(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return Scrypt(
        salt=salt,
        length=_DERIVED_KEY_BYTES,
        n=n,
        r=r,
        p=p,
    ).derive(_password_bytes(password))


def _password_bytes(password: object) -> bytes:
    if not isinstance(password, str):
        raise TypeError("비밀번호는 문자열이어야 합니다.")
    encoded = password.encode("utf-8")
    if not encoded:
        raise PasswordPolicyError("비밀번호를 입력해주세요.")
    if len(encoded) > _MAX_PASSWORD_BYTES:
        raise PasswordPolicyError("비밀번호가 너무 깁니다.")
    return encoded


def _validate_database_key(database_key: object) -> None:
    if not isinstance(database_key, bytes):
        raise TypeError("DB key는 bytes여야 합니다.")
    if len(database_key) != _DATABASE_KEY_BYTES:
        raise ValueError(f"DB key는 {_DATABASE_KEY_BYTES} bytes여야 합니다.")


def _associated_data(database_id: UUID, purpose: str) -> bytes:
    return f"{_FORMAT}|{_VERSION}|{database_id}|{purpose}".encode("ascii")


def _slot_document(slot: _WrappedKeySlot) -> dict[str, Any]:
    return {
        "kdf": {
            "name": "scrypt",
            "salt": _encode(slot.salt),
            "n": slot.n,
            "r": slot.r,
            "p": slot.p,
            "length": _DERIVED_KEY_BYTES,
        },
        "cipher": {
            "name": "aes-256-gcm",
            "nonce": _encode(slot.nonce),
            "ciphertext": _encode(slot.ciphertext),
        },
    }


def _parse_slot(value: object) -> _WrappedKeySlot:
    if not isinstance(value, dict):
        raise TypeError
    kdf = value.get("kdf")
    cipher = value.get("cipher")
    if not isinstance(kdf, dict) or not isinstance(cipher, dict):
        raise TypeError
    if kdf.get("name") != "scrypt" or cipher.get("name") != "aes-256-gcm":
        raise ValueError("algorithm")
    n = _required_int(kdf, "n")
    r = _required_int(kdf, "r")
    p = _required_int(kdf, "p")
    length = _required_int(kdf, "length")
    if (n, r, p, length) != (_SCRYPT_N, _SCRYPT_R, _SCRYPT_P, _DERIVED_KEY_BYTES):
        raise ValueError("kdf parameters")
    return _WrappedKeySlot(
        salt=_decode(_required_text(kdf, "salt"), _SALT_BYTES),
        nonce=_decode(_required_text(cipher, "nonce"), _NONCE_BYTES),
        ciphertext=_decode(
            _required_text(cipher, "ciphertext"),
            _CIPHERTEXT_BYTES,
        ),
        n=n,
        r=r,
        p=p,
    )


def _required_text(value: dict[str, Any], key: str) -> str:
    found = value[key]
    if not isinstance(found, str) or not found:
        raise TypeError
    return found


def _required_int(value: dict[str, Any], key: str) -> int:
    found = value[key]
    if isinstance(found, bool) or not isinstance(found, int):
        raise TypeError
    return found


def _encode(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _decode(value: str, expected_size: int) -> bytes:
    try:
        decoded = base64.b64decode(value.encode("ascii"), validate=True)
    except (UnicodeError, ValueError) as error:
        raise ValueError("base64") from error
    if len(decoded) != expected_size:
        raise ValueError("binary size")
    return decoded


def _write_file(path: Path, payload: bytes, *, replace_existing: bool) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        descriptor = os.open(temporary, flags, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        if not replace_existing and path.exists():
            raise CredentialAlreadyExistsError(
                f"DB 비밀번호 정보가 이미 있습니다: {path}"
            )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
