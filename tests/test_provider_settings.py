from datetime import UTC, datetime
from uuid import UUID

import pytest

from src.domain.projections import roster_audit_changes
from src.domain.provider_settings import (
    PROVIDER_SETTING_ID,
    PROVIDER_SETTING_KIND,
    build_provider_revision,
    provider_from_snapshot,
)
from src.domain.revisions import (
    EntityRef,
    HistoricalSnapshot,
    RevisionActor,
    RevisionMetadata,
    RevisionRecord,
    RevisionSource,
)
from src.domain.roster import Person, PersonProfile, RosterSnapshot
from src.storage import DatabaseSession

CURRENT = UUID("d1000000-0000-0000-0000-000000000001")
NEXT = UUID("d1000000-0000-0000-0000-000000000002")


def test_provider_from_snapshot_returns_explicit_blank_and_saved_value():
    snapshot = HistoricalSnapshot(
        CURRENT,
        {EntityRef(PROVIDER_SETTING_KIND, PROVIDER_SETTING_ID): {"provider": ""}},
    )
    assert provider_from_snapshot(snapshot) == ""

    saved = HistoricalSnapshot(
        CURRENT,
        {
            EntityRef(PROVIDER_SETTING_KIND, PROVIDER_SETTING_ID): {
                "provider": "행복센터"
            }
        },
    )
    assert provider_from_snapshot(saved) == "행복센터"


def test_provider_revision_records_one_global_entity_without_roster_rewrites():
    prepared = build_provider_revision(
        CURRENT,
        None,
        "행복센터",
        actor=RevisionActor("tester", "S-1-5-21-test"),
        recorded_at=datetime(2026, 8, 26, tzinfo=UTC),
        revision_id=NEXT,
    )
    assert len(prepared.record.changes) == 1
    change = prepared.record.changes[0]
    assert change.entity_kind == PROVIDER_SETTING_KIND
    assert change.entity_id == PROVIDER_SETTING_ID
    assert change.after == {"provider": "행복센터"}

    with pytest.raises(ValueError, match="현재와 같은"):
        build_provider_revision(
            CURRENT,
            {"provider": "행복센터"},
            "행복센터",
            actor=RevisionActor("tester", "S-1-5-21-test"),
            recorded_at=datetime(2026, 8, 26, tzinfo=UTC),
            revision_id=NEXT,
        )


def test_encrypted_database_reopen_preserves_old_legacy_snapshot_and_new_provider(
    tmp_path,
):
    first_person = UUID("d1000000-0000-0000-0000-000000000010")
    second_person = UUID("d1000000-0000-0000-0000-000000000011")
    base = RosterSnapshot(
        CURRENT,
        persons=(
            Person(first_person, PersonProfile("첫째", provider="기존A")),
            Person(second_person, PersonProfile("둘째", provider="기존B")),
        ),
    )
    actor = RevisionActor("tester", "S-1-5-21-test")
    initial = RevisionRecord(
        RevisionMetadata(
            CURRENT,
            None,
            datetime(2026, 8, 26, tzinfo=UTC),
            None,
            actor,
            "initial",
            RevisionSource.IMPORT,
        ),
        roster_audit_changes(
            RosterSnapshot(UUID("d1000000-0000-0000-0000-000000000099")), base
        ),
    )
    path = tmp_path / "provider.sqlite3"
    with DatabaseSession.create(path, "password", "recovery") as session:
        session.append_revision(initial)
        old_snapshot = session.database.snapshot(CURRENT)
        assert provider_from_snapshot(old_snapshot) == ""
        prepared = build_provider_revision(
            CURRENT,
            None,
            "공통센터",
            actor=actor,
            recorded_at=datetime(2026, 8, 27, tzinfo=UTC),
            revision_id=NEXT,
        )
        session.append_revision(prepared.record)
    with DatabaseSession.open(path, "password") as session:
        assert provider_from_snapshot(session.database.snapshot(CURRENT)) == ""
        assert provider_from_snapshot(session.database.snapshot(NEXT)) == "공통센터"
