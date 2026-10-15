"""Revision-backed delivery-sequence input mode settings."""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from PySide6.QtWidgets import QApplication, QDialog

from src.desktop_v2.sequence_settings import (
    SequenceModeController,
    SequenceModeDialog,
)
from src.domain.projections import roster_audit_changes
from src.domain.revisions import (
    EntityRef,
    HistoricalSnapshot,
    RevisionActor,
    RevisionMetadata,
    RevisionRecord,
    RevisionSource,
)
from src.domain.roster import Person, PersonProfile, RosterSnapshot
from src.domain.sequence_settings import (
    SEQUENCE_MODE_SETTING_ID,
    SEQUENCE_MODE_SETTING_KIND,
    SequenceMode,
    build_sequence_mode_revision,
    sequence_mode_from_snapshot,
)
from src.storage.database_session import DatabaseSession


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_mode_defaults_to_separate_and_revision_changes_only_setting():
    current = UUID("2d7a0000-0000-0000-0000-000000000001")
    next_id = UUID("2d7a0000-0000-0000-0000-000000000002")
    assert sequence_mode_from_snapshot(HistoricalSnapshot(current, {})) is SequenceMode.SEPARATE
    prepared = build_sequence_mode_revision(
        current,
        None,
        SequenceMode.UNIFIED,
        actor=RevisionActor("tester", "test-sid"),
        recorded_at=datetime(2026, 10, 14, tzinfo=UTC),
        revision_id=next_id,
    )
    assert len(prepared.record.changes) == 1
    assert prepared.record.changes[0].entity_kind == SEQUENCE_MODE_SETTING_KIND
    assert prepared.record.changes[0].after == {"mode": "unified"}
    with pytest.raises(ValueError, match="현재와 같은"):
        build_sequence_mode_revision(
            current,
            {"mode": "unified"},
            SequenceMode.UNIFIED,
            actor=RevisionActor("tester", "test-sid"),
            recorded_at=datetime(2026, 10, 14, tzinfo=UTC),
            revision_id=next_id,
        )


def test_mode_roundtrips_in_encrypted_database(tmp_path):
    actor = RevisionActor("tester", "test-sid")
    initial_id = uuid4()
    roster = RosterSnapshot(initial_id, persons=(Person(uuid4(), PersonProfile("아동")),))
    empty = RosterSnapshot(uuid4())
    initial = RevisionRecord(
        RevisionMetadata(
            initial_id, None, datetime(2026, 10, 14, tzinfo=UTC), None,
            actor, "initial", RevisionSource.IMPORT,
        ),
        roster_audit_changes(empty, roster),
    )
    path = tmp_path / "sequence-mode.sqlite3"
    with DatabaseSession.create(path, "password", "recovery") as session:
        session.append_revision(initial)
        prepared = build_sequence_mode_revision(
            initial_id,
            None,
            SequenceMode.UNIFIED,
            actor=actor,
            recorded_at=datetime(2026, 10, 14, tzinfo=UTC),
            revision_id=uuid4(),
        )
        session.append_revision(prepared.record)
        saved_id = prepared.record.metadata.revision_id
    with DatabaseSession.open(path, "password") as session:
        assert sequence_mode_from_snapshot(session.database.snapshot(saved_id)) is SequenceMode.UNIFIED


def test_dialog_and_controller_cancel_noop_and_save(app):
    dialog = SequenceModeDialog(SequenceMode.SEPARATE)
    assert dialog.separate.isChecked()
    dialog.unified.setChecked(True)
    assert dialog.selected_mode() is SequenceMode.UNIFIED
    dialog.close()

    revision_id = uuid4()
    snapshot = HistoricalSnapshot(
        revision_id,
        {EntityRef(SEQUENCE_MODE_SETTING_KIND, SEQUENCE_MODE_SETTING_ID): {"mode": "separate"}},
    )
    roster = RosterSnapshot(revision_id)
    saved = []

    class Database:
        def current_roster(self):
            return roster

        def snapshot(self, _revision_id):
            return snapshot

    class Dialog:
        result = QDialog.DialogCode.Rejected
        mode = SequenceMode.SEPARATE

        def __init__(self, current, parent):
            assert current is SequenceMode.SEPARATE

        def exec(self):
            return self.result

        def selected_mode(self):
            return self.mode

    session = SimpleNamespace(
        database=Database(),
        append_revision=lambda record: saved.append(record) or SimpleNamespace(error=None),
    )
    controller = SequenceModeController(
        session,
        dialog_type=Dialog,
        actor_provider=lambda: RevisionActor("tester", "test-sid"),
        recorded_at_provider=lambda: datetime(2026, 10, 14, tzinfo=UTC),
    )
    assert not controller.run().completed
    Dialog.result = QDialog.DialogCode.Accepted
    assert not controller.run().completed
    assert saved == []
    Dialog.mode = SequenceMode.UNIFIED
    assert controller.run().completed
    assert len(saved) == 1
    assert saved[0].changes[0].after == {"mode": "unified"}
