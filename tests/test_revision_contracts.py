"""Revision contract validation tests."""

from dataclasses import replace
from datetime import UTC, date, datetime
from uuid import UUID

import pytest

from src.domain.revisions import (
    AuditChange,
    ChangeOperation,
    ImportBatch,
    RevisionActor,
    RevisionMetadata,
    RevisionRecord,
    RevisionSource,
    format_revision_timestamp,
)


REVISION_ID = UUID("20000000-0000-0000-0000-000000000001")
ENTITY_ID = UUID("20000000-0000-0000-0000-000000000002")
BATCH_ID = UUID("20000000-0000-0000-0000-000000000003")


def _metadata(source: RevisionSource = RevisionSource.UI) -> RevisionMetadata:
    return RevisionMetadata(
        revision_id=REVISION_ID,
        parent_revision_id=None,
        recorded_at=datetime(2026, 8, 24, 15, 0, tzinfo=UTC),
        effective_on=date(2026, 8, 17),
        actor=RevisionActor("tester", "S-1-5-21-test"),
        reason="명단 반영",
        source=source,
        decisions={"identity": ["confirmed"]},
        rule_versions={"delivery": 1},
    )


def _change() -> AuditChange:
    return AuditChange(
        "person",
        ENTITY_ID,
        ChangeOperation.INSERT,
        before=None,
        after={"name": "홍길동"},
    )


def test_revision_json_is_deeply_immutable():
    decisions = {"nested": {"values": [1, 2]}}
    metadata = replace(_metadata(), decisions=decisions)
    decisions["nested"]["values"].append(3)

    assert metadata.decisions["nested"]["values"] == (1, 2)


def test_revision_display_code_is_optional_until_database_assignment():
    metadata = _metadata()
    stored = replace(metadata, display_code="20260825-001")

    assert metadata.display_code is None
    assert stored.display_code == "20260825-001"


def test_format_revision_timestamp_uses_korean_save_time_to_the_second():
    assert format_revision_timestamp(datetime(2026, 8, 24, 15, 0, 7, tzinfo=UTC)) == "20260825-000007"


def test_format_revision_timestamp_rejects_naive_datetime():
    with pytest.raises(ValueError, match="time zone"):
        format_revision_timestamp(datetime(2026, 8, 25, 0, 0, 7))


@pytest.mark.parametrize(
    "display_code",
    (
        "2026-08-25-001",
        "20260230-001",
        "20260825-000",
        "20260825-01",
        "20260825-0001",
    ),
)
def test_revision_display_code_rejects_invalid_values(display_code: str):
    with pytest.raises(ValueError, match="표시용 변경번호"):
        replace(_metadata(), display_code=display_code)


def test_audit_operation_requires_matching_before_and_after():
    with pytest.raises(ValueError, match="insert.*after"):
        AuditChange(
            "person",
            ENTITY_ID,
            ChangeOperation.INSERT,
            before={"name": "old"},
            after={"name": "new"},
        )


def test_import_batch_rejects_personal_full_path():
    with pytest.raises(ValueError, match="전체 경로"):
        ImportBatch(
            batch_id=BATCH_ID,
            revision_id=REVISION_ID,
            source_filename=r"C:\Users\person\roster.xlsx",
            sha256="a" * 64,
            byte_size=10,
            sheet_name="명단",
            header_fingerprint="fingerprint",
            mapping_manifest={},
            effective_on=date(2026, 8, 17),
        )


def test_import_batch_preserves_exact_internal_filename_spaces():
    batch = ImportBatch(
        batch_id=BATCH_ID,
        revision_id=REVISION_ID,
        source_filename="survey  final.xlsx",
        sha256="a" * 64,
        byte_size=10,
        sheet_name="조사표  원본",
        header_fingerprint="fingerprint",
        mapping_manifest={},
        effective_on=date(2026, 8, 17),
    )

    assert batch.source_filename == "survey  final.xlsx"
    assert batch.sheet_name == "조사표  원본"


def test_import_batch_requires_import_revision_source():
    batch = ImportBatch(
        batch_id=BATCH_ID,
        revision_id=REVISION_ID,
        source_filename="roster.xlsx",
        sha256="a" * 64,
        byte_size=10,
        sheet_name="명단",
        header_fingerprint="fingerprint",
        mapping_manifest={},
        effective_on=date(2026, 8, 17),
    )

    with pytest.raises(ValueError, match="source는 import"):
        RevisionRecord(_metadata(), (_change(),), batch)


def test_revision_rejects_two_changes_for_the_same_entity():
    with pytest.raises(ValueError, match="같은 entity"):
        RevisionRecord(_metadata(), (_change(), _change()))
