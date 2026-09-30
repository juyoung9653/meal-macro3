from datetime import UTC, date, datetime
from uuid import UUID

import pytest

from src.domain.custom_field_edits import (
    build_custom_field_create_revision,
    build_custom_field_delete_revision,
)
from src.domain.roster import (
    CustomFieldDefinition,
    CustomFieldType,
    Person,
    PersonProfile,
    RosterSnapshot,
)
from src.domain.revisions import RevisionActor
from src.domain.revisions import RevisionMetadata, RevisionRecord, RevisionSource
from src.domain.projections import roster_audit_changes
from src.storage import DatabaseSession


def _roster() -> RosterSnapshot:
    field_id = UUID("85000000-0000-0000-0000-000000000005")
    return RosterSnapshot(
        UUID("85000000-0000-0000-0000-000000000001"),
        persons=(
            Person(
                UUID("85000000-0000-0000-0000-000000000003"),
                PersonProfile("홍길동", custom_values={field_id: "A구역"}),
            ),
        ),
        custom_fields=(
            CustomFieldDefinition(field_id, "배송 구역", CustomFieldType.TEXT, 0),
        ),
    )


def _arguments():
    return {
        "actor": RevisionActor("tester", "S-1-5-21-test"),
        "recorded_at": datetime(2026, 8, 26, 12, 30, tzinfo=UTC),
        "effective_on": date(2026, 8, 26),
        "revision_id": UUID("85000000-0000-0000-0000-000000000099"),
    }


def test_create_and_delete_field_scrubs_values_but_preserves_source_snapshot():
    roster = _roster()
    field_id = roster.custom_fields[0].definition_id

    deleted = build_custom_field_delete_revision(roster, field_id, **_arguments())

    assert deleted.removed_value_count == 1
    assert deleted.snapshot.custom_fields == ()
    assert deleted.snapshot.persons[0].profile.custom_values == {}
    assert field_id in roster.persons[0].profile.custom_values
    assert len(deleted.record.changes) == 2

    created = build_custom_field_create_revision(
        roster,
        "선호 시간",
        CustomFieldType.TEXT,
        definition_id=UUID("85000000-0000-0000-0000-000000000098"),
        **_arguments(),
    )
    assert created.definition is not None
    assert created.snapshot.custom_fields[-1].name == "선호 시간"


@pytest.mark.parametrize("name", ("", "성명", "조식", "배송 구역"))
def test_create_rejects_blank_duplicate_and_reserved_labels(name):
    with pytest.raises(ValueError):
        build_custom_field_create_revision(
            _roster(),
            name,
            CustomFieldType.TEXT,
            definition_id=UUID("85000000-0000-0000-0000-000000000098"),
            **_arguments(),
        )


def test_delete_revision_persists_after_database_reopen_and_keeps_history(tmp_path):
    base = _roster()
    actor = RevisionActor("tester", "S-1-5-21-test")
    initial = RevisionRecord(
        RevisionMetadata(
            revision_id=base.revision_id,
            parent_revision_id=None,
            recorded_at=datetime(2026, 8, 26, 12, 0, tzinfo=UTC),
            effective_on=date(2026, 8, 26),
            actor=actor,
            reason="initial roster",
            source=RevisionSource.IMPORT,
        ),
        roster_audit_changes(
            RosterSnapshot(UUID("85000000-0000-0000-0000-000000000010")),
            base,
        ),
    )
    path = tmp_path / "custom-fields.sqlite3"
    with DatabaseSession.create(path, "password", "recovery") as session:
        session.append_revision(initial)
        deleted = build_custom_field_delete_revision(
            base,
            base.custom_fields[0].definition_id,
            **_arguments(),
        )
        session.append_revision(deleted.record)

    with DatabaseSession.open(path, "password") as reopened:
        current = reopened.database.current_roster()
        previous = reopened.database.snapshot(base.revision_id)
        assert current is not None
        assert current.custom_fields == ()
        assert current.persons[0].profile.custom_values == {}
        assert previous is not None
        assert previous.entity(
            "roster.custom_field_definition", base.custom_fields[0].definition_id
        )["name"] == "배송 구역"
