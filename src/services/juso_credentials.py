"""PC/user-local Juso key storage; never falls back to plaintext."""
from __future__ import annotations

SERVICE_NAME = "BundangMealSQLiteV2/JusoSearch"


class CredentialStoreError(RuntimeError):
    pass


class CredentialStore:
    def __init__(self, backend=None):
        self._backend = backend

    def _vault(self):
        if self._backend is None:
            try:
                import win32cred
                self._backend = win32cred
            except ImportError:
                raise CredentialStoreError("Windows 자격 증명 관리자를 사용할 수 없습니다.") from None
        return self._backend

    def get_api_key(self):
        vault = self._vault()
        try:
            record = vault.CredRead(SERVICE_NAME, vault.CRED_TYPE_GENERIC)
            value = record["CredentialBlob"]
            return (value.decode("utf-16-le") if isinstance(value, bytes) else value).strip() or None
        except Exception as error:
            if getattr(error, "winerror", None) == 1168:
                return None
            raise CredentialStoreError("저장된 API 키를 읽지 못했습니다.") from None

    def save_api_key(self, value):
        value = value.strip()
        if not value:
            raise CredentialStoreError("저장할 API 키를 입력해주세요.")
        vault = self._vault()
        try:
            vault.CredWrite({"Type": vault.CRED_TYPE_GENERIC, "TargetName": SERVICE_NAME,
                             "UserName": "juso-search-api-key", "CredentialBlob": value.encode("utf-16-le"),
                             "Persist": vault.CRED_PERSIST_LOCAL_MACHINE}, 0)
        except Exception:
            raise CredentialStoreError("API 키를 Windows에 저장하지 못했습니다.") from None

    def delete_api_key(self):
        vault = self._vault()
        try:
            vault.CredDelete(SERVICE_NAME, vault.CRED_TYPE_GENERIC, 0)
        except Exception as error:
            if getattr(error, "winerror", None) != 1168:
                raise CredentialStoreError("API 키를 삭제하지 못했습니다.") from None
