from uuid import UUID

import pytest
from PySide6.QtWidgets import QApplication, QDialog

from src.desktop_v2.provider_settings import (
    ProviderSettingsController,
    ProviderSettingsDialog,
)
from src.domain.provider_settings import PROVIDER_SETTING_ID, PROVIDER_SETTING_KIND
from src.domain.revisions import EntityRef, HistoricalSnapshot
from src.domain.roster import RosterSnapshot


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_provider_dialog_exposes_friendly_global_copy_and_allows_explicit_blank(app):
    dialog = ProviderSettingsDialog("기존센터", legacy_mixed=True)
    assert dialog.windowTitle() == "공통 제공기관"
    editor = dialog.findChild(type(dialog._provider), "commonProvider")
    assert editor is not None
    assert editor.text() == "기존센터"
    editor.clear()
    assert dialog.provider() == ""


def test_controller_explicit_unchanged_value_does_not_append():
    revision_id = UUID("d2000000-0000-0000-0000-000000000001")
    snapshot = HistoricalSnapshot(
        revision_id,
        {EntityRef(PROVIDER_SETTING_KIND, PROVIDER_SETTING_ID): {"provider": "센터"}},
    )

    class Database:
        def current_roster(self):
            return RosterSnapshot(revision_id)

        def snapshot(self, _revision_id):
            return snapshot

    class Session:
        database = Database()

        def append_revision(self, _record):
            raise AssertionError("must not append")

    class Dialog:
        def __init__(self, *_args):
            pass

        def exec(self):
            return QDialog.DialogCode.Accepted

        def provider(self):
            return "센터"

    outcome = ProviderSettingsController(Session(), dialog_type=Dialog).run()
    assert not outcome.completed
    assert outcome.roster is None


@pytest.mark.parametrize(
    "mode", ["cancel", "legacy_same", "save_error", "backup_error"]
)
def test_controller_cancel_legacy_migration_and_save_failures(mode):
    from datetime import UTC, datetime
    from types import SimpleNamespace
    from uuid import uuid4

    from src.domain.projections import roster_entity_states
    from src.domain.revisions import RevisionActor
    from src.domain.roster import Person, PersonProfile

    roster = RosterSnapshot(
        uuid4(), persons=(Person(uuid4(), PersonProfile("아동", provider="센터")),)
    )
    snapshot = HistoricalSnapshot(roster.revision_id, roster_entity_states(roster))
    saved, errors, warnings = [], [], []
    database = SimpleNamespace(
        current_roster=lambda: roster, snapshot=lambda _: snapshot
    )

    def append(record):
        if mode == "save_error":
            raise OSError("저장 실패")
        saved.append(record)
        return SimpleNamespace(
            error=OSError("백업 실패") if mode == "backup_error" else None
        )

    class Dialog:
        def __init__(self, current, mixed, parent):
            assert current == "센터"
            assert not mixed

        def exec(self):
            return (
                QDialog.DialogCode.Rejected
                if mode == "cancel"
                else QDialog.DialogCode.Accepted
            )

        def provider(self):
            return "센터"

    outcome = ProviderSettingsController(
        SimpleNamespace(database=database, append_revision=append),
        dialog_type=Dialog,
        actor_provider=lambda: RevisionActor("담당자", "test-sid"),
        recorded_at_provider=lambda: datetime(2026, 9, 9, tzinfo=UTC),
        message_box=SimpleNamespace(
            critical=lambda *args: errors.append(args),
            warning=lambda *args: warnings.append(args),
        ),
    ).run()
    assert outcome.completed == (mode in ("legacy_same", "backup_error"))
    assert len(saved) == int(outcome.completed)
    assert len(errors) == int(mode == "save_error")
    assert len(warnings) == int(mode == "backup_error")
    if saved:
        assert saved[0].changes[0].after == {"provider": "센터"}
        assert saved[0].changes[0].before is None
