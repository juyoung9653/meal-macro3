"""Password wrapping tests for encrypted SQLite database keys."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.storage.database_credentials import (
    CredentialAlreadyExistsError,
    CredentialFormatError,
    CredentialUnlockError,
    DatabaseCredentials,
    PasswordPolicyError,
    credential_path_for,
)


KEY = bytes.fromhex("5a" * 32)


def test_single_password_credentials_have_no_recovery_slot(tmp_path: Path):
    path = tmp_path / "single.sqlite3"
    credentials, key = DatabaseCredentials.create(path, "only-password", database_key=KEY)
    assert key == KEY
    assert credentials.recovery_slot is None
    assert json.loads(credentials.to_bytes())["version"] == 2
    assert set(json.loads(credentials.to_bytes())["slots"]) == {"user"}
    loaded = DatabaseCredentials.load(path)
    assert loaded.unlock_user("only-password") == KEY
    with pytest.raises(CredentialUnlockError):
        loaded.unlock_user("wrong-password")
    with pytest.raises(CredentialUnlockError):
        loaded.unlock_recovery("only-password")

    changed = loaded.change_user_password("only-password", "new-password")
    assert changed.recovery_slot is None
    with pytest.raises(CredentialUnlockError):
        changed.unlock_user("only-password")
    assert DatabaseCredentials.load(path).unlock_user("new-password") == KEY


def test_legacy_missing_recovery_slot_is_malformed(tmp_path: Path):
    path = tmp_path / "legacy.sqlite3"
    credentials, _ = DatabaseCredentials.create(path, "user", "recovery", database_key=KEY)
    document = json.loads(credentials.to_bytes())
    del document["slots"]["recovery"]
    with pytest.raises(CredentialFormatError):
        DatabaseCredentials.from_bytes(json.dumps(document).encode())


def test_user_and_institution_passwords_unlock_same_random_key(tmp_path: Path):
    database_path = tmp_path / "operations.sqlite3"

    credentials, key = DatabaseCredentials.create(
        database_path,
        "평소 비밀번호",
        "기관 복구 비밀번호",
        database_key=KEY,
    )

    assert key == KEY
    assert credentials.unlock_user("평소 비밀번호") == KEY
    assert credentials.unlock_recovery("기관 복구 비밀번호") == KEY
    assert DatabaseCredentials.load(database_path) == credentials

    payload = credential_path_for(database_path).read_text(encoding="utf-8")
    assert "평소 비밀번호" not in payload
    assert "기관 복구 비밀번호" not in payload
    assert KEY.hex() not in payload


def test_wrong_password_and_tampered_slot_are_rejected(tmp_path: Path):
    database_path = tmp_path / "operations.sqlite3"
    credentials, _key = DatabaseCredentials.create(
        database_path,
        "correct-user",
        "correct-recovery",
        database_key=KEY,
    )

    with pytest.raises(CredentialUnlockError, match="비밀번호"):
        credentials.unlock_user("wrong-user")
    with pytest.raises(CredentialUnlockError, match="비밀번호"):
        credentials.unlock_recovery("wrong-recovery")

    document = json.loads(credentials.to_bytes())
    ciphertext = document["slots"]["user"]["cipher"]["ciphertext"]
    document["slots"]["user"]["cipher"]["ciphertext"] = (
        ("A" if ciphertext[0] != "A" else "B") + ciphertext[1:]
    )
    tampered = DatabaseCredentials.from_bytes(
        json.dumps(document).encode("utf-8")
    )
    with pytest.raises(CredentialUnlockError):
        tampered.unlock_user("correct-user")


def test_recovery_password_resets_only_everyday_password(tmp_path: Path):
    database_path = tmp_path / "operations.sqlite3"
    credentials, _key = DatabaseCredentials.create(
        database_path,
        "old-user",
        "institution-recovery",
        database_key=KEY,
    )

    updated = credentials.reset_user_password(
        "institution-recovery",
        "new-user",
    )

    with pytest.raises(CredentialUnlockError):
        updated.unlock_user("old-user")
    assert updated.unlock_user("new-user") == KEY
    assert updated.unlock_recovery("institution-recovery") == KEY
    assert DatabaseCredentials.load(database_path) == updated


def test_everyday_password_can_rotate_recovery_password(tmp_path: Path):
    database_path = tmp_path / "operations.sqlite3"
    credentials, _key = DatabaseCredentials.create(
        database_path,
        "user-password",
        "old-recovery",
        database_key=KEY,
    )

    updated = credentials.change_recovery_password(
        "user-password",
        "new-recovery",
    )

    with pytest.raises(CredentialUnlockError):
        updated.unlock_recovery("old-recovery")
    assert updated.unlock_recovery("new-recovery") == KEY
    assert updated.unlock_user("user-password") == KEY


def test_passwords_must_be_nonempty_and_initially_different(tmp_path: Path):
    database_path = tmp_path / "operations.sqlite3"

    with pytest.raises(PasswordPolicyError, match="입력"):
        DatabaseCredentials.create(database_path, "", "recovery")
    with pytest.raises(PasswordPolicyError, match="다르게"):
        DatabaseCredentials.create(database_path, "same", "same")


def test_existing_or_malformed_credential_file_is_not_overwritten(tmp_path: Path):
    database_path = tmp_path / "operations.sqlite3"
    sidecar = credential_path_for(database_path)
    sidecar.write_text("keep-me", encoding="utf-8")

    with pytest.raises(CredentialAlreadyExistsError):
        DatabaseCredentials.create(database_path, "user", "recovery")
    assert sidecar.read_text(encoding="utf-8") == "keep-me"

    with pytest.raises(CredentialFormatError):
        DatabaseCredentials.load(database_path)


def test_slot_purpose_is_authenticated(tmp_path: Path):
    database_path = tmp_path / "operations.sqlite3"
    credentials, _key = DatabaseCredentials.create(
        database_path,
        "user-password",
        "recovery-password",
        database_key=KEY,
    )
    document = json.loads(credentials.to_bytes())
    document["slots"]["user"], document["slots"]["recovery"] = (
        document["slots"]["recovery"],
        document["slots"]["user"],
    )
    swapped = DatabaseCredentials.from_bytes(json.dumps(document).encode("utf-8"))

    with pytest.raises(CredentialUnlockError):
        swapped.unlock_user("recovery-password")
    with pytest.raises(CredentialUnlockError):
        swapped.unlock_recovery("user-password")
