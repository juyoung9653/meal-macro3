"""Generated document contracts and encrypted history storage tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import UUID

import pytest

apsw = pytest.importorskip(
    "apsw",
    reason="install requirements-sqlite-v2.txt for encrypted DB integration tests",
)

from src.domain.revisions import (
    AuditChange,
    ChangeOperation,
    RevisionActor,
    RevisionMetadata,
    RevisionRecord,
    RevisionSource,
)
from src.domain.documents import (
    DocumentKind,
    DocumentRun,
)
from src.storage.encrypted_database import EncryptedDatabase
from src.storage.migrations import CURRENT_SCHEMA_VERSION


KEY = bytes.fromhex("55" * 32)
REVISION_ID = UUID("55000000-0000-0000-0000-000000000001")
ENTITY_ID = UUID("55000000-0000-0000-0000-000000000002")
RUN_ID = UUID("55000000-0000-0000-0000-000000000003")
LATER_REVISION_ID = UUID("55000000-0000-0000-0000-000000000004")


def _revision() -> RevisionRecord:
    return RevisionRecord(
        RevisionMetadata(
            revision_id=REVISION_ID,
            parent_revision_id=None,
            recorded_at=datetime(2026, 8, 26, 1, 0, tzinfo=UTC),
            effective_on=date(2026, 8, 17),
            actor=RevisionActor("tester", "S-1-5-21-test"),
            reason="문서 원본 명단",
            source=RevisionSource.UI,
        ),
        (
            AuditChange(
                "document-probe",
                ENTITY_ID,
                ChangeOperation.INSERT,
                before=None,
                after={"value": "saved"},
            ),
        ),
    )


def _run(**changes) -> DocumentRun:
    values = {
        "run_id": RUN_ID,
        "kind": DocumentKind.INVOICE,
        "source_revision_id": REVISION_ID,
        "source_revision_display_code": "20260826-001",
        "period_start": date(2026, 8, 1),
        "period_end": date(2026, 8, 31),
        "generated_at": datetime(2026, 8, 26, 2, 0, tzinfo=UTC),
        "actor": RevisionActor("tester", "S-1-5-21-test"),
        "calculation_manifest": {"entitlement_policy_version": 1},
        "template_manifest": {
            "filename": "급식_청구서템플릿.xlsx",
            "sha256": "b" * 64,
        },
        "output_filename": "청구서_202608.xlsx",
        "output_sha256": "a" * 64,
        "output_byte_size": 1234,
    }
    values.update(changes)
    return DocumentRun(**values)


def _later_revision() -> RevisionRecord:
    return RevisionRecord(
        RevisionMetadata(
            revision_id=LATER_REVISION_ID,
            parent_revision_id=REVISION_ID,
            recorded_at=datetime(2026, 8, 26, 3, 0, tzinfo=UTC),
            effective_on=date(2026, 8, 17),
            actor=RevisionActor("tester", "S-1-5-21-test"),
            reason="문서 생성 뒤 명단 수정",
            source=RevisionSource.UI,
        ),
        (
            AuditChange(
                "document-probe",
                ENTITY_ID,
                ChangeOperation.UPDATE,
                before={"value": "saved"},
                after={"value": "later"},
            ),
        ),
    )


def _mark_as_schema_four(path: Path) -> None:
    """Make a current fixture exercise the version-4-to-5 rebuild path."""

    connection = apsw.Connection(str(path))
    try:
        connection.pragma("cipher", "chacha20")
        connection.pragma("hexkey", KEY.hex())
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("ALTER TABLE household_projection DROP COLUMN delivery_settings_json")
        connection.execute("ALTER TABLE person_projection DROP COLUMN delivery_settings_json")
        connection.execute("ALTER TABLE person_projection DROP COLUMN lunch_period_json")
        # Version 10's table must not remain in a synthetic version-4 database.
        connection.execute("DROP TABLE user_view_layout")
        connection.execute("DROP TRIGGER schema_migration_no_delete")
        connection.execute("DELETE FROM schema_migration WHERE version >= 5")
        connection.execute(
            """
            CREATE TRIGGER schema_migration_no_delete
            BEFORE DELETE ON schema_migration
            BEGIN
                SELECT RAISE(ABORT, 'schema migration rows are immutable');
            END
            """
        )
        connection.pragma("user_version", 4)
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def test_document_run_contract_freezes_manifests_and_rejects_full_path():
    calculation = {"rules": {"entitlement": [1]}}
    run = _run(calculation_manifest=calculation)
    calculation["rules"]["entitlement"].append(2)

    assert run.kind.label == "청구서"
    assert run.calculation_manifest["rules"]["entitlement"] == (1,)
    with pytest.raises(ValueError, match="전체 경로"):
        replace(run, output_filename=r"C:\Exports\청구서.xlsx")


def test_document_run_contract_requires_closed_period_and_version_manifests():
    with pytest.raises(ValueError, match="시작일"):
        _run(period_start=date(2026, 9, 1), period_end=date(2026, 8, 31))
    with pytest.raises(ValueError, match="계산 version"):
        _run(calculation_manifest={})
    with pytest.raises(ValueError, match="템플릿 version"):
        _run(template_manifest={})


def test_document_run_is_append_only_and_does_not_advance_roster_revision(
    tmp_path: Path,
):
    path = tmp_path / "operations.sqlite3"
    with EncryptedDatabase.create(path, KEY) as database:
        database.append_revision(_revision())
        latest_before = database.latest_revision().metadata.revision_id

        database.append_document_run(_run())

        assert database.latest_revision().metadata.revision_id == latest_before
        assert database.document_run(RUN_ID) == _run()
        assert database.document_runs() == (_run(),)
        assert database.document_runs(kind=DocumentKind.CONFIRMATION) == ()
        database.check_integrity()

    with EncryptedDatabase.open(path, KEY) as reopened:
        assert reopened.document_run(RUN_ID) == _run()


def test_schema_five_migration_preserves_existing_document_runs(tmp_path: Path):
    path = tmp_path / "operations.sqlite3"
    with EncryptedDatabase.create(path, KEY) as database:
        database.append_revision(_revision())
        database.append_document_run(_run())

    _mark_as_schema_four(path)

    with EncryptedDatabase.open(path, KEY) as migrated:
        assert migrated.schema_version == CURRENT_SCHEMA_VERSION
        assert migrated.document_runs() == (_run(),)
        migrated.check_integrity()


def test_document_run_accepts_delivery_report_kind(tmp_path: Path):
    path = tmp_path / "operations.sqlite3"
    report = _run(
        kind=DocumentKind.DELIVERY_REPORT,
        output_filename="2026-08-17_배송리포트.xlsx",
    )
    with EncryptedDatabase.create(path, KEY) as database:
        database.append_revision(_revision())

        database.append_document_run(report)

        assert database.document_runs(kind=DocumentKind.DELIVERY_REPORT) == (
            report,
        )
        database.check_integrity()


def test_document_source_snapshot_replays_original_revision_after_later_changes(
    tmp_path: Path,
):
    path = tmp_path / "operations.sqlite3"
    with EncryptedDatabase.create(path, KEY) as database:
        database.append_revision(_revision())
        database.append_document_run(_run())
        database.append_revision(_later_revision())

        latest = database.snapshot(LATER_REVISION_ID)
        original = database.document_source_snapshot(RUN_ID)

        assert latest.entity("document-probe", ENTITY_ID) == {"value": "later"}
        assert original.revision_id == REVISION_ID
        assert original.entity("document-probe", ENTITY_ID) == {"value": "saved"}


def test_document_run_rejects_display_code_that_does_not_match_source(
    tmp_path: Path,
):
    path = tmp_path / "operations.sqlite3"
    with EncryptedDatabase.create(path, KEY) as database:
        database.append_revision(_revision())

        with pytest.raises(apsw.ConstraintError, match="display code mismatch"):
            database.append_document_run(
                _run(source_revision_display_code="20260826-999")
            )

        assert database.document_runs() == ()


def test_document_run_rows_cannot_be_updated_or_deleted_after_recording(
    tmp_path: Path,
):
    path = tmp_path / "operations.sqlite3"
    with EncryptedDatabase.create(path, KEY) as database:
        database.append_revision(_revision())
        database.append_document_run(_run())

    connection = apsw.Connection(str(path))
    try:
        connection.pragma("cipher", "chacha20")
        connection.pragma("hexkey", KEY.hex())
        with pytest.raises(apsw.ConstraintError, match="immutable"):
            connection.execute(
                "UPDATE document_run SET output_filename = 'changed.xlsx'"
            )
        with pytest.raises(apsw.ConstraintError, match="immutable"):
            connection.execute("DELETE FROM document_run")
    finally:
        connection.close()
