"""Revision-backed shared Excel password policy tests."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from uuid import UUID

import pytest

apsw = pytest.importorskip(
    "apsw",
    reason="install requirements-sqlite-v2.txt for encrypted DB integration tests",
)

from src.domain import (
    EXPORT_PASSWORD_POLICY_VERSION,
    EXPORT_PASSWORD_SETTING_ID,
    EXPORT_PASSWORD_SETTING_KIND,
    AuditChange,
    ChangeOperation,
    RevisionActor,
    RevisionMetadata,
    RevisionRecord,
    RevisionSource,
    build_export_password_revision,
    export_password_setting_from_snapshot,
    export_password_setting_from_state,
    export_password_setting_state,
    require_export_password_setting,
)
from src.storage import EncryptedDatabase


KEY = bytes.fromhex("6a" * 32)
BASE_REVISION_ID = UUID("6a000000-0000-0000-0000-000000000001")
BASE_ENTITY_ID = UUID("6a000000-0000-0000-0000-000000000002")
FIRST_REVISION_ID = UUID("6a000000-0000-0000-0000-000000000003")
SECOND_REVISION_ID = UUID("6a000000-0000-0000-0000-000000000004")
ACTOR = RevisionActor("tester", "S-1-5-21-test")


def _base_revision() -> RevisionRecord:
    return RevisionRecord(
        RevisionMetadata(
            revision_id=BASE_REVISION_ID,
            parent_revision_id=None,
            recorded_at=datetime(2026, 8, 26, 1, 0, tzinfo=UTC),
            effective_on=date(2026, 8, 1),
            actor=ACTOR,
            reason="Excel 비밀번호 기준 명단",
            source=RevisionSource.UI,
        ),
        (
            AuditChange(
                "export-security-probe",
                BASE_ENTITY_ID,
                ChangeOperation.INSERT,
                before=None,
                after={"value": "base"},
            ),
        ),
    )


def test_first_password_revision_hides_secret_from_public_metadata_and_repr():
    prepared = build_export_password_revision(
        BASE_REVISION_ID,
        None,
        "shared-1234",
        actor=ACTOR,
        recorded_at=datetime(2026, 8, 26, 2, 0, tzinfo=UTC),
        revision_id=FIRST_REVISION_ID,
    )

    assert prepared.setting.version == 1
    assert prepared.setting.password == "shared-1234"
    assert "shared-1234" not in repr(prepared.setting)
    assert prepared.record.changes[0].entity_kind == EXPORT_PASSWORD_SETTING_KIND
    assert prepared.record.changes[0].entity_id == EXPORT_PASSWORD_SETTING_ID
    assert prepared.record.changes[0].operation is ChangeOperation.INSERT
    assert prepared.record.metadata.decisions == {
        "export_password_change": {"configured": True, "version": 1}
    }
    assert prepared.record.metadata.rule_versions == {
        "export_password_policy_version": EXPORT_PASSWORD_POLICY_VERSION
    }
    assert "shared-1234" not in repr(prepared.record.metadata.decisions)
    assert prepared.setting.public_manifest() == {
        "protected": True,
        "policy_version": 1,
        "version": 1,
        "setting_revision_id": str(FIRST_REVISION_ID),
    }


def test_state_round_trip_preserves_secret_only_in_encrypted_entity_state():
    prepared = build_export_password_revision(
        BASE_REVISION_ID,
        None,
        "공용비번-1234",
        actor=ACTOR,
        recorded_at=datetime(2026, 8, 26, 2, 0, tzinfo=UTC),
        revision_id=FIRST_REVISION_ID,
    )

    state = export_password_setting_state(prepared.setting)
    assert state["password"] == "공용비번-1234"
    assert export_password_setting_from_state(state) == prepared.setting


def test_unconfigured_snapshot_blocks_unprotected_v2_output():
    from src.domain import HistoricalSnapshot

    snapshot = HistoricalSnapshot(BASE_REVISION_ID, {})

    with pytest.raises(ValueError, match="공통 비밀번호"):
        require_export_password_setting(snapshot)


@pytest.mark.parametrize("password", ["", "   ", " leading", "trailing "])
def test_password_rejects_empty_or_accidental_outer_whitespace(password: str):
    with pytest.raises(ValueError, match="비밀번호"):
        build_export_password_revision(
            BASE_REVISION_ID,
            None,
            password,
            actor=ACTOR,
            recorded_at=datetime(2026, 8, 26, 2, 0, tzinfo=UTC),
            revision_id=FIRST_REVISION_ID,
        )


def test_same_password_is_no_op_and_change_increments_version():
    first = build_export_password_revision(
        BASE_REVISION_ID,
        None,
        "first-password",
        actor=ACTOR,
        recorded_at=datetime(2026, 8, 26, 2, 0, tzinfo=UTC),
        revision_id=FIRST_REVISION_ID,
    )
    state = export_password_setting_state(first.setting)

    with pytest.raises(ValueError, match="같은"):
        build_export_password_revision(
            FIRST_REVISION_ID,
            state,
            "first-password",
            actor=ACTOR,
            recorded_at=datetime(2026, 8, 26, 3, 0, tzinfo=UTC),
            revision_id=SECOND_REVISION_ID,
        )

    second = build_export_password_revision(
        FIRST_REVISION_ID,
        state,
        "second-password",
        actor=ACTOR,
        recorded_at=datetime(2026, 8, 26, 3, 0, tzinfo=UTC),
        revision_id=SECOND_REVISION_ID,
    )
    assert second.setting.version == 2
    assert second.setting.changed_in_revision_id == SECOND_REVISION_ID
    assert second.record.changes[0].operation is ChangeOperation.UPDATE


def test_ledger_replays_old_and_current_password_without_exposing_it_publicly(
    tmp_path: Path,
):
    first = build_export_password_revision(
        BASE_REVISION_ID,
        None,
        "first-password",
        actor=ACTOR,
        recorded_at=datetime(2026, 8, 26, 2, 0, tzinfo=UTC),
        revision_id=FIRST_REVISION_ID,
    )
    second = build_export_password_revision(
        FIRST_REVISION_ID,
        export_password_setting_state(first.setting),
        "second-password",
        actor=ACTOR,
        recorded_at=datetime(2026, 8, 26, 3, 0, tzinfo=UTC),
        revision_id=SECOND_REVISION_ID,
    )

    path = tmp_path / "operations.sqlite3"
    with EncryptedDatabase.create(path, KEY) as database:
        database.append_revision(_base_revision())
        database.append_revision(first.record)
        database.append_revision(second.record)

        old = export_password_setting_from_snapshot(
            database.snapshot(FIRST_REVISION_ID)
        )
        current = export_password_setting_from_snapshot(
            database.snapshot(SECOND_REVISION_ID)
        )
        assert old is not None and old.password == "first-password"
        assert current is not None and current.password == "second-password"
        assert current.public_manifest()["version"] == 2
        database.check_integrity()
