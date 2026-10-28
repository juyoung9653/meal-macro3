"""Profile refresh keeps unaffected contexts and correct Qt/model behavior."""
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QApplication

from src.desktop_v2.application import DesktopStartupController
from src.desktop_v2.column_layout import RosterColumnLayout
from src.desktop_v2.roster_cell_edit import RosterCellEditController
from src.desktop_v2.roster_window import RosterTableModel
from src.domain.column_input_rules import RULES_KIND
from src.domain.revisions import HistoricalSnapshot, RevisionActor
from src.domain.roster import Person, PersonProfile, RosterSnapshot
from tests.test_roster_column_layout import make_view


@pytest.fixture(scope="session")
def app():
    application = QApplication.instance() or QApplication([])
    from src.desktop_v2.theme import register_application_fonts
    register_application_fonts()
    return application


def roster():
    return RosterSnapshot(uuid4(), persons=(
        Person(uuid4(), PersonProfile("Alpha")),
        Person(uuid4(), PersonProfile("Beta")),
    ))


def edited(source, **values):
    first = source.persons[0]
    return replace(source, revision_id=uuid4(), persons=(
        replace(first, profile=replace(first.profile, **values)), source.persons[1],
    ))


def test_profile_refresh_notifies_changed_row_and_reuses_other_context(app):
    source = roster()
    model = RosterTableModel(source)
    resets, changed = [], []
    model.modelReset.connect(lambda: resets.append(True))
    model.dataChanged.connect(lambda first, last, *_: changed.append((first.row(), last.row())))
    unaffected = model._contexts[source.persons[1].person_id]
    model.replace_roster(edited(source, note="saved"))
    assert resets == [] and changed == [(0, 0)]
    assert model._contexts[source.persons[1].person_id] is unaffected
    assert model.person_at(0).profile.note == "saved"


def test_profile_refresh_reorders_and_filters_after_name_change(app):
    source = roster()
    model = RosterTableModel(source)
    model.sort(0, Qt.SortOrder.AscendingOrder)
    model.replace_roster(edited(source, name="Zulu"))
    assert [model.person_at(i).profile.name for i in range(2)] == ["Beta", "Zulu"]
    model.set_search("Zulu")
    model.replace_roster(edited(source, name="Gamma"))
    assert model.rowCount() == 0


def test_optional_profile_column_added_and_removed(app):
    source = roster()
    model = RosterTableModel(source)
    assert "school_type" not in {c.key for c in model.columns}
    model.replace_roster(edited(source, school_type="primary"))
    assert "school_type" in {c.key for c in model.columns}
    model.replace_roster(source)
    assert "school_type" not in {c.key for c in model.columns}


def test_unchanged_layout_does_not_sync_but_width_change_is_persisted(app, tmp_path):
    class Settings(QSettings):
        syncs = 0

        def sync(self):
            self.syncs += 1
            super().sync()

    settings = Settings(str(tmp_path / "layout.ini"), QSettings.Format.IniFormat)
    view, model = make_view()
    layout = RosterColumnLayout(view, model.columns, settings=settings)
    layout.save()
    synced = settings.syncs
    layout.save()
    layout.save()
    assert settings.syncs == synced
    view.horizontalHeader().resizeSection(0, 177)
    layout.save()
    assert settings.syncs > synced
    persisted = QSettings(str(tmp_path / "layout.ini"), QSettings.Format.IniFormat)
    assert '177' in persisted.value(layout.SETTINGS_KEY)


def test_startup_reads_display_kinds_and_metadata_without_full_history(app):
    source = roster()
    reads = []

    class Database:
        def current_roster(self):
            return source

        def snapshot_for_kinds(self, revision_id, kinds):
            reads.append((revision_id, kinds))
            return HistoricalSnapshot(revision_id, {})

        def revision_metadata(self, revision_id):
            assert revision_id == source.revision_id
            return SimpleNamespace(recorded_at=datetime(2026, 10, 21, tzinfo=UTC))

        def status_events(self):
            return ()

    controller = DesktopStartupController.__new__(DesktopStartupController)
    prepared = controller._prepare_startup(SimpleNamespace(database=Database()))
    assert prepared.roster == source
    assert len(reads) == 1 and reads[0][0] == source.revision_id
    assert RULES_KIND in reads[0][1]
    assert "roster.person" not in reads[0][1]


def test_cell_validation_reads_only_input_rules(app):
    source = roster()
    reads, saved = [], []

    class Database:
        def current_roster(self):
            return source

        def snapshot_for_kinds(self, revision_id, kinds):
            reads.append(kinds)
            return HistoricalSnapshot(revision_id, {})

    def append(record):
        saved.append(record)
        return SimpleNamespace(error=None)

    session = SimpleNamespace(database=Database(), append_revision=append)
    result = RosterCellEditController(
        session, actor_provider=lambda: RevisionActor("test", "test"),
    ).save(source.persons[0].person_id, "note", "saved")
    assert result.completed and len(saved) == 1
    assert reads == [(RULES_KIND,)]


def test_profile_refresh_reuses_histories_but_structural_change_reloads(app, tmp_path, monkeypatch):
    from tests.test_encrypted_database import KEY, _roster_record, _roster_snapshot
    from src.storage.encrypted_database import EncryptedDatabase
    source = _roster_snapshot(uuid4())
    with EncryptedDatabase.create(tmp_path / "db.sqlite3", KEY) as database:
        database.append_revision(_roster_record(source))
        session = SimpleNamespace(database=database)
        controller = DesktopStartupController.__new__(DesktopStartupController)
        first = controller._prepare_startup(session)
        changed = replace(
            source, revision_id=uuid4(), persons=(replace(source.persons[0],
                profile=replace(source.persons[0].profile, note="saved")),))
        database.append_revision(_roster_record(changed, before=source))
        reads = []
        original = database.status_events
        def tracked():
            reads.append(True)
            return original()
        monkeypatch.setattr(database, "status_events", tracked)
        second = controller._prepare_startup(session, changed)
        assert reads == []
        assert second.histories is first.histories
        assert second.roster == changed and second.snapshot.revision_id == changed.revision_id
        structural = replace(changed, revision_id=uuid4(), persons=(*changed.persons,
            Person(uuid4(), PersonProfile("new person"))))
        database.append_revision(_roster_record(structural, before=changed))
        third = controller._prepare_startup(session, structural)
        assert reads == [True]
        assert third.histories is not second.histories
        assert len(third.histories) > len(second.histories)
