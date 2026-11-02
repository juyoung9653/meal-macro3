"""Manual conversion never touches live addresses, credentials or databases."""
from dataclasses import replace
from datetime import UTC, date, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from src.domain.roster import CustomFieldDefinition, CustomFieldType, Person, PersonProfile, RosterSnapshot
from src.domain.revisions import RevisionActor, RevisionMetadata, RevisionRecord, RevisionSource
from src.domain.projections import roster_audit_changes
from src.services.juso_api import AddressNotFoundError, JusoServiceError
from src.services.juso_credentials import CredentialStore, CredentialStoreError, SERVICE_NAME
from src.services.old_address_update import NEW_COLUMN, prepare_address_update, validate_columns
from src.storage.database_session import DatabaseSession
from src.storage.encrypted_database import RevisionConflictError

DAY = date(2026, 10, 22)
ACTOR = RevisionActor("test", "test-sid")


@pytest.fixture
def roster():
    field = CustomFieldDefinition(uuid4(), "구주소", CustomFieldType.TEXT, 0)
    return RosterSnapshot(uuid4(), custom_fields=(field,), persons=tuple(
        Person(uuid4(), PersonProfile(name, road_address=address, detail_address="101호",
               custom_values={} if old is None else {field.definition_id: old}))
        for name, address, old in [("예제1", "가상로 1", None), ("예제2", "가상로 2", "기존 주소"),
                                  ("예제3", "가상로 1", "   "), ("예제4", None, "유지"),
                                  ("예제5", "없는로 1", None)]))


class Client:
    def __init__(self):
        self.calls = []

    def lookup(self, address):
        self.calls.append(address)
        if address == "경기도 성남시 없는로 1":
            raise AddressNotFoundError("검색 결과가 없습니다")
        return SimpleNamespace(jibun_address="구주소 " + address)


def prepare(roster, client=None, **kwargs):
    options = dict(source="address", target=f"custom:{roster.custom_fields[0].definition_id}",
                   only_empty=True, service_day=DAY, actor=ACTOR, recorded_at=datetime.now(UTC))
    options.update(kwargs)
    return prepare_address_update(roster, None, client or Client(), **options)


def seed(session, roster):
    session.append_revision(RevisionRecord(RevisionMetadata(roster.revision_id, None, datetime.now(UTC),
        DAY, ACTOR, "예제 명단", RevisionSource.UI), roster_audit_changes(RosterSnapshot(uuid4()), roster)))


def test_empty_only_preserves_filled_failed_and_source_and_deduplicates(roster):
    client = Client()
    result = prepare(roster, client)
    assert (result.updated, result.skipped, len(result.failures)) == (2, 2, 1)
    assert client.calls == ["경기도 성남시 가상로 1", "경기도 성남시 없는로 1"]
    for before, after in zip(roster.persons, result.roster.persons):
        assert before.profile.road_address == after.profile.road_address
        assert after.profile.detail_address == "101호"
    assert result.roster.persons[1] == roster.persons[1]
    assert result.roster.persons[4] == roster.persons[4]
    assert result.record.metadata.parent_revision_id == roster.revision_id


def test_all_replaces_filled_but_failure_keeps_existing(roster):
    field = roster.custom_fields[0].definition_id
    last = replace(roster.persons[-1], profile=replace(roster.persons[-1].profile, custom_values={field: "검색 실패시 유지"}))
    roster = replace(roster, persons=(*roster.persons[:-1], last))
    result = prepare(roster, only_empty=False)
    assert result.updated == 3
    assert result.roster.persons[1].profile.custom_values[field] == "구주소 경기도 성남시 가상로 2"
    assert result.roster.persons[-1] == last
    again = prepare(result.roster, only_empty=False)
    assert again.record is None
    assert again.unchanged == 3


def test_new_column_created_only_when_successful(roster):
    result = prepare(roster, target=NEW_COLUMN)
    assert result.roster.custom_fields[-1].name == "구주소2"
    assert result.updated == 3
    empty = replace(roster, persons=())
    result = prepare(empty, target=NEW_COLUMN)
    assert result.record is None and result.roster is empty


def test_invalid_columns_and_numeric_output_rejected(roster):
    key = f"custom:{roster.custom_fields[0].definition_id}"
    with pytest.raises(ValueError, match="서로"):
        validate_columns(roster, key, key)
    with pytest.raises(ValueError):
        validate_columns(roster, "address", "address")
    numeric = replace(roster.custom_fields[0], field_type=CustomFieldType.NUMBER)
    with pytest.raises(ValueError):
        validate_columns(replace(roster, custom_fields=(numeric,), persons=()), "address", key)


def test_fatal_error_aborts_after_partial_lookup(roster):
    class FailingClient(Client):
        def lookup(self, address):
            if self.calls:
                raise JusoServiceError("API 키 오류")
            return super().lookup(address)
    with pytest.raises(JusoServiceError):
        prepare(roster, FailingClient(), only_empty=False)
    assert not roster.persons[0].profile.custom_values


def test_custom_source_and_input_rules(roster, monkeypatch):
    from src.domain.column_input_rules import InputRule
    from src.services import old_address_update as module
    monkeypatch.setattr(module, "load_rules", lambda _: {f"custom:{roster.custom_fields[0].definition_id}": InputRule(max_length=2)})
    result = prepare(roster)
    assert result.record is None and len(result.failures) == 3
    result = prepare(roster, source=f"custom:{roster.custom_fields[0].definition_id}", target=NEW_COLUMN)
    assert result.updated == 2


def test_revision_persists_reopens_and_rejects_conflict_atomically(roster, tmp_path):
    path = tmp_path / "addresses.sqlite3"
    (tmp_path / "backup").mkdir()
    with DatabaseSession.create(path, "test-password", backup_directory=tmp_path / "backup") as session:
        seed(session, roster)
        roster = session.database.current_roster()
        result = prepare(roster, target=NEW_COLUMN)
        backup = session.append_revision(result.record)
        assert backup.succeeded
        assert session.database.current_roster() == result.roster
        stale = prepare(roster)
        with pytest.raises(RevisionConflictError):
            session.append_revision(stale.record)
    with DatabaseSession.open(path, "test-password") as session:
        assert session.database.current_roster() == result.roster
        assert {p.person_id: p.profile.road_address for p in session.database.current_roster().persons} == {
            p.person_id: p.profile.road_address for p in roster.persons}


class Vault:
    CRED_TYPE_GENERIC = 1
    CRED_PERSIST_LOCAL_MACHINE = 2

    def __init__(self):
        self.record = None

    def CredRead(self, target, kind):
        assert target == SERVICE_NAME
        if self.record is None:
            error = OSError("missing")
            error.winerror = 1168
            raise error
        return self.record

    def CredWrite(self, record, flags):
        # Match pywin32: writes require text; reads return UTF-16 bytes.
        if not isinstance(record["CredentialBlob"], str):
            raise TypeError("CredentialBlob must be Unicode")
        self.record = {**record, "CredentialBlob": record["CredentialBlob"].encode("utf-16-le")}

    def CredDelete(self, target, kind, flags):
        self.record = None


def test_credentials_use_only_user_local_windows_vault():
    vault = Vault()
    store = CredentialStore(vault)
    assert store.get_api_key() is None
    store.save_api_key(" synthetic-key ")
    assert vault.record["Persist"] == vault.CRED_PERSIST_LOCAL_MACHINE
    assert store.get_api_key() == "synthetic-key"
    with pytest.raises(CredentialStoreError):
        store.save_api_key(" ")
    assert store.get_api_key() == "synthetic-key"
    store.delete_api_key()
    assert store.get_api_key() is None


@pytest.fixture
def app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_dialog_defaults_validates_and_keeps_cancel_without_calls(app, roster):
    from PySide6.QtWidgets import QDialog
    from src.desktop_v2.old_address import OldAddressDialog
    from src.desktop_v2.roster_window import RosterTableModel
    model = RosterTableModel(roster)
    dialog = OldAddressDialog(roster, model.columns, DAY)
    assert dialog.empty_only.isChecked()
    assert dialog.source_combo.currentData() == "address"
    dialog.source_combo.setCurrentIndex(dialog.source_combo.findData(dialog.target_combo.currentData()))
    dialog.accept()
    assert dialog.result() != QDialog.Accepted and "서로" in dialog.error_label.text()
    dialog.reject()
    assert dialog.result() == QDialog.Rejected


@pytest.mark.parametrize("action_name", ["JUSO_KEY", "OLD_ADDRESS"])
def test_actual_settings_entry_routes_to_address_dialog(app, roster, monkeypatch, action_name):
    from PySide6.QtWidgets import QDialog
    from src.desktop_v2 import old_address
    from src.desktop_v2.application import DesktopStartupController
    from src.desktop_v2.settings_hub import SettingsAction
    from src.desktop_v2.roster_window import RosterWindow
    monkeypatch.setattr(old_address, "CredentialStore", lambda: CredentialStore(Vault()))
    action = getattr(SettingsAction, action_name)
    controller = DesktopStartupController.__new__(DesktopStartupController)
    controller._settings_hub_dialog_type = lambda parent: SimpleNamespace(selected_action=action)
    session = SimpleNamespace(database=SimpleNamespace(current_roster=lambda: roster, snapshot=lambda _: None))
    window = RosterWindow(roster)
    steps = controller._run_settings_hub.steps(controller, session, window)
    try:
        next(steps)
        request = steps.send(QDialog.Accepted)
        expected = old_address.JusoKeyDialog if action_name == "JUSO_KEY" else old_address.OldAddressDialog
        assert isinstance(request.dialog, expected)
        request.dialog.reject()
    finally:
        steps.close()
        window.close()


def test_controller_commits_once_refreshes_before_report_and_cancel_does_not_write(app, roster, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QDialog
    from src.desktop_v2 import old_address
    from src.desktop_v2.roster_window import RosterTableModel
    monkeypatch.setattr(old_address, "save_revision", lambda session, record, **kwargs: session.append_revision(record))
    store = SimpleNamespace(get_api_key=lambda: "synthetic-key")
    with DatabaseSession.create(tmp_path / "controller.sqlite3", "password") as session:
        seed(session, roster)
        controller = old_address.OldAddressController(session, store=store, client_type=lambda key: Client())
        displayed = []
        controller.on_saved = displayed.append
        steps = controller.run.steps(controller, RosterTableModel(roster).columns, DAY)
        request = next(steps)
        with pytest.raises(StopIteration):
            steps.send(QDialog.Rejected)
        assert session.database.current_roster().revision_id == roster.revision_id
        steps = controller.run.steps(controller, RosterTableModel(roster).columns, DAY)
        next(steps)
        report = steps.send(QDialog.Accepted)
        assert displayed and displayed[0].revision_id != roster.revision_id
        assert report.dialog.windowTitle() == "구주소 업데이트 결과"
        saved = session.database.current_roster()
        assert saved.person(roster.persons[1].person_id) == roster.persons[1]
        with pytest.raises(StopIteration):
            steps.send(QDialog.Accepted)
        assert session.database.current_roster().revision_id == saved.revision_id


@pytest.mark.parametrize("only_empty", [True, False])
def test_progress_counts_cached_skipped_failed_and_unchanged_rows(roster, only_empty):
    progress = []
    client = Client()
    result = prepare(roster, client, only_empty=only_empty,
                     on_progress=lambda done, total: progress.append((done, total)))
    assert progress == [(done, 5) for done in range(6)]
    assert result.updated + result.unchanged + result.skipped + len(result.failures) == 5
    assert len(client.calls) == (2 if only_empty else 3)
    progress.clear()
    repeated = prepare(result.roster, only_empty=only_empty,
                       on_progress=lambda done, total: progress.append((done, total)))
    assert repeated.record is None
    assert progress == [(done, 5) for done in range(6)]


def test_empty_progress_and_fatal_error_do_not_claim_incomplete_work_finished(roster):
    progress = []
    prepare(replace(roster, persons=()), on_progress=lambda done, total: progress.append((done, total)))
    assert progress == [(0, 0)]
    progress.clear()

    class FailingClient(Client):
        def lookup(self, address):
            if self.calls:
                raise JusoServiceError("합성 오류")
            return super().lookup(address)

    with pytest.raises(JusoServiceError):
        prepare(roster, FailingClient(), only_empty=False,
                on_progress=lambda done, total: progress.append((done, total)))
    assert progress == [(0, 5), (1, 5)]
    assert not roster.persons[0].profile.custom_values


@pytest.mark.parametrize("fail", [False, True])
def test_address_progress_updates_on_ui_thread_and_restores_parent(app, fail):
    from threading import Event, get_ident
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QDialog
    from src.desktop_v2.old_address import AddressUpdateProgressDialog, run_address_update

    released = Event()
    ui_thread = get_ident()
    observed = []
    parent = QDialog()
    parent.show()

    def task(progress):
        assert get_ident() != ui_thread
        progress(0, 5)
        progress(2, 5)
        assert released.wait(5), "Progress UI did not respond"
        if fail:
            raise RuntimeError("synthetic lookup failure")
        progress(5, 5)
        return "converted"

    def inspect():
        if released.is_set():
            return
        dialog = next((w for w in app.topLevelWidgets()
                       if isinstance(w, AddressUpdateProgressDialog) and w.isVisible()), None)
        if dialog is None or dialog.progress.value() != 2:
            return
        observed.append((dialog.progress.text(), parent.isEnabled(), get_ident()))
        dialog.reject()
        assert not dialog.close()
        assert dialog.isVisible()
        released.set()

    timer = QTimer()
    timer.timeout.connect(inspect)
    timer.start(10)
    try:
        if fail:
            with pytest.raises(RuntimeError, match="synthetic lookup failure"):
                run_address_update(task, total=5, parent=parent)
        else:
            assert run_address_update(task, total=5, parent=parent) == "converted"
        assert observed == [("처리 2 / 5명 · 40%", False, ui_thread)]
        assert parent.isEnabled()
        assert not any(isinstance(w, AddressUpdateProgressDialog) and w.isVisible()
                       for w in app.topLevelWidgets())
    finally:
        timer.stop()
        released.set()
        parent.close()


@pytest.mark.parametrize("road,expected", [
    ("  분당구 가상로 1  ", "경기도 성남시 분당구 가상로 1"),
    ("경기도 성남시 분당구 가상로 1", "경기도 성남시 분당구 가상로 1"),
    ("성남시 분당구 가상로 1", "경기도 성남시 분당구 가상로 1"),
    ("경기도   성남시  가상로 1", "경기도 성남시 가상로 1"),
    ("  ", None),
    (None, None),
])
def test_single_source_adds_region_once_ignores_jurisdiction_and_skips_empty(roster, road, expected):
    person = replace(roster.persons[0], profile=replace(roster.persons[0].profile,
                     jurisdiction="가상동", road_address=road))
    roster = replace(roster, persons=(person,))
    client = Client()
    result = prepare(roster, client)
    assert client.calls == ([] if expected is None else [expected])
    assert (result.updated, result.skipped) == ((0, 1) if expected is None else (1, 0))
    if expected is not None:
        assert result.roster.persons[0].profile.custom_values[roster.custom_fields[0].definition_id] == "구주소 " + expected
        decision = result.record.metadata.decisions["old_address_update"]
        assert (decision["source"], decision["search_region"]) == ("address", "경기도 성남시")
    assert result.roster.persons[0].profile.jurisdiction == person.profile.jurisdiction
    assert result.roster.persons[0].profile.road_address == person.profile.road_address
    assert roster.persons[0] is person


def test_region_cache_reuses_equivalent_queries_and_supports_custom_source(roster):
    persons = tuple(replace(p, profile=replace(p.profile, jurisdiction=district,
                    road_address=road, custom_values={}))
                    for p, district, road in zip(roster.persons,
                        ["가상동", "다른동", "가상동"],
                        ["가상로 1", "경기도 성남시 가상로 1", "성남시 가상로 1"]))
    client = Client()
    result = prepare(replace(roster, persons=persons), client)
    assert client.calls == ["경기도 성남시 가상로 1"]
    assert result.updated == 3
    client = Client()
    prepare(replace(roster, persons=(roster.persons[1],)), client,
            source=f"custom:{roster.custom_fields[0].definition_id}", target=NEW_COLUMN)
    assert client.calls == ["경기도 성남시 기존 주소"]


def test_dialog_has_one_source_and_explains_region(app, roster):
    from PySide6.QtWidgets import QComboBox, QDialog, QLabel
    from src.desktop_v2.old_address import OldAddressDialog
    from src.desktop_v2.roster_window import RosterTableModel

    dialog = OldAddressDialog(roster, RosterTableModel(roster).columns, DAY)
    assert len(dialog.findChildren(QComboBox)) == 2  # One input and one output.
    assert dialog.source_combo.currentData() == "address"
    assert any("경기도 성남시" in label.text() for label in dialog.findChildren(QLabel))
    dialog.accept()
    assert dialog.result() == QDialog.Accepted
