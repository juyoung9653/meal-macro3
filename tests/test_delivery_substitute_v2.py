"""Synthetic SQLite substitute documents: source semantics, layout, publication and UI."""
from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from dataclasses import replace
from datetime import UTC, date, datetime
from io import BytesIO
from threading import Event, get_ident
from uuid import UUID, uuid4

import apsw
import msoffcrypto
import openpyxl
import pytest
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication, QDialog

from src.desktop_v2.delivery_substitute import DeliverySubstituteDialog
from src.desktop_v2.document_generation import DocumentGenerationController
from src.desktop_v2.document_hub import DocumentAction
from src.desktop_v2.document_meal import DocumentMealSelection
from src.desktop_v2.document_output import DocumentOutputMode, next_available_document_output
from src.desktop_v2.document_period import DocumentPeriodSelection
from src.domain.delivery_substitute import SubstituteOptions
from src.domain.delivery_routing import DRIVER_KIND, ROUTING_EVENT_KIND
from src.domain.documents import DocumentKind, DocumentRun
from src.domain.export_security import EXPORT_PASSWORD_SETTING_KIND, EXPORT_PASSWORD_SETTING_ID, ExportPasswordSetting, export_password_setting_state
from src.domain.projections import roster_entity_states, status_event_state
from src.domain.revisions import AuditChange, ChangeOperation, EntityRef, HistoricalSnapshot, RevisionActor, RevisionMetadata, RevisionRecord, RevisionSource
from src.domain.roster import CustomFieldDefinition, CustomFieldType, DeliveryAssignment, Household, HouseholdDeliverySetting, HouseholdMembership, InclusiveDateRange, Meal, MealEnrollment, Person, PersonProfile, RosterSnapshot
from src.domain.status import MealStatus, StatusEvent
from src.generators.substitute_generator import _build_sheet, build_substitute_workbook, make_substitute_publication
from src.services.sqlite_delivery_substitute_service import prepare_delivery_substitute_data
from src.services.revision_restore import create_revision_branch, restore_revision
from src.storage.database_session import DatabaseSession, AutomaticBackupResult
from src.storage.encrypted_database import EncryptedDatabase
from src.storage.migrations import MIGRATIONS, CURRENT_SCHEMA_VERSION

DAY = date(2026, 8, 28)  # Friday: bundled Friday/Saturday/Sunday breakfasts.
ACTOR = RevisionActor("synthetic", "S-1-5-test")
CUSTOM = UUID("73000000-0000-0000-0000-000000000001")


def source(*, sizes=(2, 1, 1), meal=Meal.BREAKFAST):
    revision = uuid4()
    households, persons, memberships, enrollments, assignments = [], [], [], [], []
    for group, size in enumerate(sizes):
        household = Household(uuid4(), (HouseholdDeliverySetting(meal, form="BOX"),) if group == 0 else ())
        households.append(household)
        for member in range(size):
            person = Person(uuid4(), PersonProfile(f"합성{group}-{member}", jurisdiction="가동" if group < 2 else "나동",
                road_address="테스트로 1", detail_address="001호", mobile_phone="010-0000-0000" if group < 2 else None,
                note="표준 비고", custom_values={CUSTOM: "=문자 그대로"}))
            persons.append(person)
            memberships.append(HouseholdMembership(person.person_id, household.household_id))
            enrollments.append(MealEnrollment(person.person_id, meal, InclusiveDateRange(date(2026, 8, 1), None)))
            assignments.append(DeliveryAssignment(person.person_id, meal, "합성기사", f"{group + 1:03d}"))
    roster = RosterSnapshot(revision, persons=tuple(persons), households=tuple(households), memberships=tuple(memberships),
        enrollments=tuple(enrollments), assignments=tuple(assignments),
        custom_fields=(CustomFieldDefinition(CUSTOM, "비고2", CustomFieldType.TEXT, 0),))
    return HistoricalSnapshot(revision, roster_entity_states(roster)), roster


def initial_record(snapshot):
    states = dict(snapshot.entities)
    states[EntityRef(EXPORT_PASSWORD_SETTING_KIND, EXPORT_PASSWORD_SETTING_ID)] = export_password_setting_state(ExportPasswordSetting(1, "sheet-password", snapshot.revision_id))
    return RevisionRecord(RevisionMetadata(snapshot.revision_id, None, datetime(2026, 8, 28, tzinfo=UTC), DAY, ACTOR, "합성 배송대타", RevisionSource.UI),
        tuple(AuditChange(ref.entity_kind, ref.entity_id, ChangeOperation.INSERT, None, state) for ref, state in states.items()))


def options(data, **kwargs):
    return SubstituteOptions(data.rows[0].driver_key, data.default_columns, **kwargs)


def selection_for(session, outputs):
    summary = session.database.revision_summaries()[0]
    period = DocumentPeriodSelection(DocumentAction.DELIVERY_SUBSTITUTE, summary, DAY, Meal.BREAKFAST, DAY, DAY)
    data = prepare_delivery_substitute_data(session.database.snapshot(summary.revision_id), DAY, Meal.BREAKFAST)
    return next_available_document_output(DocumentMealSelection(period, (Meal.BREAKFAST,), options(data)), outputs, DocumentOutputMode.SAVED_DEFAULT)


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def session(tmp_path):
    backups = tmp_path / "backups"
    backups.mkdir()
    with DatabaseSession.create(tmp_path / "source.sqlite3", "database-password", backup_directory=backups) as opened:
        snapshot, _ = source()
        opened.append_revision(initial_record(snapshot))
        yield opened


def test_physical_breakfast_household_ids_hidden_fields_and_box():
    snapshot, roster = source()
    data = prepare_delivery_substitute_data(snapshot, DAY, Meal.BREAKFAST)
    assert len(data.rows) == 4
    assert sum(row.entitlement_count for row in data.rows) == 12
    assert data.default_columns[-1] == f"custom:{CUSTOM}"
    assert len({row.household_id for row in data.rows}) == 3  # DB household identities remain available; display grouping uses phones.
    assert sum(row.box for row in data.rows) == 2
    wb, _, _ = _build_sheet(data, replace(options(data), columns=("name",)))
    ws = wb.active
    assert ws["B3"].value == 3
    assert "B3:B5" in {str(r) for r in ws.merged_cells.ranges}
    assert ws["A3"].border.left.style == "thick"
    assert ws["A5"].border.bottom.style == "thick"
    assert ws["A7"].value == ""
    assert ws["G10"].value == 4
    assert prepare_delivery_substitute_data(snapshot, date(2026, 8, 29), Meal.BREAKFAST).rows == ()
    wb.close()


def test_effective_routing_address_status_enrollment_and_revision():
    snapshot, roster = source(sizes=(1, 1, 1))
    states = dict(snapshot.entities)
    driver, event = uuid4(), uuid4()
    states[EntityRef(DRIVER_KIND, driver)] = {"name": "새 기사", "telephone": ""}
    states[EntityRef(ROUTING_EVENT_KIND, event)] = {
        "person_id": str(roster.persons[0].person_id), "effective_on": DAY.isoformat(), "position": 1,
        "driver_ids": {Meal.BREAKFAST.value: str(driver)}, "road_address": "새 도로 7", "detail_address": "002호", "jurisdiction": "새동"}
    status = StatusEvent(uuid4(), snapshot.revision_id, roster.persons[1].person_id, Meal.BREAKFAST, MealStatus.STOPPED,
                         InclusiveDateRange(DAY, None), datetime(2026, 8, 20, tzinfo=UTC))
    states[EntityRef("status.event", status.event_id)] = status_event_state(status)
    updated = HistoricalSnapshot(uuid4(), states)
    data = prepare_delivery_substitute_data(updated, DAY, Meal.BREAKFAST)
    row = next(row for row in data.rows if row.person_id == roster.persons[0].person_id)
    assert row.driver_key == f"driver:{driver}"
    assert row.values["road_address"] == "새 도로 7"
    assert row.values["jurisdiction"] == "새동"
    assert roster.persons[1].person_id not in {row.person_id for row in data.rows}
    assert len(prepare_delivery_substitute_data(snapshot, DAY, Meal.BREAKFAST).rows) == 3
    assert not prepare_delivery_substitute_data(snapshot, date(2026, 7, 24), Meal.BREAKFAST).rows


@pytest.mark.parametrize("orientation", ["portrait", "landscape", "auto"])
@pytest.mark.parametrize("split", ["none", "dong", "groups"])
def test_layout_options_group_breaks_and_long_text(orientation, split):
    snapshot, _ = source(sizes=(2, 1, 1))
    data = prepare_delivery_substitute_data(snapshot, DAY, Meal.BREAKFAST)
    long_text = "긴주소" * 200 + "\n마지막 주소"
    data = replace(data, rows=tuple(replace(row, values={**row.values, "road_address": long_text}) for row in data.rows))
    opts = replace(options(data), columns=("name", "road_address", "sequence"), orientation=orientation, split=split, groups_per_page=1)
    wb = build_substitute_workbook(data, opts)
    ws = wb.active
    assert ws.page_setup.orientation == ("landscape" if orientation == "auto" else orientation)
    assert str(ws.page_setup.paperSize) == ws.PAPERSIZE_A4
    assert ws.print_title_rows == "$1:$2"
    assert ws["B3"].value == long_text.replace("\n", " ")
    assert ws["B3"].alignment.wrap_text
    assert ws["C3"].value == "001"
    assert all(d.height <= 409 for d in ws.row_dimensions.values())
    ranges = [merged for merged in ws.merged_cells.ranges if merged.min_row >= 3]
    assert all(not (merged.min_row <= br.id < merged.max_row) for br in ws.row_breaks.brk for merged in ranges)
    assert bool(ws.row_breaks.brk) == (split != "none")
    wb.close()


def test_more_than_five_people_and_missing_driver_do_not_use_report_constraints():
    snapshot, roster = source(sizes=(6,))
    roster = replace(roster, assignments=tuple(replace(a, driver=None) for a in roster.assignments))
    data = prepare_delivery_substitute_data(HistoricalSnapshot(snapshot.revision_id, roster_entity_states(roster)), DAY, Meal.BREAKFAST)
    assert len(data.rows) == 6
    assert data.rows[0].driver_key == "unassigned"
    wb, _, _ = _build_sheet(data, options(data))
    assert wb.active["G9"].value == "6식"
    assert wb.active["H10"].value == 6
    wb.close()


def test_workbook_encrypts_preserves_text_and_never_overwrites(tmp_path):
    snapshot, _ = source()
    data = prepare_delivery_substitute_data(snapshot, DAY, Meal.BREAKFAST)
    path = tmp_path / "substitute.xlsx"
    result = make_substitute_publication(data, options(data), path, output_password="sheet-password", overwrite=False)
    original = path.read_bytes()
    assert result.password_protected
    with path.open("rb") as stream:
        office = msoffcrypto.OfficeFile(stream)
        office.load_key(password="sheet-password")
        plaintext = BytesIO()
        office.decrypt(plaintext)
    wb = openpyxl.load_workbook(plaintext)
    assert wb.active["A3"].value == "001"
    assert wb.active["G3"].value == "=문자 그대로"
    assert wb.active["G3"].data_type == "s"
    wb.close()
    with pytest.raises(FileExistsError):
        make_substitute_publication(data, options(data), path, output_password="sheet-password", overwrite=False)
    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]


class Messages:
    def __init__(self): self.errors, self.warnings = [], []
    def critical(self, *args): self.errors.append(args)
    def warning(self, *args): self.warnings.append(args)


def test_real_generation_run_backup_restore_branch_and_gui_worker(app, session, tmp_path):
    selection = selection_for(session, tmp_path)
    gui = get_ident()
    released = Event()
    calls, ticks = [], []
    timer = QTimer()
    timer.timeout.connect(lambda: (ticks.append(1), released.set()))
    timer.start(5)
    def writer(*args, **kwargs):
        calls.append(get_ident())
        assert released.wait(5)
        return make_substitute_publication(*args, **kwargs)
    messages = Messages()
    try:
        outcome = DocumentGenerationController(session, message_box=messages, actor_provider=lambda: ACTOR, substitute_writer=writer).run(selection)
    finally:
        timer.stop()
    assert outcome.completed and ticks and all(thread != gui for thread in calls)
    assert not messages.errors
    run = session.database.document_runs(kind=DocumentKind.DELIVERY_SUBSTITUTE)[0]
    assert run.template_manifest["substitute_options"]["groups_per_page"] == 18
    assert "sheet-password" not in str(run.template_manifest)
    assert outcome.backup_result.record is not None
    session.database.check_integrity()
    with DatabaseSession.restore_and_open_with_user_password(outcome.backup_result.record.path, tmp_path / "restored.sqlite3", "database-password") as restored:
        assert restored.database.document_runs()[0] == run
        restored.database.check_integrity()
    head = session.database.revision_summaries()[0].revision_id
    branch_result = create_revision_branch(session, head, head, ACTOR, tmp_path / "branch.sqlite3", "branch-password", name="합성 분기")
    with DatabaseSession.open(branch_result.path, "branch-password") as branch:
        assert branch.database.document_runs()[0] == run
        branch.database.check_integrity()
    # A revision with an opaque application setting exercises a real restore
    # while preserving every recorded generated file, including the new kind.
    setting_id = uuid4()
    later = uuid4()
    session.append_revision(RevisionRecord(
        RevisionMetadata(later, head, datetime(2026, 8, 29, tzinfo=UTC), DAY, ACTOR, "restore probe", RevisionSource.UI),
        (AuditChange("substitute.test_setting", setting_id, ChangeOperation.INSERT, None, {"value": "later"}),)))
    restored = restore_revision(session, head, later, ACTOR)
    assert restored.revision_id is not None
    assert session.database.document_runs()[0] == run
    session.database.check_integrity()


def test_failure_rolls_back_output_and_run_and_backup_only_failure_succeeds(app, session, tmp_path, monkeypatch):
    selection = selection_for(session, tmp_path)
    messages = Messages()
    original = session.append_document_run
    def fail(_run): raise OSError("ledger fail")
    monkeypatch.setattr(session, "append_document_run", fail)
    outcome = DocumentGenerationController(session, message_box=messages).run(selection)
    assert not outcome.completed and messages.errors
    assert not selection.output_path.exists()
    assert session.database.document_runs() == ()
    monkeypatch.setattr(session, "append_document_run", original)
    monkeypatch.setattr(session, "try_backup_now", lambda: AutomaticBackupResult(None, OSError("backup fail")))
    outcome = DocumentGenerationController(session, message_box=messages).run(selection)
    assert outcome.completed and messages.warnings
    assert len(session.database.document_runs()) == 1


def test_options_dialog_defaults_reorder_cancel_and_empty_columns(app, session):
    summary = session.database.revision_summaries()[0]
    period = DocumentPeriodSelection(DocumentAction.DELIVERY_SUBSTITUTE, summary, DAY, Meal.BREAKFAST, DAY, DAY)
    document = DocumentMealSelection(period, (Meal.BREAKFAST,))
    data = prepare_delivery_substitute_data(session.database.snapshot(summary.revision_id), DAY, Meal.BREAKFAST)
    dialog = DeliverySubstituteDialog(document, data)
    dialog.show()
    app.processEvents()
    assert dialog.options().columns == data.default_columns
    assert dialog.options().orientation == "portrait"
    assert [dialog.orientation.itemData(i) for i in range(dialog.orientation.count())] == ["portrait", "landscape"]
    dialog.columns.setCurrentRow(0)
    dialog._move(1)
    assert dialog.options().columns[1] == "sequence"
    dialog.orientation.setCurrentIndex(1)
    assert dialog.selection().substitute_options.orientation == "landscape"
    for index in range(dialog.columns.count()):
        dialog.columns.item(index).setCheckState(Qt.CheckState.Unchecked)
    assert not dialog.accept_button.isEnabled()
    assert "칼럼" in dialog.error.text()
    dialog.reject()
    assert dialog.result() == QDialog.DialogCode.Rejected
    assert document.substitute_options is None
    dialog.close()


def test_schema_eight_upgrade_preserves_old_runs_and_accepts_substitute(tmp_path, monkeypatch):
    import src.storage.encrypted_database as storage
    path = tmp_path / "old.sqlite3"
    key = bytes.fromhex("73" * 32)
    snapshot, _ = source()
    record = initial_record(snapshot)
    with monkeypatch.context() as old:
        old.setattr(storage, "MIGRATIONS", MIGRATIONS[:8])
        old.setattr(storage, "CURRENT_SCHEMA_VERSION", 8)
        with EncryptedDatabase.create(path, key) as database:
            database.append_revision(record)
            summary = database.revision_summaries()[0]
            old_run = DocumentRun(uuid4(), DocumentKind.INVOICE, snapshot.revision_id, summary.display_code,
                DAY, DAY, datetime(2026, 8, 28, tzinfo=UTC), ACTOR, {"version": 1}, {"version": 1}, "old.xlsx", "b" * 64, 80)
            database.append_document_run(old_run)
    with EncryptedDatabase.open(path, key) as database:
        assert database.schema_version == CURRENT_SCHEMA_VERSION
        assert database.document_runs() == (old_run,)
        run = replace(old_run, run_id=uuid4(), kind=DocumentKind.DELIVERY_SUBSTITUTE, output_filename="substitute.xlsx")
        database.append_document_run(run)
        assert database.document_runs() == (run, old_run)
        database.check_integrity()
        connection = database._require_connection()
        with pytest.raises(apsw.ConstraintError):
            connection.execute("DELETE FROM document_run WHERE run_id = ?", (str(run.run_id),))


def test_long_name_preserves_one_row_per_person_without_merging_names():
    snapshot, _ = source(sizes=(2,))
    data = prepare_delivery_substitute_data(snapshot, DAY, Meal.BREAKFAST)
    long_name = "긴이름" * 90
    data = replace(data, rows=tuple(replace(row, values={**row.values, "name": long_name + str(index)}) for index, row in enumerate(data.rows)))
    wb = build_substitute_workbook(data, replace(options(data), columns=("name",)))
    ws = wb.active
    name_ranges = sorted((cell_range.min_row, cell_range.max_row) for cell_range in ws.merged_cells.ranges if cell_range.min_col == 1 and cell_range.min_row >= 3)
    assert name_ranges == []
    assert ws["A3"].value == long_name + "0"
    assert ws["A4"].value == long_name + "1"
    wb.close()


@pytest.mark.parametrize("mode", ["accept", "cancel", "no_data"])
def test_actual_document_menu_prepares_in_worker_and_preserves_options(app, session, tmp_path, mode):
    from types import SimpleNamespace
    from src.desktop_v2.application import DesktopStartupController
    gui = get_ident()
    observed = []
    emitted = []
    actual_day = date(2026, 8, 29) if mode == "no_data" else DAY
    window = SimpleNamespace(table_model=SimpleNamespace(service_day=actual_day, meal=Meal.BREAKFAST),
                             document_output_selected=SimpleNamespace(emit=emitted.append))
    class Hub:
        selected_action = DocumentAction.DELIVERY_SUBSTITUTE
        def __init__(self, **kwargs): pass
        def exec(self): return QDialog.DialogCode.Accepted
    class Source:
        def __init__(self, summaries, **kwargs): self.selected_summary = summaries[0]
        def exec(self): return QDialog.DialogCode.Accepted
    class Period:
        def __init__(self, request, **kwargs): self.request = request
        def exec(self): return QDialog.DialogCode.Accepted
        def selected_period(self):
            r = self.request
            return DocumentPeriodSelection(r.action, r.source, actual_day, r.anchor_meal, actual_day, actual_day)
    class Meals:
        def __init__(self, period, **kwargs): self.period = period
        def exec(self): return QDialog.DialogCode.Accepted
        def selection(self): return DocumentMealSelection(self.period, (Meal.BREAKFAST,))
    class Substitute:
        def __init__(self, document, data, **kwargs):
            assert get_ident() == gui
            observed.append("options")
            self.document, self.data = document, data
        def exec(self): return QDialog.DialogCode.Rejected if mode == "cancel" else QDialog.DialogCode.Accepted
        def selection(self): return replace(self.document, substitute_options=replace(options(self.data), orientation="landscape"))
    class Output:
        def __init__(self, document, directory, **kwargs): self.document = document
        def exec(self): return QDialog.DialogCode.Accepted
        def selection(self): return next_available_document_output(self.document, tmp_path, DocumentOutputMode.SAVED_DEFAULT)
    class Password:
        def __init__(self, *_): pass
        def ensure_configured(self, *_): return SimpleNamespace(completed=True, roster=None)
    messages = Messages()
    messages.information = lambda *args: observed.append("no_data")
    controller = DesktopStartupController(settings=SimpleNamespace(document_output_directory=tmp_path),
        document_hub_dialog_type=Hub, document_source_dialog_type=Source,
        document_period_dialog_type=Period, document_meal_dialog_type=Meals,
        delivery_substitute_dialog_type=Substitute, document_output_dialog_type=Output,
        export_password_controller_type=Password, message_box=messages)
    controller._run_document_hub(session, window)
    assert not messages.errors
    assert session.database.document_runs() == ()
    if mode == "accept":
        assert len(emitted) == 1
        assert emitted[0].document.substitute_options.orientation == "landscape"
    else:
        assert not emitted
        assert observed == (["no_data"] if mode == "no_data" else ["options"])


def test_options_preferences_are_database_scoped_accept_only_and_tolerate_removed_fields(app, session, tmp_path):
    from PySide6.QtCore import QSettings
    import json
    settings = QSettings(str(tmp_path / "substitute-options.ini"), QSettings.Format.IniFormat)
    summary = session.database.revision_summaries()[0]
    period = DocumentPeriodSelection(DocumentAction.DELIVERY_SUBSTITUTE, summary, DAY, Meal.BREAKFAST, DAY, DAY)
    document = DocumentMealSelection(period, (Meal.BREAKFAST,))
    data = prepare_delivery_substitute_data(session.database.snapshot(summary.revision_id), DAY, Meal.BREAKFAST)
    def make(scope="database-a", prepared=data):
        dialog = DeliverySubstituteDialog(document, prepared)
        dialog.set_preferences_scope(scope, settings=settings)
        return dialog
    first = make()
    first.columns.setCurrentRow(0)
    first._move(1)
    first.orientation.setCurrentIndex(1)
    first.split.setCurrentIndex(1)
    first.group_limit.setValue(7)
    expected = first.options()
    first.accept()
    assert first.result() == QDialog.DialogCode.Accepted
    saved = settings.value(first._preferences_key)
    assert "driver" not in saved and "합성기사" not in saved
    reopened = make()
    assert reopened.options() == expected
    reopened.orientation.setCurrentIndex(0)
    reopened.reject()
    assert settings.value(first._preferences_key) == saved
    assert make().options() == expected
    assert make("database-b").options().orientation == "portrait"
    legacy = json.loads(saved)
    legacy["orientation"] = "auto"
    settings.setValue(first._preferences_key, json.dumps(legacy))
    assert make().options().orientation == "portrait"
    settings.setValue(first._preferences_key, saved)
    # Removed custom IDs are ignored; known selected/order keys survive.
    altered = replace(data, columns=tuple(column for column in data.columns if not column.key.startswith("custom:")),
                      default_columns=tuple(key for key in data.default_columns if not key.startswith("custom:")))
    restored = make(prepared=altered)
    assert restored.options().columns == tuple(key for key in expected.columns if not key.startswith("custom:"))
    # A corrupt preference remains recoverable and is never rewritten on cancel.
    settings.setValue(first._preferences_key, "not json")
    invalid = make()
    assert invalid.options().columns == data.default_columns
    invalid.reject()
    assert settings.value(first._preferences_key) == "not json"
