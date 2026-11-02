"""Versioned import-profile states stored in the immutable revision ledger."""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast
from uuid import UUID

from src.domain.revisions import (
    AuditChange,
    ChangeOperation,
    HistoricalSnapshot,
    JsonValue,
)

from .contracts import ImportMappingProfile


IMPORT_PROFILE_KIND = "import_profile"


@dataclass(frozen=True, slots=True)
class StoredImportProfile:
    profile: ImportMappingProfile
    header_fingerprint: str

    def __post_init__(self) -> None:
        if not isinstance(self.profile, ImportMappingProfile):
            raise TypeError("profile은 ImportMappingProfile이어야 합니다.")
        if not isinstance(self.header_fingerprint, str):
            raise TypeError("header fingerprint는 문자열이어야 합니다.")
        fingerprint = self.header_fingerprint.strip().lower()
        if len(fingerprint) != 64 or any(
            character not in "0123456789abcdef" for character in fingerprint
        ):
            raise ValueError("header fingerprint는 64자리 SHA-256이어야 합니다.")
        object.__setattr__(self, "header_fingerprint", fingerprint)

    @property
    def profile_id(self) -> UUID:
        return self.profile.profile_id

    @property
    def version(self) -> int:
        return self.profile.version

    def state(self) -> dict[str, JsonValue]:
        return cast(
            dict[str, JsonValue],
            {
                "header_fingerprint": self.header_fingerprint,
                "profile": self.profile.manifest(),
            },
        )


def import_profile_audit_change(
    before: StoredImportProfile | None,
    after: StoredImportProfile,
) -> AuditChange:
    """Create one immutable insert/update while enforcing linear profile versions."""

    if before is not None and not isinstance(before, StoredImportProfile):
        raise TypeError("before는 StoredImportProfile 또는 None이어야 합니다.")
    if not isinstance(after, StoredImportProfile):
        raise TypeError("after는 StoredImportProfile이어야 합니다.")
    if before is None:
        if after.version != 1:
            raise ValueError("새 import profile의 첫 version은 1이어야 합니다.")
        return AuditChange(
            IMPORT_PROFILE_KIND,
            after.profile_id,
            ChangeOperation.INSERT,
            None,
            after.state(),
        )
    if before.profile_id != after.profile_id:
        raise ValueError("import profile update의 profile ID가 다릅니다.")
    if after.version != before.version + 1:
        raise ValueError("import profile version은 정확히 1씩 증가해야 합니다.")
    return AuditChange(
        IMPORT_PROFILE_KIND,
        after.profile_id,
        ChangeOperation.UPDATE,
        before.state(),
        after.state(),
    )


def stored_import_profile_from_state(
    profile_id: UUID,
    state: object,
) -> StoredImportProfile:
    if not isinstance(profile_id, UUID):
        raise TypeError("profile ID는 UUID여야 합니다.")
    if not isinstance(state, dict) and not hasattr(state, "items"):
        raise TypeError("import profile state는 mapping이어야 합니다.")
    values = dict(state)
    if set(values) != {"header_fingerprint", "profile"}:
        raise ValueError("import profile state 필드가 올바르지 않습니다.")
    profile_state = values["profile"]
    if not isinstance(profile_state, dict) and not hasattr(profile_state, "items"):
        raise TypeError("import profile manifest는 mapping이어야 합니다.")
    profile = ImportMappingProfile.from_manifest(dict(profile_state))
    if profile.profile_id != profile_id:
        raise ValueError("import profile entity ID와 manifest ID가 다릅니다.")
    return StoredImportProfile(profile, str(values["header_fingerprint"]))


def import_profiles_from_snapshot(
    snapshot: HistoricalSnapshot | None,
) -> tuple[StoredImportProfile, ...]:
    if snapshot is None:
        return ()
    if not isinstance(snapshot, HistoricalSnapshot):
        raise TypeError("snapshot은 HistoricalSnapshot 또는 None이어야 합니다.")
    profiles = (
        stored_import_profile_from_state(reference.entity_id, state)
        for reference, state in snapshot.entities.items()
        if reference.entity_kind == IMPORT_PROFILE_KIND
    )
    return tuple(
        sorted(
            profiles,
            key=lambda item: (item.profile.name.casefold(), str(item.profile_id)),
        )
    )
